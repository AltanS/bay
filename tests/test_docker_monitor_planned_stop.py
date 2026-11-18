"""docker-monitor does not page for a planned stop.

A deploy stops the old container with SIGTERM. Docker logs `kill
signal=15`, then `stop` and `die` (in either order), then `destroy`. An app
that exits 1 on SIGTERM (Node apps often do) made every deploy and every
webhook rebuild send container.crash (eu, 2026-11-18).

Now a non-zero `die` within docker_monitor_planned_stop_grace_seconds of a
stop request for the SAME container id is a planned stop: no alert, no
crash_state row. A die with no stop request, a die after the grace window, and
an OOM kill still alert.

These tests render the real template and drive its handlers, like
test_docker_monitor_routing.py.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import yaml

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_docker_monitor_routing import (  # noqa: E402,F401  (fixtures)
    _RECIPIENTS,
    _SECRET_URL,
    _load,
    _render,
    recipients_only,
    sent,
)

_REPO_ROOT = _TESTS_DIR.parent
_CID = "a" * 64


@pytest.fixture
def clock(monkeypatch):
    """A controllable time.monotonic()."""
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def no_inspect(monkeypatch):
    """Record `docker inspect` calls; answer OOMKilled=false by default."""
    calls: list[list[str]] = []
    answer = {"oom": "false"}

    class _Done:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Done(answer["oom"] + "\n")

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls, answer


def _monitor(tmp_path, **ctx):
    ctx.setdefault("alert_recipients", _RECIPIENTS)
    return _load(_render(tmp_path, **ctx))


def _event(action, name="web", cid=_CID, **attrs):
    return {
        "Type": "container",
        "Action": action,
        "id": cid,
        "Actor": {"ID": cid, "Attributes": {"name": name, **attrs}},
    }


def _kill(monitor, signal="15", **kw):
    monitor.handle_stop_request(_event("kill", signal=signal, **kw))


def _stop(monitor, **kw):
    monitor.handle_stop_request(_event("stop", **kw))


def _die(monitor, code="1", **kw):
    monitor.handle_die(_event("die", exitCode=code, **kw))


def _crashes(calls):
    return [c for c in calls if c["url"] == _SECRET_URL and "Container crash" in c["body"]]


def test_kill_stop_die_is_a_planned_stop(tmp_path, sent, recipients_only, clock, no_inspect, capsys):
    monitor = _monitor(tmp_path)
    _kill(monitor, "15")
    _stop(monitor)
    clock[0] += 2
    _die(monitor, "1")
    assert _crashes(sent) == []
    assert monitor.crash_state == {}
    assert "Planned stop: web (exit 1) after SIGTERM, no alert" in capsys.readouterr().out


def test_kill_die_stop_order_is_a_planned_stop(tmp_path, sent, recipients_only, clock, no_inspect):
    monitor = _monitor(tmp_path)
    _kill(monitor, "15")
    clock[0] += 1
    _die(monitor, "1")
    _stop(monitor)
    assert _crashes(sent) == []


@pytest.mark.parametrize("signal", ["2", "9", "SIGTERM", "SIGINT", "KILL"])
def test_every_stop_signal_counts(tmp_path, sent, recipients_only, clock, no_inspect, signal):
    monitor = _monitor(tmp_path)
    _kill(monitor, signal)
    _die(monitor, "1")
    assert _crashes(sent) == []


def test_stop_event_alone_counts(tmp_path, sent, recipients_only, clock, no_inspect, capsys):
    monitor = _monitor(tmp_path)
    _stop(monitor)
    _die(monitor, "1")
    assert _crashes(sent) == []
    assert "after docker stop" in capsys.readouterr().out


def test_die_without_a_stop_request_alerts(tmp_path, sent, recipients_only, clock, no_inspect):
    monitor = _monitor(tmp_path)
    _die(monitor, "1")
    assert len(_crashes(sent)) == 1
    assert monitor.crash_state["web"]["exit_code"] == 1


def test_a_reload_signal_is_not_a_stop(tmp_path, sent, recipients_only, clock, no_inspect):
    monitor = _monitor(tmp_path)
    _kill(monitor, "1")  # SIGHUP
    _die(monitor, "1")
    assert len(_crashes(sent)) == 1


def test_die_after_the_grace_window_alerts(tmp_path, sent, recipients_only, clock, no_inspect):
    monitor = _monitor(tmp_path)
    _kill(monitor, "15")
    clock[0] += 31
    _die(monitor, "1")
    assert len(_crashes(sent)) == 1


def test_grace_window_is_configurable(tmp_path, sent, recipients_only, clock, no_inspect):
    monitor = _monitor(tmp_path, docker_monitor_planned_stop_grace_seconds=120)
    _kill(monitor, "15")
    clock[0] += 100
    _die(monitor, "1")
    assert _crashes(sent) == []


def test_stop_of_another_container_id_does_not_cover(tmp_path, sent, recipients_only, clock, no_inspect):
    """The old container is stopped; the NEW one, same name, then crashes."""
    monitor = _monitor(tmp_path)
    _kill(monitor, "15", cid="old" * 10)
    _die(monitor, "1", cid="new" * 10)
    assert len(_crashes(sent)) == 1


def test_a_stop_request_covers_one_die_only(tmp_path, sent, recipients_only, clock, no_inspect):
    monitor = _monitor(tmp_path)
    _kill(monitor, "15")
    _die(monitor, "1")
    _die(monitor, "1")
    assert len(_crashes(sent)) == 1


def test_oom_event_alerts_after_a_stop_request(tmp_path, sent, recipients_only, clock, no_inspect):
    monitor = _monitor(tmp_path)
    _kill(monitor, "15")
    monitor.handle_oom(_event("oom"))
    _die(monitor, "137")
    assert len(_crashes(sent)) == 1
    assert monitor.crash_state["web"]["exit_code"] == 137


def test_exit_137_with_oom_killed_alerts(tmp_path, sent, recipients_only, clock, no_inspect):
    calls, answer = no_inspect
    answer["oom"] = "true"
    monitor = _monitor(tmp_path)
    _die(monitor, "137")
    assert len(_crashes(sent)) == 1
    assert calls and calls[0][:2] == ["docker", "inspect"]
    assert "{{.State.OOMKilled}}" in calls[0]
    assert calls[0][-1] == _CID


def test_exit_137_without_oom_stays_quiet(tmp_path, sent, recipients_only, clock, no_inspect):
    monitor = _monitor(tmp_path)
    _kill(monitor, "9")
    _die(monitor, "137")
    _die(monitor, "143", cid="b" * 64)
    assert _crashes(sent) == []


def test_inspect_failure_is_not_oom(tmp_path, sent, recipients_only, clock, monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=5)

    monkeypatch.setattr(subprocess, "run", boom)
    monitor = _monitor(tmp_path)
    _die(monitor, "137")
    assert _crashes(sent) == []


def test_planned_stop_sends_no_recovered(tmp_path, sent, recipients_only, clock, no_inspect):
    monitor = _monitor(tmp_path, alerts_enabled=["container.recovered"])
    _kill(monitor, "15")
    _die(monitor, "1")
    monitor.handle_start(_event("start", cid="c" * 64))
    assert [c for c in sent if "recovered" in c["body"]] == []


def test_grace_zero_turns_the_check_off(tmp_path, sent, recipients_only, clock, no_inspect):
    monitor = _monitor(tmp_path, docker_monitor_planned_stop_grace_seconds=0)
    _kill(monitor, "15")
    _die(monitor, "1")
    assert len(_crashes(sent)) == 1


def test_marks_are_bounded(tmp_path, clock, no_inspect):
    monitor = _monitor(tmp_path)
    for i in range(50):
        _kill(monitor, "15", cid=f"{i:064d}")
        monitor.handle_oom(_event("oom", cid=f"{i:064d}"))
    clock[0] += 31
    _kill(monitor, "15", cid="z" * 64)
    assert list(monitor.stop_requests) == ["z" * 64]
    assert monitor.oom_events == {}


def test_events_are_subscribed(tmp_path):
    source = _render(tmp_path)
    for name in ("die", "start", "health_status", "kill", "stop", "oom"):
        assert f'"--filter", "event={name}"' in source


def test_grace_default_is_in_the_role_defaults():
    defaults = yaml.safe_load(
        (_REPO_ROOT / "roles" / "docker_monitor" / "defaults" / "main.yml").read_text()
    )
    assert defaults["docker_monitor_planned_stop_grace_seconds"] == 30
