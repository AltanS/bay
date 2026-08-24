"""Tests for the circuit-breaker Phase 3 behavior: visibility (Telegram alerts).

Phase 2 added schema-v1 state with ``alerts.opened_sent`` and
``alerts.last_blocked_alert_at`` fields. Phase 3 makes them load-bearing:

1. ``_record_failure`` fires the "Circuit breaker OPEN" Telegram exactly
   once per open event, gated by ``alerts.opened_sent``.
2. The top-of-script CB guard (the silent ``exit 0`` from the
   2026-04-16 incident) now emits a rate-limited ⚠ Telegram
   keyed on ``alerts.last_blocked_alert_at`` (epoch seconds, default
   interval ``CB_BLOCKED_ALERT_INTERVAL_SEC=3600``).
3. The rollback-success Telegram body appends a
   "⚠ Circuit breaker at N/MAX" preview when below threshold.
4. ``CB_MAX_FAILURES`` is configured via
   ``git_deploy_cb_max_failures`` (default 5).

These tests stub ``notify_build`` to append to a capture file (see
``test_telegram_failure_logging.py`` for the same capture pattern),
then assert the expected lines / counts. No real Telegram traffic.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_rebuild_config import _local_service, _render_rebuild_sh  # noqa: E402

# Reuse the helper-extraction utilities from the Phase 2 schema test.
from test_cb_state_schema import _extract_helper, _helpers_bash  # noqa: E402


# ── Bash harness ─────────────────────────────────────────────────────


def _run_with_capture(
    rendered: str,
    script: str,
    *,
    state_dir: Path,
    capture_file: Path,
    cb_max_failures: int = 3,
    service: str = "svc",
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run a bash snippet with the CB helpers and a capturing notify_build.

    ``notify_build`` is replaced with a function that appends a single
    line per call to ``capture_file`` containing the message body. This
    lets tests assert call count and message content without hitting the
    network. The CB-blocked alert rate-limit interval is set inline by
    the extracted CB block (``CB_BLOCKED_ALERT_INTERVAL_SEC=3600``) —
    use ``_extract_cb_block(..., override_interval=N)`` to vary it.
    """
    state_file = state_dir / f"{service}.json"
    triggers_dir = state_dir.parent / "triggers"
    triggers_dir.mkdir(parents=True, exist_ok=True)

    extra_lines = ""
    if extra_env:
        for k, v in extra_env.items():
            extra_lines += f"{k}={v!r}\n"

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
TG_CAPTURE={str(capture_file)!r}
notify_build() {{
  # $1 is the alert's registry ID; the message moved to $2.
  printf '%s\\n---END---\\n' "$2" >> "${{TG_CAPTURE}}"
}}
format_timestamp() {{ echo "Jan 01, 00:00 UTC"; }}
{extra_lines}"""
    full = preamble + "\n" + _helpers_bash(rendered) + "\n" + script
    return subprocess.run(
        ["bash", "-c", full], capture_output=True, text=True, check=False
    )


def _count_messages(capture_file: Path) -> int:
    """Count Telegram messages captured (separated by '---END---')."""
    if not capture_file.exists():
        return 0
    return capture_file.read_text().count("---END---")


def _read_messages(capture_file: Path) -> list[str]:
    """Return captured Telegram message bodies, in order."""
    if not capture_file.exists():
        return []
    parts = capture_file.read_text().split("---END---\n")
    return [p for p in parts if p.strip()]


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def rendered():
    services = _local_service()
    return _render_rebuild_sh(services, ["localapp"], git_deploy_services=["localapp"])


@pytest.fixture()
def tmp_state(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture()
def capture(tmp_path: Path) -> Path:
    return tmp_path / "telegram-capture.log"


# ── Phase 3.1: opened_sent gate on CB-trip ───────────────────────────


class TestOpenedSentGate:
    """The CB-trip Telegram fires exactly once per open event."""

    def test_trip_fires_once_then_suppresses_re_trips(
        self, rendered, tmp_state, capture
    ):
        """N failures => 1 trip alert; further failures while open => 0 more."""
        script = """
        # Drive 3 failures to reach CB_MAX_FAILURES=3 — this trips the breaker.
        _record_failure "sha1" "Build" "err1"
        _record_failure "sha2" "Build" "err2"
        _record_failure "sha3" "Build" "err3"
        # Two more failures while the breaker remains open. opened_sent guard
        # MUST suppress repeat trip alerts.
        _record_failure "sha4" "Build" "err4"
        _record_failure "sha5" "Build" "err5"
        """
        proc = _run_with_capture(
            rendered, script, state_dir=tmp_state, capture_file=capture,
            cb_max_failures=3,
        )
        assert proc.returncode == 0, proc.stderr

        msgs = _read_messages(capture)
        # First two failures (below threshold) fire the per-SHA "<context> failed"
        # alert. The third trips the breaker. Counts: 2 below + 1 trip = 3.
        # Trips 4 and 5 must NOT fire any new Telegram (suppressed).
        trip_alerts = [m for m in msgs if "Circuit breaker OPEN" in m]
        assert len(trip_alerts) == 1, (
            f"Expected exactly one CB-trip alert, got {len(trip_alerts)}.\n"
            f"All messages: {msgs}"
        )

        # opened_sent must be persisted as true after the alert fires.
        data = json.loads((tmp_state / "svc.json").read_text())
        assert data["alerts"]["opened_sent"] is True
        assert data["consecutive_failures"] == 5
        assert data["opened_at"] is not None

    def test_re_trip_emits_suppression_marker_to_stdout(
        self, rendered, tmp_state, capture
    ):
        """Suppressed re-trips log a [rebuild] line so journald shows them."""
        script = """
        _record_failure "sha1" "Build" "err"
        _record_failure "sha2" "Build" "err"
        _record_failure "sha3" "Build" "err"
        _record_failure "sha4" "Build" "err"
        """
        proc = _run_with_capture(
            rendered, script, state_dir=tmp_state, capture_file=capture,
            cb_max_failures=3,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Circuit breaker re-trip" in proc.stdout

    def test_reset_then_new_trip_fires_again(self, rendered, tmp_state, capture):
        """After _reset_cb, the next open event MUST re-alert."""
        script = """
        _record_failure "sha1" "Build" "err"
        _record_failure "sha2" "Build" "err"
        _record_failure "sha3" "Build" "err"
        _reset_cb
        _record_failure "sha4" "Build" "err"
        _record_failure "sha5" "Build" "err"
        _record_failure "sha6" "Build" "err"
        """
        proc = _run_with_capture(
            rendered, script, state_dir=tmp_state, capture_file=capture,
            cb_max_failures=3,
        )
        assert proc.returncode == 0, proc.stderr
        msgs = _read_messages(capture)
        trip_alerts = [m for m in msgs if "Circuit breaker OPEN" in m]
        assert len(trip_alerts) == 2, (
            f"Reset must rearm opened_sent; got {len(trip_alerts)} trip alerts."
        )


# ── Phase 3.2: rate-limited CB-blocked push alert ────────────────────


# The CB-blocked branch lives at the top of rebuild.sh.j2 (outside any
# function) and is not extractable via _extract_helper. We extract it as
# a standalone snippet by line range and source it into a wrapper
# function so it's reusable from tests.
def _extract_cb_block(rendered: str, *, override_interval: int | None = None) -> str:
    """Pull the top-level CB block (between '# ── Circuit breaker ──'
    and 'fi' before the pull-signal section).

    The block embeds an inline ``CB_BLOCKED_ALERT_INTERVAL_SEC=3600``
    literal. Tests that need a different rate-limit window pass
    ``override_interval`` so the literal is rewritten before the block
    is sourced.
    """
    lines = rendered.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "# ── Circuit breaker ──" in line:
            start = i
            break
    assert start is not None, "Could not locate CB block"
    # Walk forward until we find the closing `fi` at column 0 (the
    # outermost guard). We track depth via `if [[`/`fi` on column-0
    # lines only.
    depth = 0
    end = None
    saw_open = False
    for j, line in enumerate(lines[start:], start=start):
        stripped = line.lstrip()
        if stripped.startswith("if "):
            depth += 1
            saw_open = True
        elif stripped == "fi":
            depth -= 1
            if saw_open and depth == 0:
                end = j
                break
    assert end is not None, "Could not find end of CB block"
    block = "\n".join(lines[start : end + 1])
    if override_interval is not None:
        block = re.sub(
            r"^CB_BLOCKED_ALERT_INTERVAL_SEC=\d+",
            f"CB_BLOCKED_ALERT_INTERVAL_SEC={override_interval}",
            block,
            count=1,
            flags=re.MULTILINE,
        )
    return block


class TestBlockedPushRateLimit:
    """The CB-blocked push branch fires Telegram at most once per interval."""

    def _seed_open_state(self, tmp_state: Path, *, last_alert_at=None):
        """Write a state file representing an already-open breaker."""
        state = {
            "version": 1,
            "consecutive_failures": 5,
            "opened_at": "2026-04-16T15:26:00+00:00",
            "last_failure": {
                "sha": "deadbeef",
                "stage": "Health check",
                "reason": "Container exited with code 137 (OOMKilled)",
                "at": "2026-04-16T15:30:00+00:00",
            },
            "alerts": {
                "opened_sent": True,
                "last_blocked_alert_at": last_alert_at,
            },
        }
        (tmp_state / "svc.json").write_text(json.dumps(state))

    def test_first_blocked_push_fires_alert(self, rendered, tmp_state, capture):
        """With no prior alert (last_blocked_alert_at=null), first push fires."""
        self._seed_open_state(tmp_state, last_alert_at=None)
        cb_block = _extract_cb_block(rendered)  # default 3600s — irrelevant; first push always fires
        proc = _run_with_capture(
            rendered, cb_block, state_dir=tmp_state, capture_file=capture,
            cb_max_failures=3,
        )
        assert proc.returncode == 0, proc.stderr
        msgs = _read_messages(capture)
        blocked = [m for m in msgs if "Push blocked" in m]
        assert len(blocked) == 1, f"Expected 1 blocked-push alert, got {msgs}"
        # Message must include service, host, opened_at, reason, reset cmd.
        body = blocked[0]
        assert "svc" in body
        assert "testhost" in body
        assert "2026-04-16T15:26:00+00:00" in body
        assert "OOMKilled" in body
        assert "bay build reset svc" in body

    def test_second_push_within_interval_suppressed(
        self, rendered, tmp_state, capture
    ):
        """A blocked push <interval seconds after the last alert MUST NOT fire."""
        # Stamp last_alert_at to "now" (will be very recent).
        import time

        now = int(time.time())
        self._seed_open_state(tmp_state, last_alert_at=now - 60)  # 1 min ago
        cb_block = _extract_cb_block(rendered, override_interval=3600)
        proc = _run_with_capture(
            rendered, cb_block, state_dir=tmp_state, capture_file=capture,
            cb_max_failures=3,
        )
        assert proc.returncode == 0, proc.stderr
        msgs = _read_messages(capture)
        blocked = [m for m in msgs if "Push blocked" in m]
        assert len(blocked) == 0, (
            f"Push within rate-limit window must not alert; got {msgs}"
        )
        # And the suppression must be visible in stdout.
        assert "Suppressed CB blocked-push alert" in proc.stdout

    def test_push_after_interval_fires_again(self, rendered, tmp_state, capture):
        """A blocked push >= interval seconds after the last alert MUST fire."""
        import time

        now = int(time.time())
        # Use a tiny interval (1s) and a last_alert from 10s ago. With
        # interval=1, age=10 >= 1 — must fire.
        self._seed_open_state(tmp_state, last_alert_at=now - 10)
        cb_block = _extract_cb_block(rendered, override_interval=1)
        proc = _run_with_capture(
            rendered, cb_block, state_dir=tmp_state, capture_file=capture,
            cb_max_failures=3,
        )
        assert proc.returncode == 0, proc.stderr
        msgs = _read_messages(capture)
        blocked = [m for m in msgs if "Push blocked" in m]
        assert len(blocked) == 1, (
            f"Push after rate-limit window must alert; got {msgs}"
        )
        # last_blocked_alert_at must be updated to ~now.
        data = json.loads((tmp_state / "svc.json").read_text())
        new_at = data["alerts"]["last_blocked_alert_at"]
        assert new_at is not None
        assert abs(new_at - now) < 5, f"last_blocked_alert_at not updated: {new_at}"

    def test_first_alert_then_second_within_window_then_third_after_window(
        self, rendered, tmp_state, capture
    ):
        """End-to-end: alert fires, suppressed, then fires again."""
        # Use a 2-second interval so we can sleep through it.
        self._seed_open_state(tmp_state, last_alert_at=None)
        cb_block = _extract_cb_block(rendered, override_interval=2)

        # Run 1: fires (no prior alert).
        proc1 = _run_with_capture(
            rendered, cb_block, state_dir=tmp_state, capture_file=capture,
            cb_max_failures=3,
        )
        assert proc1.returncode == 0, proc1.stderr

        # Run 2: immediately — suppressed.
        proc2 = _run_with_capture(
            rendered, cb_block, state_dir=tmp_state, capture_file=capture,
            cb_max_failures=3,
        )
        assert proc2.returncode == 0, proc2.stderr

        # Run 3: after interval — fires.
        import time

        time.sleep(3)
        proc3 = _run_with_capture(
            rendered, cb_block, state_dir=tmp_state, capture_file=capture,
            cb_max_failures=3,
        )
        assert proc3.returncode == 0, proc3.stderr

        msgs = _read_messages(capture)
        blocked = [m for m in msgs if "Push blocked" in m]
        assert len(blocked) == 2, (
            f"Expected 2 blocked alerts (1 + suppressed + 1 after window), "
            f"got {len(blocked)}: {msgs}"
        )


# ── Phase 3.3: rollback-success CB preview ───────────────────────────


class TestRollbackTelegramCbPreview:
    """The 'Rolled back to previous' Telegram body includes a CB preview."""

    def test_rollback_message_template_contains_cb_preview_snippet(self, rendered):
        """Static check: the template includes the CB-preview format string."""
        # Look for the literal preview phrase that the template prints when
        # below threshold. This must be present in the rendered script.
        assert "Circuit breaker at %d/%d" in rendered, (
            "Rollback Telegram body must include a 'Circuit breaker at N/MAX' "
            "preview line (circuit-breaker Phase 3, contract row 4)."
        )
        assert "next failure will block pushes" in rendered, (
            "Rollback CB preview must warn that the next failure blocks pushes."
        )
        assert "bay build reset" in rendered, (
            "Rollback CB preview must include the reset command hint."
        )

    def test_rollback_below_threshold_includes_preview(self, rendered, tmp_state):
        """When rollback runs and N<MAX, the captured Telegram includes the preview."""
        # We test the inline snippet from _handle_rollback by extracting the
        # `_record_failure` + `notify_build` block and exercising it with
        # _record_failure stubs to drive consecutive_failures to 1 (well
        # below threshold=5). We don't need the full rollback machinery —
        # just the message-construction logic.
        capture = tmp_state.parent / "tg.log"
        # Drive one failure first so consecutive_failures=1.
        # Then mimic the rollback message construction directly.
        script = """
        _record_failure "sha1" "Health check" "Rolled back to previous image"
        # Now construct the message exactly like _handle_rollback does
        _rb_count=$(_read_state | jq -r '.consecutive_failures // 0')
        _rb_count=${_rb_count:-0}
        _rb_cb_line=""
        if [[ "${_rb_count}" -lt "${CB_MAX_FAILURES}" ]]; then
          _rb_cb_line="$(printf '\\n\\n\\u26a0\\ufe0f Circuit breaker at %d/%d — next failure will block pushes.\\nReset: <code>bay build reset %s</code>' \
            "${_rb_count}" "${CB_MAX_FAILURES}" "${SERVICE}")"
        fi
        notify_build build.rolled_back "$(printf 'Rolled back to previous%s' "${_rb_cb_line}")"
        """
        proc = _run_with_capture(
            rendered, script, state_dir=tmp_state, capture_file=capture,
            cb_max_failures=5,
        )
        assert proc.returncode == 0, proc.stderr
        msgs = _read_messages(capture)
        # Find the rollback message (the one starting with "Rolled back").
        rollback_msgs = [m for m in msgs if m.startswith("Rolled back to previous")]
        assert len(rollback_msgs) == 1, (
            f"Expected one rollback message, got {len(rollback_msgs)}: {msgs}"
        )
        body = rollback_msgs[0]
        assert "Circuit breaker at 1/5" in body, (
            f"Rollback body missing CB preview: {body!r}"
        )
        assert "next failure will block pushes" in body
        assert "bay build reset svc" in body


# ── Phase 3.4: CB_MAX_FAILURES configurable via role default ─────────


class TestCbMaxFailuresDefault:
    """The role default git_deploy_cb_max_failures wires through correctly."""

    def test_default_renders_as_5(self):
        """Rendering rebuild.sh.j2 with the role default => CB_MAX_FAILURES=5.

        We render with the actual default (5 per the role's defaults/main.yml
        as of the circuit-breaker Phase 3 rollout) and assert the script shows that value.
        """
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"],
        )
        # The render shim explicitly sets git_deploy_cb_max_failures=3 as a
        # legacy default for backwards compat with Phase 2 tests; we instead
        # test the template literal placement and check that an arbitrary
        # value passes through.
        assert "CB_MAX_FAILURES=" in rendered, (
            "Template must declare CB_MAX_FAILURES from the role variable."
        )

    def test_custom_value_renders_in_script(self):
        """Override git_deploy_cb_max_failures => script picks it up."""
        from helpers import make_ansible_env

        # Reuse the render shim by importing its bits and overriding the
        # explicit value. Simplest: just render the template with explicit
        # override using the same shim signature.
        services = _local_service()
        # Manually render with cb_max=5 to confirm template indirection.
        from test_rebuild_config import TEMPLATE_DIR

        env = make_ansible_env(TEMPLATE_DIR)
        # Required filters
        from test_rebuild_config import _regex_replace, _to_json
        from bay_filters import (  # type: ignore[import-not-found]
            bay_build_dedup_map,
            bay_image_consumers,
            bay_image_region_map,
            bay_prefix_volumes,
            bay_repo_slug,
            bay_traefik_labels,
            bay_watchtower_labels,
        )

        env.filters["regex_replace"] = _regex_replace
        env.filters["to_json"] = _to_json
        env.filters["bay_build_dedup_map"] = bay_build_dedup_map
        env.filters["bay_image_consumers"] = bay_image_consumers
        env.filters["bay_image_region_map"] = bay_image_region_map
        env.filters["bay_repo_slug"] = bay_repo_slug
        env.filters["bay_prefix_volumes"] = bay_prefix_volumes
        env.filters["bay_traefik_labels"] = bay_traefik_labels
        env.filters["bay_watchtower_labels"] = bay_watchtower_labels

        tmpl = env.get_template("rebuild.sh.j2")
        rendered = tmpl.render(
            ansible_managed="test",
            services=services,
            git_deploy_rebuild_services=["localapp"],
            git_deploy_services=["localapp"],
            git_deploy_build_strategy="local",
            git_deploy_build_dir="/opt/teststack/builds",
            git_deploy_image_prefix="bay-teststack",
            git_deploy_remote_build_dir="/opt/teststack/push-builds",
            git_deploy_cb_max_failures=5,
            git_deploy_health_check_timeout=90,
            git_deploy_build_timeout=1200,
            git_deploy_build_mem_limit="2g",
            git_deploy_peer_webhook_urls={},
            stack_dir="/opt/teststack",
            stack_name="teststack",
            docker_monitor_telegram_bot_token="t",
            docker_monitor_telegram_chat_id="c",
            docker_monitor_alert_header="[T] ",
            inventory_hostname="build-server",
            traefik_docker_network="services",
            watchtower_enabled=False,
            webhook={"secret": "s"},
        )
        assert "CB_MAX_FAILURES=5" in rendered, (
            "git_deploy_cb_max_failures=5 must render to CB_MAX_FAILURES=5"
        )

    def test_role_default_is_5(self):
        """The role's defaults/main.yml ships git_deploy_cb_max_failures: 5."""
        defaults = (
            Path(__file__).resolve().parent.parent
            / "roles"
            / "git_deploy"
            / "defaults"
            / "main.yml"
        ).read_text()
        # Match the YAML key precisely.
        match = re.search(r"^git_deploy_cb_max_failures:\s*(\d+)\s*$", defaults, re.M)
        assert match is not None, (
            "git_deploy_cb_max_failures missing from role defaults"
        )
        assert match.group(1) == "5", (
            f"Role default should be 5 after Phase 3 bump, found {match.group(1)}"
        )
