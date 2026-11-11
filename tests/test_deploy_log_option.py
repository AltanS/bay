"""``bay deploy`` and ``bay provision`` take ``--log <file>``, as ``bay up`` does.

The redirect is :func:`bay_cli.commands.output.routed_output`, the one that
``bay up``, ``bay plan``, ``bay remove`` and ``bay host upgrade`` use. With
``--log``, the progress and the Ansible output (a child that inherits the
descriptors too) go to the file, and the terminal gets only the end summary:
the result word, the PLAY RECAP counts per host and the log path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_plan_up import _git_identity, box, runner, world  # noqa: F401  (fixtures)
from typer.testing import CliRunner

from bay_cli.cli import app
from bay_cli.commands import host_cmd, ops, output, project_cmd, remove_cmd

_CHILD = (
    "import sys; sys.stderr.write('CHILD-STDERR-LINE\\n'); "
    "sys.stdout.write('CHILD-STDOUT-LINE\\n')"
)


# ── the option reaches the shared helper ────────────────────────────────────


def _ctx(*args: str) -> Any:
    return SimpleNamespace(args=list(args))


@pytest.mark.parametrize(
    "args, expected_log, expected_args",
    [
        (["--log", "/tmp/a.log"], "/tmp/a.log", []),
        (["--log=/tmp/a.log"], "/tmp/a.log", []),
        (["--tags", "x", "--log", "/tmp/a.log", "--offline"], "/tmp/a.log", ["--tags", "x", "--offline"]),
        (["--", "--log", "/tmp/a.log"], None, ["--", "--log", "/tmp/a.log"]),
        (["--check"], None, ["--check"]),
        (["--log"], None, ["--log"]),
    ],
)
def test_take_log_option_rescues_the_flag_before_the_separator(
    args: list[str], expected_log: str | None, expected_args: list[str]
) -> None:
    ctx = _ctx(*args)
    got = output.take_log_option(ctx, None)
    assert (str(got) if got else None) == expected_log
    assert ctx.args == expected_args


def test_a_declared_log_option_wins_over_nothing_and_a_rescued_one_wins_over_declared() -> None:
    ctx = _ctx()
    assert output.take_log_option(ctx, Path("/x")) == Path("/x")


@pytest.mark.parametrize(
    "command", [ops.deploy, ops.provision, host_cmd.upgrade, project_cmd.up, project_cmd.plan, remove_cmd.remove]
)
def test_every_command_declares_the_same_log_option(command: Any) -> None:
    import inspect
    import typing

    hints = typing.get_type_hints(command, include_extras=True)
    assert hints["log"] == output.LogOpt


def test_every_command_uses_the_one_redirect_helper() -> None:
    assert project_cmd.routed_output is output.routed_output
    import inspect

    assert "route_command_output" in inspect.getsource(ops.deploy)
    assert "route_command_output" in inspect.getsource(ops.provision)
    assert "routed_output" in inspect.getsource(host_cmd.upgrade)
    assert not hasattr(project_cmd, "_routed_output_copy")
    assert "def routed_output" not in inspect.getsource(project_cmd)


# ── through the real command ────────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["deploy", "provision"])
@pytest.mark.parametrize("where", ["before", "after"])
def test_log_takes_progress_and_a_child_that_inherits_the_descriptors(
    world: dict[str, Path],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    verb: str,
    where: str,
) -> None:
    from bay_cli import runner as runmod

    seen: dict[str, Any] = {}

    def fake_gate(ctx: Any, cx: Any, v: str, env: str, **kw: Any) -> None:
        seen["gate_args"] = list(ctx.args or [])

    def fake_playbook(cx: Any, playbook: str, env: str, tags: Any, extra: list[str], **kw: Any) -> None:
        seen["extra"] = list(extra)
        runmod.run([sys.executable, "-c", _CHILD], capture=False)

    monkeypatch.setattr(ops, "_fleet_gate", fake_gate)
    monkeypatch.setattr(ops, "_validate_env", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_run_playbook", fake_playbook)
    monkeypatch.setattr(ops, "_show_headscale_onboarding", lambda cx: None)
    monkeypatch.setattr(ops, "_check_rig_state", lambda *a, **k: False)
    monkeypatch.setattr(ops, "_record_rig_matched", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_invalidate_rig_cache", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_config_files_root", lambda cx, reach: __import__("contextlib").nullcontext(None))
    monkeypatch.setattr("bay_cli.commands.validate.run_validation", lambda *a, **k: SimpleNamespace(total_issues=0))

    log = tmp_path / "run.log"
    args = ["--fleet", str(world["fleet"]), verb]
    if where == "before":
        args += ["--log", str(log), "production"]
    else:
        args += ["production", "--log", str(log)]
    if verb == "deploy":
        args[3:3] = ["--skip-healthcheck"]
    result = runner.invoke(app, [*args, "--", "--check"])
    assert result.exit_code == 0, result.output + str(result.exception)
    captured = capfd.readouterr()
    text = log.read_text()
    assert "CHILD-STDERR-LINE" in text and "CHILD-STDOUT-LINE" in text
    assert "CHILD" not in result.output + captured.out + captured.err
    assert "--log" not in seen["extra"] and str(log) not in seen["extra"]
    assert "--check" in seen["extra"]
    assert str(log) not in seen["gate_args"]


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_without_log_nothing_is_redirected(
    world: dict[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    monkeypatch.setattr(ops, "_fleet_gate", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_validate_env", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_run_playbook", lambda *a, **k: print("PLAYBOOK-LINE"))
    monkeypatch.setattr(ops, "_show_headscale_onboarding", lambda cx: None)
    args = ["--fleet", str(world["fleet"]), verb, "production"]
    if verb == "deploy":
        args = ["--fleet", str(world["fleet"]), verb, "--rig", "--skip-validate", "--skip-healthcheck", "production"]
        monkeypatch.setattr(ops, "_record_rig_matched", lambda *a, **k: None)
        monkeypatch.setattr(ops, "_config_files_root", lambda cx, reach: __import__("contextlib").nullcontext(None))
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output + str(result.exception)
    assert "PLAYBOOK-LINE" in result.output + capfd.readouterr().out


# ── the end summary on the terminal ─────────────────────────────────────────

_RECAP = (
    "PLAY RECAP *********************************************************\n"
    "\x1b[0;33malpha-host\x1b[0m                 : \x1b[0;32mok=41  \x1b[0m \x1b[0;33mchanged=6   \x1b[0m "
    "unreachable=0    failed=0    skipped=9    rescued=0    ignored=0   \n"
    "\x1b[0;32mbeta-host\x1b[0m                  : ok=40   changed=0    unreachable=0    failed=0    "
    "skipped=9    rescued=0    ignored=0   \n"
    "\n"
    "Playbook run took 0 days, 0 hours, 2 minutes, 3 seconds\n"
)


def test_play_recap_reads_the_last_block_per_host_and_drops_colour() -> None:
    text = "PLAY RECAP\nalpha-host : ok=1 changed=1 unreachable=0 failed=1\n\nTASK [x]\nalpha-host : ok=9\n" + _RECAP
    recap = output.play_recap(text)
    assert recap == {
        "alpha-host": {"ok": 41, "changed": 6, "unreachable": 0, "failed": 0, "skipped": 9,
                       "rescued": 0, "ignored": 0},
        "beta-host": {"ok": 40, "changed": 0, "unreachable": 0, "failed": 0, "skipped": 9,
                      "rescued": 0, "ignored": 0},
    }
    assert output.play_recap("no recap here") == {}


def test_a_failed_host_in_the_recap_makes_the_summary_failed(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("PLAY RECAP\nalpha-host : ok=3 changed=0 unreachable=1 failed=0\n")
    lines = output.summary_lines("bay deploy", ok=True, log=log)
    assert lines[0] == "bay deploy: FAILED"
    assert "  alpha-host: ok=3 changed=0 unreachable=1 failed=0" in lines


def _fake_deploy(monkeypatch: pytest.MonkeyPatch, playbook: Any) -> None:
    monkeypatch.setattr(ops, "_fleet_gate", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_validate_env", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_run_playbook", playbook)
    monkeypatch.setattr(ops, "_show_headscale_onboarding", lambda cx: None)
    monkeypatch.setattr(ops, "_check_rig_state", lambda *a, **k: False)
    monkeypatch.setattr(ops, "_record_rig_matched", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_invalidate_rig_cache", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_config_files_root", lambda cx, reach: __import__("contextlib").nullcontext(None))
    monkeypatch.setattr("bay_cli.commands.validate.run_validation", lambda *a, **k: SimpleNamespace(total_issues=0))


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_log_ends_with_a_summary_on_the_terminal(
    world: dict[str, Path],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    _fake_deploy(monkeypatch, lambda *a, **k: print(_RECAP, flush=True))
    log = tmp_path / "run.log"
    log.write_text("PLAY RECAP\nold-host : ok=1 changed=0 unreachable=0 failed=1\n")
    args = ["--fleet", str(world["fleet"]), verb, "--log", str(log), "production"]
    if verb == "deploy":
        args.insert(3, "--skip-healthcheck")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output + str(result.exception)
    out = result.output
    assert f"bay {verb}: ok" in out
    assert "alpha-host: ok=41 changed=6 failed=0" in out
    assert "beta-host: ok=40 changed=0 failed=0" in out
    assert "old-host" not in out
    assert f"log: {log}" in out
    assert "PLAY RECAP" not in out and "Playbook run took" not in out


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_a_failed_run_with_log_says_failed_on_the_terminal(
    world: dict[str, Path],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    from bay_cli.errors import BayError

    def failing(*a: Any, **k: Any) -> None:
        # The recap says failed=0: the word comes from the exception alone.
        print(_RECAP, flush=True)
        raise BayError("Command failed with exit code 2")

    _fake_deploy(monkeypatch, failing)
    log = tmp_path / "run.log"
    args = ["--fleet", str(world["fleet"]), verb, "--log", str(log), "production"]
    if verb == "deploy":
        args.insert(3, "--skip-healthcheck")
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert f"bay {verb}: FAILED" in result.output
    assert f"log: {log}" in result.output
