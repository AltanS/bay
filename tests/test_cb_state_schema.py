"""Tests for M75-S7 Phase 2: circuit-breaker state file schema v1.

These tests render rebuild.sh.j2, extract the CB-state bash helpers
(`_read_state`, `_write_state`, `_record_failure`, `_reset_cb`,
`_clean_state_json`), and exercise them in a real bash subprocess
against a temp STATE_FILE.

Each helper is isolated from the rest of the script by only sourcing
the functions we need plus enough scaffolding (STATE_DIR, STATE_FILE,
CB_MAX_FAILURES, HOSTNAME, SERVICE, TELEGRAM_* stubbed empty, etc.).
`notify_build` is stubbed as a no-op so failures don't try to hit
the network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Reuse the Jinja render shim from test_rebuild_config so we stay in
# sync with the canonical template context.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_rebuild_config import _render_rebuild_sh, _local_service  # noqa: E402


# ── Helper extraction ───────────────────────────────────────────────


def _extract_helper(rendered: str, fn_name: str) -> str:
    """Extract a bash function definition from the rendered template."""
    # Match `FN() {` through the matching closing brace at column 0.
    pattern = rf"^{re.escape(fn_name)}\(\) \{{.*?^\}}"
    match = re.search(pattern, rendered, flags=re.DOTALL | re.MULTILINE)
    assert match is not None, f"Could not find {fn_name}() in rendered template"
    return match.group(0)


def _helpers_bash(rendered: str) -> str:
    """Build a self-contained bash preamble with the CB-state helpers."""
    parts = [
        # Pulled from the rendered script (it comes from the shared
        # alert_channel snippet) rather than re-implemented here, so the
        # escaping the real alert path uses is the escaping under test.
        _extract_helper(rendered, "bay_html_escape"),
        _extract_helper(rendered, "_log"),
        _extract_helper(rendered, "_clean_state_json"),
        _extract_helper(rendered, "_read_state"),
        _extract_helper(rendered, "_write_state"),
        _extract_helper(rendered, "_record_failure"),
        _extract_helper(rendered, "_reset_cb"),
    ]
    return "\n\n".join(parts)


def _run_bash(
    rendered: str,
    script: str,
    *,
    state_dir: Path,
    cb_max_failures: int = 3,
    service: str = "svc",
) -> subprocess.CompletedProcess:
    """Run a bash snippet with the CB helpers sourced.

    notify_build is stubbed, STATE_* vars are wired to `state_dir`.
    Uses `set -uo pipefail` (not -e) so jq errors don't abort the
    harness before we can inspect state.
    """
    state_file = state_dir / f"{service}.json"
    triggers_dir = state_dir.parent / "triggers"
    triggers_dir.mkdir(parents=True, exist_ok=True)
    preamble = f"""#!/usr/bin/env bash
