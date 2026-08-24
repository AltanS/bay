"""Tests for Telegram delivery hardening.

`send_telegram` in both rebuild.sh.j2 and the webhook app.py previously
swallowed failures silently (`>/dev/null 2>&1 || true`). This meant a
Telegram outage produced no operator-visible signal. The fix writes a
one-line record to `${STACK_DIR}/state/telegram-failures.log` (mounted
into the webhook container at `/state/telegram-failures.log`) on every
failed send. These tests verify the append-on-failure behavior and
that the happy path stays quiet.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WEBHOOK_DIR = str(
    Path(__file__).resolve().parent.parent
    / "roles"
    / "git_deploy"
    / "files"
    / "webhook"
)
if _WEBHOOK_DIR not in sys.path:
    sys.path.insert(0, _WEBHOOK_DIR)

import app as webhook_app  # noqa: E402


# ── rebuild.sh.j2 template checks ────────────────────────────────────────

# These assert on the RENDERED script rather than the template text. The
# delivery code now lives in the shared alert_channel snippet that rebuild.sh.j2
# includes, so grepping the template alone would test where the code is written
# instead of what actually runs on the host.

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_rebuild_config import _local_service, _render_rebuild_sh  # noqa: E402


def _rendered_rebuild() -> str:
    return _render_rebuild_sh(
        _local_service(), ["localapp"], git_deploy_services=["localapp"]
    )


def test_rebuild_alert_captures_http_code():
    """The alert path captures the HTTP status code so it can branch on success."""
    content = _rendered_rebuild()
    assert "-w '%{http_code}'" in content, (
        "the alert sender must capture the HTTP status code via -w '%{http_code}' "
        "so non-200 responses can be logged."
    )


def test_rebuild_alert_failure_log_is_wired_to_state_dir():
    """rebuild.sh points the shared failure log at ${STATE_DIR}/telegram-failures.log."""
    content = _rendered_rebuild()
    assert 'BAY_ALERT_FAILURE_LOG="${STATE_DIR}/telegram-failures.log"' in content, (
        "rebuild.sh must point BAY_ALERT_FAILURE_LOG at "
        "${STATE_DIR}/telegram-failures.log so delivery outages stay visible."
    )
    assert '"${http_code}" == "200"' in content, (
        "the alert sender must treat anything other than 200 as a delivery failure."
    )


def _extract_fn(rendered: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{.*?^\}}", rendered, flags=re.DOTALL | re.MULTILINE
    )
    assert match is not None, f"Could not find {name}() in the rendered script"
    return match.group(0)


def test_alert_failure_is_logged_and_never_raises(tmp_path):
    """An unreachable sink appends one failure record and still returns success.

    Executed rather than grepped: the guarantee that matters is that a dead
    alert endpoint cannot fail a build, and only running it proves that.
    """
    rendered = _rendered_rebuild()
    log = tmp_path / "state" / "telegram-failures.log"
    script = "\n".join(
        [
            "set -euo pipefail",
            "BAY_TG_TOKEN=''",
            "BAY_TG_CHAT=''",
            # Port 1 on loopback refuses immediately — no network, no waiting.
            "BAY_ALERT_URL='http://127.0.0.1:1/hook'",
            "BAY_ALERT_FORMAT='campfire'",
            "BAY_ALERT_MAX_CHARS=3500",
            "BAY_ALERT_TIMEOUT=2",
            f"BAY_ALERT_FAILURE_LOG={str(log)!r}",
            "BAY_ALERT_FAILURE_CONTEXT='service=svc host=testhost'",
            _extract_fn(rendered, "_bay_clip"),
            _extract_fn(rendered, "_bay_html_unescape"),
            _extract_fn(rendered, "_bay_to_mrkdwn"),
            _extract_fn(rendered, "_bay_strip_tags"),
            _extract_fn(rendered, "_bay_log_send_failure"),
            _extract_fn(rendered, "_bay_send_webhook"),
            '_bay_send_webhook "hello"',
            "echo REACHED_END",
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert "REACHED_END" in proc.stdout, (
        "a failed alert delivery aborted the script — it must be fail-open"
    )
    assert log.is_file(), "the delivery failure was not recorded"
    recorded = log.read_text().strip().splitlines()
    assert len(recorded) == 1, f"expected exactly one failure record, got {recorded}"
    assert "service=svc host=testhost" in recorded[0]
    assert "sink=campfire" in recorded[0]


# ── app.py unit tests ────────────────────────────────────────────────────


@pytest.fixture
def failures_log(tmp_path, monkeypatch):
    """Redirect TELEGRAM_FAILURES_LOG to a tmp file for the duration of one test."""
    log_path = tmp_path / "telegram-failures.log"
    monkeypatch.setattr(webhook_app, "TELEGRAM_FAILURES_LOG", log_path)
    return log_path


@pytest.fixture
def telegram_configured(monkeypatch):
    """Pretend Telegram is configured so send_telegram actually tries to POST."""
    monkeypatch.setattr(webhook_app, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(webhook_app, "TELEGRAM_CHAT_ID", "fake-chat")
    monkeypatch.setattr(webhook_app, "HOSTNAME", "test-host")


def test_record_telegram_failure_writes_log_entry(failures_log):
    """_record_telegram_failure appends a timestamped line to the log file."""
    webhook_app._record_telegram_failure("connection refused")
    assert failures_log.is_file()
    content = failures_log.read_text()
    assert "send_alert failed" in content
    assert "connection refused" in content


def test_record_telegram_failure_creates_parent_dir(tmp_path, monkeypatch):
    """The log parent directory is created on first write."""
    log_path = tmp_path / "nested" / "state" / "telegram-failures.log"
    monkeypatch.setattr(webhook_app, "TELEGRAM_FAILURES_LOG", log_path)
    webhook_app._record_telegram_failure("boom")
    assert log_path.is_file()


def test_record_telegram_failure_appends_not_overwrites(failures_log):
    """Multiple failures accumulate in the log rather than replacing each other."""
    webhook_app._record_telegram_failure("first")
    webhook_app._record_telegram_failure("second")
    lines = failures_log.read_text().splitlines()
    assert len(lines) == 2
    assert "first" in lines[0]
    assert "second" in lines[1]


def test_record_telegram_failure_never_raises(monkeypatch):
    """Log-write errors are swallowed — Telegram hardening must never cascade."""
    # Set path to something that will fail on open (e.g. a directory).
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(webhook_app, "TELEGRAM_FAILURES_LOG", Path(d))
        # Should not raise even though the path is a directory.
        webhook_app._record_telegram_failure("irrelevant")


def test_send_telegram_records_failure_on_urlopen_exception(
    failures_log, telegram_configured
):
    """send_telegram records to the failures log when urlopen raises."""
    with patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
        webhook_app.send_alert("alerts.test", "hello")
    assert failures_log.is_file()
    content = failures_log.read_text()
    assert "boom" in content
    assert "test-host" in content


def test_send_telegram_does_not_log_on_success(failures_log, telegram_configured):
    """The happy path writes nothing to the failures log."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__ = lambda self: self
        mock_urlopen.return_value.__exit__ = lambda *a: None
        webhook_app.send_alert("alerts.test", "hello")
    assert not failures_log.exists(), (
        "Successful sends must not pollute the failures log."
    )


