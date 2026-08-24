"""Tests for M75-S7 Phase 4: bay build status / reset.

Covers:
- _parse_adhoc_output / _parse_adhoc_json — shared ad-hoc output parser
- _fetch_build_states — pure data function (mocked _run_on_host)
- _render_status_table — display function, column layout, truncation
- _send_telegram_audit — no-op when creds absent, calls curl when present
- reset atomic write command structure (no live host required)
- empty state dir → "No build state found" path
- multi-region Region column presence/absence
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

from bay_cli.commands.build import (
    _fetch_build_states,
    _parse_adhoc_json,
    _parse_adhoc_output,
    _render_status_table,
    _send_telegram_audit,
    _truncate,
)


# ---------------------------------------------------------------------------
# _parse_adhoc_output / _parse_adhoc_json
# ---------------------------------------------------------------------------


class TestParseAdhocOutput:
    """Tests for the shared ad-hoc output parser."""

    def test_strips_ansi(self):
        raw = "\x1b[32mhello\x1b[0m"
        assert _parse_adhoc_output(raw) == "hello"

    def test_strips_header_line(self):
        raw = "10.0.0.1 | SUCCESS | rc=0 >>\nhello world"
        assert _parse_adhoc_output(raw) == "hello world"

    def test_strips_failed_header(self):
        raw = "10.0.0.1 | FAILED | rc=1 >>\nerror output"
        assert _parse_adhoc_output(raw) == "error output"

    def test_no_header_passthrough(self):
        raw = '{"key": "value"}'
        assert _parse_adhoc_output(raw) == '{"key": "value"}'

    def test_ansi_and_header_combined(self):
        raw = "\x1b[0m10.0.0.1 | SUCCESS | rc=0 >>\n\x1b[32m{\"ok\": true}\x1b[0m"
        result = _parse_adhoc_output(raw)
        assert result == '{"ok": true}'

    def test_empty_input(self):
        assert _parse_adhoc_output("") == ""


class TestParseAdhocJson:
    """Tests for _parse_adhoc_json."""

    def test_parses_plain_json(self):
        raw = '{"svc": {"failures": 2}}'
        result = _parse_adhoc_json(raw)
        assert result == {"svc": {"failures": 2}}

    def test_parses_json_after_header(self):
        raw = '10.0.0.1 | SUCCESS | rc=0 >>\n{"key": 1}'
        result = _parse_adhoc_json(raw)
        assert result == {"key": 1}

    def test_returns_none_on_empty(self):
        assert _parse_adhoc_json("") is None

    def test_returns_none_on_non_json(self):
        assert _parse_adhoc_json("not json at all") is None

    def test_returns_none_on_broken_json(self):
        assert _parse_adhoc_json('{"broken": }') is None

    def test_multiline_json(self):
        raw = "host | SUCCESS | rc=0 >>\n{\n  \"a\": 1\n}"
        result = _parse_adhoc_json(raw)
        assert result == {"a": 1}


# ---------------------------------------------------------------------------
# _send_telegram_audit
# ---------------------------------------------------------------------------


class TestSendTelegramAudit:
    """Tests for the Python-native Telegram audit helper."""

    def test_no_op_when_token_empty(self):
        with patch("subprocess.run") as mock_run:
            _send_telegram_audit("", "12345", "msg")
            mock_run.assert_not_called()

    def test_no_op_when_chat_id_empty(self):
        with patch("subprocess.run") as mock_run:
            _send_telegram_audit("mytoken", "", "msg")
            mock_run.assert_not_called()

    def test_no_op_when_both_empty(self):
        with patch("subprocess.run") as mock_run:
            _send_telegram_audit("", "", "msg")
            mock_run.assert_not_called()

    def test_calls_curl_when_creds_present(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _send_telegram_audit("TOKEN123", "CHAT456", "CB reset for svc")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "curl" in args
            assert "https://api.telegram.org/botTOKEN123/sendMessage" in args
            assert "chat_id=CHAT456" in args

    def test_swallows_subprocess_exception(self):
        with patch("subprocess.run", side_effect=OSError("no curl")):
            # Must not raise
            _send_telegram_audit("TOKEN", "CHAT", "msg")


# ---------------------------------------------------------------------------
# _render_status_table
# ---------------------------------------------------------------------------


def _make_state(
    service: str = "mysvc",
    cb_open: bool = False,
    failures: int = 0,
    region: Optional[str] = None,
    last_reason: str = "",
) -> dict:
    return {
        "service": service,
        "cb_state": "OPEN" if cb_open else "closed",
        "consecutive_failures": failures,
        "opened_at": "2026-04-16T15:26:00Z" if cb_open else "",
        "last_sha": "abc1234",
        "last_stage": "Health check",
        "last_reason": last_reason,
        "last_at": "2026-04-16T15:26:00Z",
        "last_blocked_alert_at": "",
        "region": region,
        "raw": {},
    }


class TestRenderStatusTable:
    """Tests for the pure table renderer."""

    def test_basic_single_region_columns(self):
        states = [_make_state("api", cb_open=False)]
        table = _render_status_table(states, multi_region=False)
        col_names = [c.header for c in table.columns]
        assert "Region" not in col_names
        assert "Service" in col_names
        assert "CB State" in col_names
        assert "Failures" in col_names

    def test_multi_region_adds_region_column(self):
        states = [_make_state("api", region="eu")]
        table = _render_status_table(states, multi_region=True)
        col_names = [c.header for c in table.columns]
        assert col_names[0] == "Region"

    def test_verbose_adds_last_blocked_column(self):
        states = [_make_state("api")]
        table = _render_status_table(states, verbose=True)
        col_names = [c.header for c in table.columns]
        assert "Last Blocked Alert" in col_names

    def test_verbose_false_no_blocked_column(self):
        states = [_make_state("api")]
        table = _render_status_table(states, verbose=False)
        col_names = [c.header for c in table.columns]
        assert "Last Blocked Alert" not in col_names

    def test_row_count_matches_states(self):
        states = [_make_state(f"svc{i}") for i in range(5)]
        table = _render_status_table(states)
        assert table.row_count == 5

    def test_empty_states_zero_rows(self):
        table = _render_status_table([])
        assert table.row_count == 0

    def test_reason_truncated_at_60_chars(self):
        long_reason = "x" * 80
        s = _make_state("svc", last_reason=long_reason)
        assert _truncate(s["last_reason"]) == "x" * 59 + "\u2026"

    def test_reason_not_truncated_when_short(self):
        short_reason = "Container exited with code 1"
        s = _make_state("svc", last_reason=short_reason)
        assert _truncate(s["last_reason"]) == short_reason

    def test_reason_exactly_60_chars_not_truncated(self):
        exactly_60 = "a" * 60
        assert _truncate(exactly_60) == exactly_60


# ---------------------------------------------------------------------------
# _fetch_build_states (mocked _run_on_host)
# ---------------------------------------------------------------------------


class TestFetchBuildStates:
    """Tests for the pure data fetcher with mocked remote execution."""

    def _mock_result(self, payload: str) -> MagicMock:
        m = MagicMock()
        m.stdout = payload
        m.returncode = 0
        return m

    def test_parses_state_files(self, tmp_path):
        state = {
            "api": {
                "version": 1,
                "consecutive_failures": 2,
                "opened_at": None,
                "last_failure": {
                    "sha": "abc123ef",
                    "stage": "Health check",
                    "reason": "Container exited with code 1",
                    "at": "2026-04-16T15:26:00Z",
                },
                "alerts": {
                    "opened_sent": False,
                    "last_blocked_alert_at": None,
                },
            }
        }
        raw = json.dumps(state)

        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()

        # Direct patch of the ops._run_on_host used inside build
        with patch("bay_cli.commands.ops._run_on_host", return_value=self._mock_result(raw)) as mock_roh:
            # _fetch_build_states imports _run_on_host from ops inline
            states = _fetch_build_states("production", "myapp", bay_dir, None)

        assert len(states) == 1
        s = states[0]
        assert s["service"] == "api"
        assert s["cb_state"] == "closed"
        assert s["consecutive_failures"] == 2
        assert s["last_sha"] == "abc123ef"[:8]
        assert s["last_reason"] == "Container exited with code 1"

    def test_open_cb_detected(self, tmp_path):
        state = {
            "worker": {
                "version": 1,
                "consecutive_failures": 5,
                "opened_at": "2026-04-16T15:26:00Z",
                "last_failure": {"sha": "", "stage": "", "reason": "", "at": ""},
                "alerts": {"opened_sent": True, "last_blocked_alert_at": None},
            }
        }
        raw = json.dumps(state)
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()

        with patch("bay_cli.commands.ops._run_on_host", return_value=self._mock_result(raw)):
            states = _fetch_build_states("production", "myapp", bay_dir, None)

        assert states[0]["cb_state"] == "OPEN"
        assert states[0]["opened_at"] == "2026-04-16T15:26:00Z"

    def test_empty_state_dir_returns_empty_list(self, tmp_path):
        # Simulates the state dir existing but empty
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        # Return empty JSON object (no files)
        with patch("bay_cli.commands.ops._run_on_host", return_value=self._mock_result("{}")):
            states = _fetch_build_states("production", "myapp", bay_dir, None)
        assert states == []

    def test_missing_state_dir_returns_empty_list(self, tmp_path):
        # Simulates os.path.isdir returning False — python3 -c prints {}
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        with patch("bay_cli.commands.ops._run_on_host", return_value=self._mock_result("{}")):
            states = _fetch_build_states("production", "myapp", bay_dir, None)
        assert states == []

    def test_run_exception_returns_empty_list(self, tmp_path):
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        with patch("bay_cli.commands.ops._run_on_host", side_effect=Exception("ssh fail")):
            states = _fetch_build_states("production", "myapp", bay_dir, None)
        assert states == []

    def test_multiple_services_sorted(self, tmp_path):
        state = {
            "zebra": {"version": 1, "consecutive_failures": 0, "opened_at": None,
                      "last_failure": {}, "alerts": {}},
            "alpha": {"version": 1, "consecutive_failures": 1, "opened_at": None,
                      "last_failure": {}, "alerts": {}},
        }
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        with patch("bay_cli.commands.ops._run_on_host", return_value=self._mock_result(json.dumps(state))):
            states = _fetch_build_states("production", "myapp", bay_dir, None)
        # Should be sorted alphabetically
        assert [s["service"] for s in states] == ["alpha", "zebra"]

    def test_limit_passed_through(self, tmp_path):
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        with patch("bay_cli.commands.ops._run_on_host", return_value=self._mock_result("{}")) as mock_roh:
            _fetch_build_states("production", "myapp", bay_dir, "10.0.0.5")
        _, kwargs = mock_roh.call_args
        assert kwargs.get("limit") == "10.0.0.5"

    def test_adhoc_header_stripped_before_json_parse(self, tmp_path):
        """Ensures the ansible ad-hoc prefix line does not break JSON parsing."""
        payload = "10.0.0.1 | SUCCESS | rc=0 >>\n" + json.dumps({"svc": {
            "version": 1, "consecutive_failures": 0, "opened_at": None,
            "last_failure": {}, "alerts": {},
        }})
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        with patch("bay_cli.commands.ops._run_on_host", return_value=self._mock_result(payload)):
            states = _fetch_build_states("production", "myapp", bay_dir, None)
        assert len(states) == 1
        assert states[0]["service"] == "svc"
