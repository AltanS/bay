"""Tests for M78-S7: trigger stall watchdog template rendering and behaviour.

The watchdog is a systemd timer + oneshot service + bash script. These
tests render the templates via Jinja2, assert the expected content, and
run the rendered script in a tmpdir to verify that stale trigger files
are detected while fresh ones are not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from helpers import make_ansible_env

_REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = _REPO_ROOT / "roles" / "git_deploy" / "templates"


def _to_json(value):
    return json.dumps(value)


def _render(
    template_name: str,
    *,
    stack_dir: str = "/opt/teststack",
    app_user: str = "bay",
    threshold: int = 600,
    repeat_sec: int = 1800,
    telegram_token: str = "",
    telegram_chat: str = "",
    header: str = "[TEST] ",
    hostname: str = "test-host",
) -> str:
    env = make_ansible_env(TEMPLATE_DIR)
    env.filters["to_json"] = _to_json
    tmpl = env.get_template(template_name)
    return tmpl.render(
        ansible_managed="Ansible managed - test render",
        app_user=app_user,
        stack_dir=stack_dir,
        git_deploy_stall_watchdog_threshold=threshold,
        git_deploy_stall_watchdog_repeat_sec=repeat_sec,
        docker_monitor_telegram_bot_token=telegram_token,
        docker_monitor_telegram_chat_id=telegram_chat,
        docker_monitor_alert_header=header,
        inventory_hostname=hostname,
    )


# ── Timer unit ──────────────────────────────────────────────────────────


class TestTimerUnit:
    """bay-trigger-watchdog.timer.j2 — 5-minute recurring timer."""

    def test_onbootsec_rendered(self):
        out = _render("bay-trigger-watchdog.timer.j2")
        assert "OnBootSec=5min" in out

    def test_onunitactivesec_rendered(self):
        out = _render("bay-trigger-watchdog.timer.j2")
        assert "OnUnitActiveSec=5min" in out

    def test_persistent_true(self):
        """Persistent=true lets it catch up after host downtime."""
        out = _render("bay-trigger-watchdog.timer.j2")
        assert "Persistent=true" in out

    def test_targets_service_unit(self):
        out = _render("bay-trigger-watchdog.timer.j2")
        assert "Unit=bay-trigger-watchdog.service" in out

    def test_installed_in_timers_target(self):
        out = _render("bay-trigger-watchdog.timer.j2")
        assert "WantedBy=timers.target" in out


# ── Service unit ────────────────────────────────────────────────────────


class TestServiceUnit:
    """bay-trigger-watchdog.service.j2 — oneshot under app_user, no OnFailure."""

    def test_type_oneshot(self):
        out = _render("bay-trigger-watchdog.service.j2")
        assert "Type=oneshot" in out

    def test_runs_as_app_user(self):
        out = _render("bay-trigger-watchdog.service.j2", app_user="bay")
        assert "User=bay" in out

    def test_execstart_points_to_script(self):
        out = _render(
            "bay-trigger-watchdog.service.j2", stack_dir="/opt/teststack"
        )
        assert "ExecStart=/opt/teststack/bin/bay-trigger-watchdog.sh" in out

    def test_no_onfailure_cascade(self):
        """Watchdog must not cascade into bay-build-alert@ on failure.

        The bay-build-alert@ unit is reserved for per-service build failures.
        Routing watchdog self-failures through it would misclassify the alert.
        """
        out = _render("bay-trigger-watchdog.service.j2")
        # Strip commented lines so the explanatory comment about OnFailure=
        # being absent doesn't trip the check.
        code_only = "\n".join(
            line for line in out.splitlines() if not line.strip().startswith("#")
        )
        assert "OnFailure=" not in code_only


# ── Watchdog bash script ────────────────────────────────────────────────


class TestWatchdogScript:
    """bay-trigger-watchdog.sh.j2 — the detection logic."""

    def test_threshold_rendered(self):
        out = _render("bay-trigger-watchdog.sh.j2", threshold=600)
        assert "STALE_AGE_SEC=600" in out

    def test_threshold_custom_value(self):
        out = _render("bay-trigger-watchdog.sh.j2", threshold=1800)
        assert "STALE_AGE_SEC=1800" in out

    def test_find_uses_threshold_variable(self):
        out = _render("bay-trigger-watchdog.sh.j2")
        # The script converts STALE_AGE_SEC -> STALE_MINUTES -> find -mmin.
        assert 'find "${TRIGGER_DIR}"' in out
        assert "-mmin +" in out
        assert "STALE_MINUTES" in out

    def test_audit_log_path(self):
        out = _render("bay-trigger-watchdog.sh.j2")
        assert 'AUDIT_LOG="${STATE_DIR}/stall-watchdog.log"' in out

    def test_never_sets_strict_pipefail(self):
        """`set -euo pipefail` would turn any subshell failure into exit 1,
        which would fire a false systemd-level failure. The script must
        tolerate broken pipes, missing binaries, and race conditions."""
        out = _render("bay-trigger-watchdog.sh.j2")
        # Scan only non-comment lines so the explanatory comment about
        # NOT using strict mode doesn't trip the check.
        code_only = "\n".join(
            line for line in out.splitlines() if not line.strip().startswith("#")
        )
        assert "set -euo pipefail" not in code_only
        assert "set -e" not in code_only

    def test_always_exits_zero_at_end(self):
        """Final line must be `exit 0` — watchdog failures are silent."""
        out = _render("bay-trigger-watchdog.sh.j2").strip().splitlines()
        assert out[-1] == "exit 0"

    def test_no_exit_one_anywhere(self):
        """The script should never exit non-zero.

        Any `exit 1` would cascade into a systemd 'failed' state, which
        defeats the "watchdog alerts are self-contained" principle.
        """
        out = _render("bay-trigger-watchdog.sh.j2")
        for line in out.splitlines():
            stripped = line.strip()
            # Allow inside comments or strings, but no bare `exit 1`.
            if stripped.startswith("#"):
                continue
            assert "exit 1" not in stripped, f"Forbidden `exit 1` in: {line!r}"

    def test_reads_trigger_content(self):
        """Script reads trigger content so "pull" vs empty is distinguishable."""
        out = _render("bay-trigger-watchdog.sh.j2")
        assert "head -c" in out or "cat " in out

    def test_rate_limit_uses_state_file(self):
        out = _render("bay-trigger-watchdog.sh.j2")
        assert "watchdog-last-alert.json" in out
        assert "WATCHDOG_REPEAT_SEC" in out

    def test_clears_last_alert_on_clean_scan(self):
        """When the scan is clean, the last-alert snapshot is cleared so
        the next stall alerts immediately instead of being suppressed."""
        out = _render("bay-trigger-watchdog.sh.j2")
        assert 'rm -f "${LAST_ALERT_FILE}"' in out


# ── Integration smoke test ──────────────────────────────────────────────


def _write_rendered_script(tmp_path: Path, *, stack_dir: Path, threshold: int = 600) -> Path:
    """Render the script with stack_dir pointed at the tmpdir and write it out."""
    content = _render(
        "bay-trigger-watchdog.sh.j2",
        stack_dir=str(stack_dir),
        threshold=threshold,
        telegram_token="",  # disabled -> send_telegram no-ops
        telegram_chat="",
    )
    script = tmp_path / "bay-trigger-watchdog.sh"
    script.write_text(content)
    script.chmod(0o750)
    return script


def _run_script(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestWatchdogIntegration:
    """Render the script and run it against a tmpdir with stale + fresh files."""

    def test_detects_only_stale_files(self, tmp_path):
        stack_dir = tmp_path / "stack"
        (stack_dir / "triggers").mkdir(parents=True)
        (stack_dir / "state").mkdir(parents=True)

        triggers = stack_dir / "triggers"
        fresh = triggers / "fresh-svc.trigger"
        stale = triggers / "stale-svc.trigger"
        fresh.write_text("pull")
        stale.write_text("")  # empty = push trigger

        # Backdate stale by 1 hour, keep fresh at now.
        now = time.time()
        os.utime(stale, (now - 3600, now - 3600))
        os.utime(fresh, (now, now))

        script = _write_rendered_script(tmp_path, stack_dir=stack_dir)
        result = _run_script(script)

        assert result.returncode == 0, f"stderr: {result.stderr}"

        audit_log = stack_dir / "state" / "stall-watchdog.log"
        assert audit_log.is_file(), "Audit log was not written"
        log_contents = audit_log.read_text()
        assert "stale_count=1" in log_contents
        assert "action=alerted" in log_contents

        # Rate-limit snapshot should record exactly the stale service.
        snapshot = stack_dir / "state" / "watchdog-last-alert.json"
        assert snapshot.is_file()
        snapshot_data = json.loads(snapshot.read_text())
        assert snapshot_data["services"] == ["stale-svc"]
        assert isinstance(snapshot_data["ts_epoch"], int)

    def test_clean_scan_writes_zero_count(self, tmp_path):
        stack_dir = tmp_path / "stack"
        (stack_dir / "triggers").mkdir(parents=True)
        (stack_dir / "state").mkdir(parents=True)

        # Only fresh files.
        (stack_dir / "triggers" / "active.trigger").write_text("pull")

        script = _write_rendered_script(tmp_path, stack_dir=stack_dir)
        result = _run_script(script)
        assert result.returncode == 0, f"stderr: {result.stderr}"

        audit_log = stack_dir / "state" / "stall-watchdog.log"
        assert audit_log.is_file()
        assert "stale_count=0" in audit_log.read_text()
        assert "action=clean" in audit_log.read_text()

    def test_empty_triggers_dir_is_clean(self, tmp_path):
        stack_dir = tmp_path / "stack"
        (stack_dir / "triggers").mkdir(parents=True)
        (stack_dir / "state").mkdir(parents=True)

        script = _write_rendered_script(tmp_path, stack_dir=stack_dir)
        result = _run_script(script)
        assert result.returncode == 0

        audit_log = stack_dir / "state" / "stall-watchdog.log"
        assert "stale_count=0" in audit_log.read_text()

    def test_missing_triggers_dir_no_crash(self, tmp_path):
        """A fresh host without triggers/ must not crash the watchdog."""
        stack_dir = tmp_path / "stack"
        stack_dir.mkdir()  # No triggers/ subdir

        script = _write_rendered_script(tmp_path, stack_dir=stack_dir)
        result = _run_script(script)
        assert result.returncode == 0, f"stderr: {result.stderr}"

        audit_log = stack_dir / "state" / "stall-watchdog.log"
        assert audit_log.is_file()
        assert "no-trigger-dir" in audit_log.read_text()

    def test_rate_limit_suppresses_duplicate_set(self, tmp_path):
        """Second run with the same stale set within the repeat window suppresses the alert."""
        stack_dir = tmp_path / "stack"
        (stack_dir / "triggers").mkdir(parents=True)
        (stack_dir / "state").mkdir(parents=True)

        stale = stack_dir / "triggers" / "stuck.trigger"
        stale.write_text("")
        now = time.time()
        os.utime(stale, (now - 3600, now - 3600))

        script = _write_rendered_script(tmp_path, stack_dir=stack_dir)
        first = _run_script(script)
        assert first.returncode == 0

        second = _run_script(script)
        assert second.returncode == 0

        audit_log = (stack_dir / "state" / "stall-watchdog.log").read_text()
        assert "action=alerted" in audit_log
        assert "action=suppressed" in audit_log