def test_send_telegram_no_op_when_not_configured(failures_log, monkeypatch):
    """When Telegram is not configured, send_telegram returns without touching the log."""
    monkeypatch.setattr(webhook_app, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(webhook_app, "TELEGRAM_CHAT_ID", "")
    webhook_app.send_alert("alerts.test", "hello")
    assert not failures_log.exists()


# ── Deployment surface: webhook compose template mounts /state ───────────

_WEBHOOK_COMPOSE = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "deploy_stack"
    / "templates"
    / "_webhook_receiver.j2"
)


def test_webhook_compose_mounts_state_dir():
    """The webhook container must mount ${stack_dir}/state so the failures log persists."""
    content = _WEBHOOK_COMPOSE.read_text()
    assert "stack_dir }}/state:/state" in content, (
        "webhook container is missing the /state mount — telegram-failures.log "
        "would not persist across restarts."
    )
    assert "TELEGRAM_FAILURES_LOG=/state/telegram-failures.log" in content, (
        "webhook container needs TELEGRAM_FAILURES_LOG env var pointing at the mount."
    )


# ── Deployment surface: git_deploy task creates state dir ────────────────


def test_git_deploy_webhook_task_creates_state_dir():
    """The webhook deploy task must pre-create ${stack_dir}/state with write perms."""
    task_file = (
        Path(__file__).resolve().parent.parent
        / "roles"
        / "git_deploy"
        / "tasks"
        / "webhook.yml"
    )
    content = task_file.read_text()
    assert "stack_dir }}/state" in content, (
        "webhook.yml must ensure ${stack_dir}/state exists before the container starts."
    )
