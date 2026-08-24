"""Rig-state cache correctness tests (M85-S9).

The cache must be a HIT only when it was computed for the same framework
version and consumer_ref; any input change (or staleness) is a miss, so a
genuinely-needed rig is never skipped because of a stale cached `false`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bay_cli.commands.ops import _read_rig_cache, _write_rig_cache


class TestRigCacheKeying:
    def test_hit_when_inputs_match(self, tmp_path: Path) -> None:
        _write_rig_cache(tmp_path, False, version="0.95.4", consumer_ref="abc")
        assert _read_rig_cache(tmp_path, version="0.95.4", consumer_ref="abc") is False

    def test_true_value_round_trips(self, tmp_path: Path) -> None:
        _write_rig_cache(tmp_path, True, version="v", consumer_ref="r")
        assert _read_rig_cache(tmp_path, version="v", consumer_ref="r") is True

    def test_miss_on_version_change(self, tmp_path: Path) -> None:
        _write_rig_cache(tmp_path, False, version="0.95.4", consumer_ref="abc")
        # framework bump must not serve a stale "no rig needed"
        assert _read_rig_cache(tmp_path, version="0.95.5", consumer_ref="abc") is None

    def test_miss_on_consumer_ref_change(self, tmp_path: Path) -> None:
        _write_rig_cache(tmp_path, False, version="0.95.4", consumer_ref="abc")
        # consumer config change must force a fresh check (the correctness bug)
        assert _read_rig_cache(tmp_path, version="0.95.4", consumer_ref="def") is None

    def test_miss_when_stale(self, tmp_path: Path) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        (tmp_path / ".rig-state-cache").write_text(
            json.dumps({"rig_needed": False, "version": "v", "consumer_ref": "r", "checked_at": old})
        )
        assert _read_rig_cache(tmp_path, version="v", consumer_ref="r") is None

    def test_miss_when_absent(self, tmp_path: Path) -> None:
        assert _read_rig_cache(tmp_path, version="v", consumer_ref="r") is None

    def test_legacy_cache_without_keys_is_miss(self, tmp_path: Path) -> None:
        # a pre-S9 cache file (no version/consumer_ref) must not be trusted
        (tmp_path / ".rig-state-cache").write_text(
            json.dumps({"rig_needed": False, "checked_at": datetime.now(timezone.utc).isoformat()})
        )
        assert _read_rig_cache(tmp_path, version="v", consumer_ref="r") is None
