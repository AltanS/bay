"""A secret that `bay gateway` mints goes to stdout once, alone.

`bay gateway apikey`, `key` and `enroll` used to print help text around the key
when stdout was not a terminal, and the API key twice. `bay gateway apikey |
bay vault set <env> <name>` then stored the help text. The rule now: stdout
carries ONLY the secret and one newline, in every mode (`enroll --json` keeps
its document). Headers, hints and errors go to stderr. On a terminal the key
still shows in the alternate screen.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_gateway import _RecordingBackend, gateway_stub  # noqa: F401  (fixture)
from typer.testing import CliRunner

from bay_cli.cli import app
from bay_cli.commands import gateway
from bay_cli.errors import BayError

runner = CliRunner()

API_KEY = "hskey-api-placeholder-key"
PREAUTH = "nodekey:fake"


class _Backend(_RecordingBackend):
    api_key: str = API_KEY

    def create_api_key(self, expiration: str) -> str:
        self.calls.append(("create_api_key", expiration))
        return self.api_key


@pytest.fixture()
def backend(gateway_stub: _RecordingBackend, monkeypatch: pytest.MonkeyPatch) -> _Backend:  # noqa: F811
    stub = _Backend()
    monkeypatch.setattr(gateway, "_make_backend", lambda *a, **k: stub)
    return stub


def _out(result: Any) -> tuple[str, str]:
    return result.stdout, result.stderr


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_apikey_stdout_is_the_key_once(backend: _Backend, flags: list[str]) -> None:
    result = runner.invoke(app, [*flags, "gateway", "apikey"])
    assert result.exit_code == 0, result.output
    stdout, stderr = _out(result)
    assert stdout == API_KEY + "\n"
    assert API_KEY not in stderr, "the key must not reach a log through stderr"
    assert "bay gateway apikey | bay vault set" in stderr


def test_apikey_passes_the_expiration(backend: _Backend) -> None:
    result = runner.invoke(app, ["gateway", "apikey", "--expiration", "90d"])
    assert result.stdout == API_KEY + "\n"
    assert backend.calls == [("create_api_key", "90d")]


def test_apikey_error_leaves_stdout_empty(backend: _Backend) -> None:
    backend.api_key = ""
    result = runner.invoke(app, ["gateway", "apikey"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "No API key returned" in result.stderr


@pytest.mark.parametrize("args", [["--expiration", "soon"], ["create", "extra"]])
def test_apikey_bad_input_leaves_stdout_empty(backend: _Backend, args: list[str]) -> None:
    result = runner.invoke(app, ["gateway", "apikey", *args])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr != ""
    assert backend.calls == []


def test_a_bay_error_before_the_key_goes_to_stderr(
    backend: _Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise BayError("no gateway here", hint="Configure it.")

    monkeypatch.setattr(gateway, "_get_gateway_config", boom)
    for cmd in (["apikey"], ["key", "alice"], ["enroll", "--user", "alice"]):
        result = runner.invoke(app, ["gateway", *cmd])
        assert result.exit_code != 0, cmd
        assert result.stdout == "", cmd
        assert "no gateway here" in result.stderr and "Configure it." in result.stderr, cmd


def test_key_stdout_is_the_key_once(backend: _Backend) -> None:
    result = runner.invoke(app, ["gateway", "key", "alice"])
    assert result.exit_code == 0, result.output
    stdout, stderr = _out(result)
    assert stdout == PREAUTH + "\n"
    assert PREAUTH not in stderr
    assert "Generating pre-auth key for: alice" in stderr


def test_key_with_json_flag_still_prints_only_the_key(backend: _Backend) -> None:
    result = runner.invoke(app, ["--json", "gateway", "key", "alice"])
    assert result.exit_code == 0, result.output
    assert result.stdout == PREAUTH + "\n"


def test_key_for_an_unknown_user_leaves_stdout_empty(backend: _Backend) -> None:
    result = runner.invoke(app, ["gateway", "key", "ghost"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "not found" in result.stderr


def test_enroll_stdout_is_the_key_once(backend: _Backend) -> None:
    result = runner.invoke(app, ["gateway", "enroll", "--user", "alice", "--hostname", "alice-laptop"])
    assert result.exit_code == 0, result.output
    stdout, stderr = _out(result)
    assert stdout == PREAUTH + "\n"
    assert PREAUTH not in stderr
    assert "tailscale up --login-server=https://hs.example.com --authkey=<key>" in stderr
    assert "--hostname=alice-laptop" in stderr
    assert "Pre-auth key generated for 'alice'" in stderr


def test_enroll_json_keeps_its_document(backend: _Backend) -> None:
    result = runner.invoke(app, ["--json", "gateway", "enroll", "--user", "alice", "--tag", "tag:agent"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["ok"] is True and doc["command"] == "gateway.enroll"
    data = doc["data"]
    assert data["key"] == PREAUTH
    assert data["command"].startswith("tailscale up --login-server=https://hs.example.com --authkey=nodekey:fake")
    assert set(data) == {
        "user", "created", "key", "command", "hostname", "tags", "default_deny", "acl_policy_file",
    }


def test_enroll_failure_leaves_stdout_empty(backend: _Backend) -> None:
    backend.generate_preauth_key = lambda *a, **k: ""  # type: ignore[method-assign]
    result = runner.invoke(app, ["gateway", "enroll", "--user", "alice"])
    assert result.exit_code != 0
    assert result.stdout == ""
    assert "No key returned" in result.stderr


# ── a terminal keeps the alternate screen ────────────────────────────────────


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_on_a_terminal_the_key_shows_in_the_alternate_screen_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown: list[tuple[str, str | None]] = []
    monkeypatch.setattr(gateway, "show_ephemeral", lambda c, clipboard=None: shown.append((c, clipboard)))
    tty = _Tty()
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setattr("bay_cli.console.output._json_mode", False)
    with gateway._secret_output() as deliver:
        deliver("SECRET-VALUE", screen="screen with SECRET-VALUE", hints="hints")
    assert shown == [("screen with SECRET-VALUE", "SECRET-VALUE")]
    assert tty.getvalue() == ""


def test_off_a_terminal_the_screen_is_never_shown(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr(gateway, "show_ephemeral", lambda *a, **k: pytest.fail("no alternate screen"))
    with gateway._secret_output() as deliver:
        print("progress")
        deliver("SECRET-VALUE", screen="screen", hints="the hint")
    captured = capsys.readouterr()
    assert captured.out == "SECRET-VALUE\n"
    assert "progress" in captured.err and "the hint" in captured.err