set -uo pipefail
STATE_DIR={str(state_dir)!r}
STATE_FILE={str(state_file)!r}
STACK_DIR={str(state_dir.parent)!r}
CB_MAX_FAILURES={cb_max_failures}
SERVICE={service!r}
HOSTNAME="testhost"
TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
MSG_HEADER=""
notify_build() {{ :; }}
format_timestamp() {{ echo "Jan 01, 00:00 UTC"; }}
"""
    full = preamble + "\n" + _helpers_bash(rendered) + "\n" + script
    return subprocess.run(
        ["bash", "-c", full], capture_output=True, text=True, check=False
    )


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def rendered():
    services = _local_service()
    return _render_rebuild_sh(services, ["localapp"], git_deploy_services=["localapp"])


@pytest.fixture()
def tmp_state(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


# ── Tests ───────────────────────────────────────────────────────────


class TestReadState:
    """_read_state emits schema-v1 JSON in all cases."""

    def test_missing_file_emits_clean_state(self, rendered, tmp_state):
        proc = _run_bash(rendered, "_read_state", state_dir=tmp_state)
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data == {
            "version": 1,
            "consecutive_failures": 0,
            "opened_at": None,
            "last_failure": None,
            "alerts": {"opened_sent": False, "last_blocked_alert_at": None},
        }

    def test_malformed_file_emits_clean_state(self, rendered, tmp_state):
        (tmp_state / "svc.json").write_text("this is not json {{{")
        proc = _run_bash(rendered, "_read_state", state_dir=tmp_state)
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data["version"] == 1
        assert data["consecutive_failures"] == 0
        assert data["last_failure"] is None
        assert data["opened_at"] is None
        assert data["alerts"] == {
            "opened_sent": False,
            "last_blocked_alert_at": None,
        }

    def test_migration_from_legacy_shape(self, rendered, tmp_state):
        """Old {failures, last_failure, last_notified_sha} migrates to v1."""
        legacy = {
            "failures": 3,
            "last_failure": "2026-01-01T00:00:00+00:00",
            "last_notified_sha": "abc123",
        }
        (tmp_state / "svc.json").write_text(json.dumps(legacy))
        proc = _run_bash(rendered, "_read_state", state_dir=tmp_state)
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data["version"] == 1
        assert data["consecutive_failures"] == 3
        # opened_at must NOT be inferred — we don't know whether the legacy
        # counter crossed the threshold
        assert data["opened_at"] is None
        assert data["last_failure"] == {
            "sha": "abc123",
            "stage": "",
            "reason": "",
            "at": "2026-01-01T00:00:00+00:00",
        }
        assert data["alerts"] == {
            "opened_sent": False,
            "last_blocked_alert_at": None,
        }

    def test_migration_with_only_failures_field(self, rendered, tmp_state):
        """Legacy file with just {failures:N} and no SHA still migrates."""
        (tmp_state / "svc.json").write_text('{"failures":2}')
        proc = _run_bash(rendered, "_read_state", state_dir=tmp_state)
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data["consecutive_failures"] == 2
        assert data["last_failure"] is None
        assert data["opened_at"] is None

    def test_v1_file_roundtrips(self, rendered, tmp_state):
        """A schema-v1 file is returned as-is (with defensive fill-ins)."""
        v1 = {
            "version": 1,
            "consecutive_failures": 2,
            "opened_at": None,
            "last_failure": {
                "sha": "deadbeef",
                "stage": "Build",
                "reason": "compile error",
                "at": "2026-04-16T15:00:00+00:00",
            },
            "alerts": {"opened_sent": False, "last_blocked_alert_at": None},
        }
        (tmp_state / "svc.json").write_text(json.dumps(v1))
        proc = _run_bash(rendered, "_read_state", state_dir=tmp_state)
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data == v1


class TestWriteState:
    """_write_state writes atomically and the result parses as JSON."""

    def test_write_and_read_back(self, rendered, tmp_state):
        payload = json.dumps(
            {
                "version": 1,
                "consecutive_failures": 1,
                "opened_at": None,
                "last_failure": None,
                "alerts": {
                    "opened_sent": False,
                    "last_blocked_alert_at": None,
                },
            }
        )
        script = f"printf {payload!r} | _write_state\ncat \"${{STATE_FILE}}\""
        proc = _run_bash(rendered, script, state_dir=tmp_state)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["consecutive_failures"] == 1

    def test_atomic_write_produces_valid_json_after_each_call(
        self, rendered, tmp_state
    ):
        """After N back-to-back writes the file always parses as JSON.

        We can't truly observe a torn write from userspace without a
        parallel reader, but the contract we care about is: at any
        moment the file either doesn't exist or contains a parseable
        JSON document. Repeated writes shouldn't leave a half-written
        file visible under STATE_FILE (that's what the tmp + mv gives
        us).
        """
        script = """
        for i in $(seq 1 20); do
          printf '{"version":1,"consecutive_failures":%d,"opened_at":null,"last_failure":null,"alerts":{"opened_sent":false,"last_blocked_alert_at":null}}' "$i" | _write_state
          # Read back immediately — must always parse
          jq -e . "${STATE_FILE}" >/dev/null || { echo "parse fail iter $i"; exit 1; }
        done
        echo OK
        """
        proc = _run_bash(rendered, script, state_dir=tmp_state)
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout

    def test_write_creates_state_dir(self, rendered, tmp_path):
        """_write_state mkdir -p's the state dir if missing."""
        nested = tmp_path / "does-not-exist-yet"
        # Don't pre-create it; let _write_state handle it.
        script = (
            "rm -rf \"${STATE_DIR}\"\n"
            'printf "%s" "$(_clean_state_json)" | _write_state\n'
            'test -f "${STATE_FILE}" && echo FILE_EXISTS'
        )
        proc = _run_bash(rendered, script, state_dir=nested)
        assert proc.returncode == 0, proc.stderr
        assert "FILE_EXISTS" in proc.stdout


