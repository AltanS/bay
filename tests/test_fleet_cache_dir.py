"""Caches of fleet state live inside the fleet, never in the shared framework checkout.

The rig-state cache and the validate probe cache describe one fleet's boxes.
Two fleets on one machine must never read each other's cache.
"""

from __future__ import annotations

from pathlib import Path

from bay_cli.commands.ops import _read_rig_cache, _record_rig_matched, _write_rig_cache
from bay_cli.commands.validate import ProbeCache
from bay_cli.context import CACHE_DIRNAME, Context


def _two_fleets(tmp_path: Path) -> tuple[Context, Context]:
    framework = tmp_path / "framework"
    framework.mkdir()
    a, b = tmp_path / "fleet-a", tmp_path / "fleet-b"
    a.mkdir()
    b.mkdir()
    return Context.for_fleet_root(a, framework), Context.for_fleet_root(b, framework)


def test_two_contexts_get_different_cache_paths(tmp_path: Path) -> None:
    a, b = _two_fleets(tmp_path)
    assert a.cache_dir != b.cache_dir
    assert a.cache_dir == a.fleet_root / CACHE_DIRNAME == a.fleet_root / ".bay-cache"
    assert b.cache_dir == b.fleet_root / ".bay-cache"
    assert a.framework_root == b.framework_root
    assert a.framework_root not in a.cache_dir.parents


def test_rig_state_cache_of_one_fleet_is_a_miss_for_another(tmp_path: Path) -> None:
    a, b = _two_fleets(tmp_path)
    _write_rig_cache(a.cache_dir, False, version="v", consumer_ref="r")
    assert _read_rig_cache(a.cache_dir, version="v", consumer_ref="r") is False
    assert _read_rig_cache(b.cache_dir, version="v", consumer_ref="r") is None
    assert (a.cache_dir / ".rig-state-cache").is_file()
    assert not (a.framework_root / ".rig-state-cache").exists()


def test_deploy_records_rig_state_in_the_fleet(tmp_path: Path) -> None:
    a, b = _two_fleets(tmp_path)
    _record_rig_matched(a.framework_root, a.fleet_root)
    assert (a.cache_dir / ".rig-state-cache").is_file()
    assert not b.cache_dir.exists()
    assert not (a.framework_root / ".rig-state-cache").exists()


def test_probe_cache_of_one_fleet_is_a_miss_for_another(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("BAY_PROBE_CACHE_DIR", raising=False)
    a, b = _two_fleets(tmp_path)
    first = ProbeCache(a.cache_dir)
    first.put(ProbeCache.image_key("nginx", "latest"), "ok", "image reference resolved")
    first.flush()
    assert (a.cache_dir / ".validate-probe-cache").is_file()
    assert ProbeCache(a.cache_dir).get("image:nginx:latest") == "image reference resolved"
    assert ProbeCache(b.cache_dir).get("image:nginx:latest") is None
    assert not (a.framework_root / ".validate-probe-cache").exists()
