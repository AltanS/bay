"""Tests for the deploy/provision performance surface (M111-01, audit P3).

Covers:
- `run_playbook()` sets `ANSIBLE_STRATEGY=mitogen_linear` when the Mitogen
  strategy plugin is present in the framework venv.
- `--profile` puts
  `ANSIBLE_CALLBACKS_ENABLED=ansible.posix.profile_tasks,ansible.posix.timer`
  in the playbook environment.
- Without `--profile` that key is ABSENT, not empty — an empty value would
  clobber an operator's own callback list.
- Exactly one strategy line is printed before the playbook starts, naming
  either `mitogen_linear` or `linear (mitogen unavailable)`.
- The strategy line is suppressed in JSON mode.
- Both `deploy` and `provision` expose `--profile` and thread it through.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from bay_cli import ansible
from bay_cli.commands import ops
from bay_cli.console import output as console_output

CALLBACKS = "ansible.posix.profile_tasks,ansible.posix.timer"


@pytest.fixture(autouse=True)
def isolated_console_state():
    """Snapshot and restore the module-global console state.

    `bay_cli.console.output` keeps json-mode and the message buffer as module
    globals. A test that flips either one leaks into whatever runs next, and
    reads state whatever ran before it left behind — which makes the outcome
    depend on test order (pytest-randomly shuffles it every run). Each test in
    this file therefore starts from human mode with an empty buffer, and the
    previous values are put back afterwards.
    """
    prev_json = console_output.is_json_mode()
    prev_messages = console_output.drain_messages()
    console_output.set_json_mode(False)
    try:
        yield
    finally:
        console_output.drain_messages()
        console_output._message_buffer.extend(prev_messages)
        console_output.set_json_mode(prev_json)


def _bay_dir(tmp_path: Path, *, mitogen: bool) -> Path:
    bay_dir = tmp_path / ".bay"
    bay_dir.mkdir()
    if mitogen:
        (
            bay_dir / ".venv" / "lib" / "python3.12" / "site-packages"
            / "ansible_mitogen" / "plugins" / "strategy"
        ).mkdir(parents=True)
    return bay_dir


def _run(bay_dir: Path, **kwargs) -> dict[str, str]:
    """Call run_playbook with the subprocess stubbed; return the env it passed."""
    captured: dict[str, str] = {}

    def fake_run(cmd, **rkwargs):
        captured.update(rkwargs.get("env") or {})
        return None

    with patch.object(ansible.runner, "run", fake_run), patch.dict(
        "os.environ", {}, clear=False
    ):
        # BAY_NO_MITOGEN in the ambient env would flip the strategy under us.
        import os

        os.environ.pop("BAY_NO_MITOGEN", None)
        ansible.run_playbook("deploy", "production", bay_dir=bay_dir, **kwargs)
    return captured


# ── Strategy env ─────────────────────────────────────────────────────


def test_run_playbook_sets_mitogen_strategy(tmp_path):
    env = _run(_bay_dir(tmp_path, mitogen=True))
    assert env["ANSIBLE_STRATEGY"] == "mitogen_linear"
    assert env["ANSIBLE_STRATEGY_PLUGINS"].endswith(
        "ansible_mitogen/plugins/strategy"
    )


def test_run_playbook_without_mitogen_sets_no_strategy(tmp_path):
    env = _run(_bay_dir(tmp_path, mitogen=False))
    assert "ANSIBLE_STRATEGY" not in env


# ── --profile env contract ───────────────────────────────────────────


def test_profile_sets_callbacks_env(tmp_path):
    env = _run(_bay_dir(tmp_path, mitogen=True), profile=True)
    assert env["ANSIBLE_CALLBACKS_ENABLED"] == CALLBACKS


def test_without_profile_callbacks_key_is_absent(tmp_path):
    env = _run(_bay_dir(tmp_path, mitogen=True))
    assert "ANSIBLE_CALLBACKS_ENABLED" not in env


def test_profile_env_helper_is_empty_when_off():
    assert ansible._profile_env(False) == {}
    assert ansible._profile_env(True) == {"ANSIBLE_CALLBACKS_ENABLED": CALLBACKS}


# ── The printed strategy line ────────────────────────────────────────


def test_strategy_line_names_mitogen(tmp_path, capsys):
    _run(_bay_dir(tmp_path, mitogen=True))
    out = capsys.readouterr().out
    assert "strategy: mitogen_linear" in out
    assert out.count("strategy:") == 1


def test_strategy_line_reports_missing_mitogen(tmp_path, capsys):
    _run(_bay_dir(tmp_path, mitogen=False))
    out = capsys.readouterr().out
    assert "strategy: linear (mitogen unavailable)" in out


def test_strategy_line_reports_missing_mitogen_when_disabled(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BAY_NO_MITOGEN", "1")
    bay_dir = _bay_dir(tmp_path, mitogen=True)

    def fake_run(cmd, **rkwargs):
        return None

    with patch.object(ansible.runner, "run", fake_run):
        ansible.run_playbook("deploy", "production", bay_dir=bay_dir)
    assert "strategy: linear (mitogen unavailable)" in capsys.readouterr().out


def test_strategy_line_suppressed_in_json_mode(tmp_path, capsys):
    # The autouse fixture guarantees an empty buffer and restores json mode.
    console_output.set_json_mode(True)
    _run(_bay_dir(tmp_path, mitogen=True))
    assert "strategy:" not in capsys.readouterr().out
    assert not any("strategy:" in m for m in console_output.drain_messages())


# ── CLI surface ──────────────────────────────────────────────────────


def _app(cmd) -> typer.Typer:
    app = typer.Typer()
    # Same context settings as cli.py, so `--profile` after the positional env
    # lands in ctx.args exactly as it does in the real CLI.
    app.command(
        context_settings={"allow_extra_args": True, "allow_interspersed_args": False}
    )(cmd)
    return app


def test_deploy_and_provision_declare_profile():
    for cmd in (ops.deploy, ops.provision):
        assert "profile" in inspect.signature(cmd).parameters


def test_deploy_help_advertises_profile():
    result = CliRunner().invoke(_app(ops.deploy), ["--help"])
    assert result.exit_code == 0
    assert "--profile" in result.output


def test_provision_passes_profile_through(tmp_path):
    seen = {}

    def fake_run_playbook(playbook, env, tags, extra_args, **kwargs):
        seen["playbook"] = playbook
        seen["extra_args"] = extra_args
        seen["profile"] = kwargs.get("profile")

    with patch.object(ops.paths, "find_bay_dir", lambda: tmp_path / ".bay"), patch.object(
        ops, "_run_playbook", fake_run_playbook
    ):
        result = CliRunner().invoke(_app(ops.provision), ["--profile", "production"])

    assert result.exit_code == 0, result.output
    assert seen["profile"] is True
    assert "--profile" not in seen["extra_args"]


def test_provision_rescues_profile_after_env(tmp_path):
    seen = {}

    def fake_run_playbook(playbook, env, tags, extra_args, **kwargs):
        seen["extra_args"] = extra_args
        seen["profile"] = kwargs.get("profile")

    with patch.object(ops.paths, "find_bay_dir", lambda: tmp_path / ".bay"), patch.object(
        ops, "_run_playbook", fake_run_playbook
    ):
        result = CliRunner().invoke(_app(ops.provision), ["production", "--profile"])

    assert result.exit_code == 0, result.output
    assert seen["profile"] is True
    assert "--profile" not in seen["extra_args"]