class TestRecordFailure:
    """_record_failure populates last_failure fields and manages opened_at."""

    def test_populates_last_failure_reason_from_arg3(self, rendered, tmp_state):
        script = '_record_failure "abc123" "Build" "compile error on line 42"'
        proc = _run_bash(rendered, script, state_dir=tmp_state, cb_max_failures=5)
        assert proc.returncode == 0, proc.stderr
        data = json.loads((tmp_state / "svc.json").read_text())
        assert data["last_failure"]["reason"] == "compile error on line 42"
        assert data["last_failure"]["stage"] == "Build"
        assert data["last_failure"]["sha"] == "abc123"
        assert data["last_failure"]["at"] != ""

    def test_opened_at_not_set_before_threshold(self, rendered, tmp_state):
        script = '_record_failure "sha1" "Build" "err"'
        proc = _run_bash(rendered, script, state_dir=tmp_state, cb_max_failures=3)
        assert proc.returncode == 0, proc.stderr
        data = json.loads((tmp_state / "svc.json").read_text())
        assert data["consecutive_failures"] == 1
        assert data["opened_at"] is None

    def test_opened_at_set_when_breaker_trips(self, rendered, tmp_state):
        """Three failures with CB_MAX=3 — opened_at set on the 3rd."""
        script = """
        _record_failure "sha1" "Build" "err1"
        _record_failure "sha2" "Build" "err2"
        _record_failure "sha3" "Build" "err3"
        """
        proc = _run_bash(rendered, script, state_dir=tmp_state, cb_max_failures=3)
        assert proc.returncode == 0, proc.stderr
        data = json.loads((tmp_state / "svc.json").read_text())
        assert data["consecutive_failures"] == 3
        assert data["opened_at"] is not None
        assert data["opened_at"] != ""
        assert data["last_failure"]["sha"] == "sha3"
        assert data["last_failure"]["reason"] == "err3"

    def test_opened_at_preserved_across_additional_failures(
        self, rendered, tmp_state
    ):
        """Once set, opened_at is preserved on subsequent failures."""
        script = """
        _record_failure "sha1" "Build" "err1"
        _record_failure "sha2" "Build" "err2"
        _record_failure "sha3" "Build" "err3"
        FIRST_OPEN=$(jq -r '.opened_at' "${STATE_FILE}")
        sleep 1
        _record_failure "sha4" "Build" "err4"
        SECOND_OPEN=$(jq -r '.opened_at' "${STATE_FILE}")
        [[ "${FIRST_OPEN}" == "${SECOND_OPEN}" ]] && echo PRESERVED || echo "CHANGED: ${FIRST_OPEN} -> ${SECOND_OPEN}"
        """
        proc = _run_bash(rendered, script, state_dir=tmp_state, cb_max_failures=3)
        assert proc.returncode == 0, proc.stderr
        assert "PRESERVED" in proc.stdout, proc.stdout + proc.stderr

    def test_alerts_fields_present_but_unmanaged(self, rendered, tmp_state):
        """Phase 2 guarantees alerts.* exist; Phase 3 will populate them."""
        script = '_record_failure "sha" "Build" "err"'
        proc = _run_bash(rendered, script, state_dir=tmp_state, cb_max_failures=3)
        assert proc.returncode == 0, proc.stderr
        data = json.loads((tmp_state / "svc.json").read_text())
        assert "alerts" in data
        assert data["alerts"]["opened_sent"] is False
        assert data["alerts"]["last_blocked_alert_at"] is None

    def test_consecutive_failures_increments(self, rendered, tmp_state):
        script = """
        _record_failure "sha1" "Build" "err"
        _record_failure "sha1" "Build" "err"
        """
        proc = _run_bash(rendered, script, state_dir=tmp_state, cb_max_failures=5)
        assert proc.returncode == 0, proc.stderr
        data = json.loads((tmp_state / "svc.json").read_text())
        assert data["consecutive_failures"] == 2

    def test_trigger_file_removed(self, rendered, tmp_state):
        """_record_failure removes the service's trigger file."""
        triggers_dir = tmp_state.parent / "triggers"
        triggers_dir.mkdir(parents=True, exist_ok=True)
        trigger = triggers_dir / "svc.trigger"
        trigger.write_text("push")
        script = '_record_failure "sha" "Build" "err"'
        proc = _run_bash(rendered, script, state_dir=tmp_state)
        assert proc.returncode == 0, proc.stderr
        assert not trigger.exists()


class TestResetCb:
    """_reset_cb writes a clean schema-v1 file (not a missing file)."""

    def test_reset_writes_clean_file(self, rendered, tmp_state):
        script = """
        _record_failure "sha" "Build" "err"
        _reset_cb
        """
        proc = _run_bash(rendered, script, state_dir=tmp_state)
        assert proc.returncode == 0, proc.stderr
        state_file = tmp_state / "svc.json"
        assert state_file.exists(), "_reset_cb must write a file, not delete it"
        data = json.loads(state_file.read_text())
        assert data == {
            "version": 1,
            "consecutive_failures": 0,
            "opened_at": None,
            "last_failure": None,
            "alerts": {"opened_sent": False, "last_blocked_alert_at": None},
        }

    def test_reset_on_fresh_install_writes_clean_file(self, rendered, tmp_state):
        """Even with no prior state, _reset_cb yields a valid clean file."""
        script = "_reset_cb"
        proc = _run_bash(rendered, script, state_dir=tmp_state)
        assert proc.returncode == 0, proc.stderr
        data = json.loads((tmp_state / "svc.json").read_text())
        assert data["version"] == 1
        assert data["consecutive_failures"] == 0
