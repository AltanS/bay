"""Unit tests for the Zot tag-retention policy.

Prior policy kept every tag pushed OR pulled within 720h in addition to
the N-most-recent rules, so a busy repo could accumulate dozens of tags
(one repo hit 91). This bounds retention to a fixed count of most
recently PUSHED tags, a fixed count of most recently PULLED tags (so the
image a host is currently running survives even if older than the last
N pushes), plus an always-keep tag pattern list (`latest` by default).
The time window (`zot_retention_keep_within`) is now optional and empty
by default — setting it re-introduces the unbounded-count tradeoff, so
it must be opt-in.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_ansible_env

_REPO_ROOT = Path(__file__).parent.parent
_ZOT_CONFIG_TPL = _REPO_ROOT / "roles" / "zot" / "templates"


def _to_json(value):
    return json.dumps(value)


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "yes", "1", "on"}
    return bool(value)


def _render_config(**overrides) -> str:
    env = make_ansible_env(_ZOT_CONFIG_TPL)
    env.filters["to_json"] = _to_json
    env.filters["bool"] = _bool
    base = {
        "zot_storage_root": "/var/lib/registry",
        "zot_gc_enabled": True,
        "zot_gc_delay": "4h",
        "zot_gc_interval": "24h",
        "zot_untagged_retention_delay": "72h",
        "zot_retention_keep_count": 10,
        "zot_retention_keep_pulled_count": 3,
        "zot_retention_keep_within": "",
        "zot_retention_always_keep": ["^latest$"],
        "zot_storage_driver": "filesystem",
        "zot_s3_bucket": "",
        "zot_s3_region": "",
        "zot_s3_rootdirectory": "/zot",
        "zot_s3_secure": True,
        "zot_s3_force_path_style": False,
        "zot_remote_cache": False,
        "zot_cache_driver": "boltdb",
        "zot_cache_dir": "/var/lib/registry",
        "zot_port": 5000,
        "zot_domain": "registry.example.com",
    }
    base.update(overrides)
    return env.get_template("config.json.j2").render(**base)


def _keep_tags(rendered: str) -> list[dict]:
    parsed = json.loads(rendered)
    return parsed["storage"]["retention"]["policies"][0]["keepTags"]


def test_defaults_render_valid_json_with_bounded_policy() -> None:
    rendered = _render_config()
    keep_tags = _keep_tags(rendered)

    pattern_rules = [r for r in keep_tags if "patterns" in r]
    assert len(pattern_rules) == 1
    assert pattern_rules[0]["patterns"] == ["^latest$"]

    count_rules = [r for r in keep_tags if "mostRecentlyPushedCount" in r]
    assert len(count_rules) == 1
    rule = count_rules[0]
    assert rule["mostRecentlyPushedCount"] == 10
    assert rule["mostRecentlyPulledCount"] == 3
    assert "pulledWithin" not in rule
    assert "pushedWithin" not in rule


def test_keep_within_adds_window_keys() -> None:
    rendered = _render_config(zot_retention_keep_within="168h")
    keep_tags = _keep_tags(rendered)
    count_rules = [r for r in keep_tags if "mostRecentlyPushedCount" in r]
    assert len(count_rules) == 1
    rule = count_rules[0]
    assert rule["pulledWithin"] == "168h"
    assert rule["pushedWithin"] == "168h"


def test_empty_always_keep_omits_pattern_rule() -> None:
    rendered = _render_config(zot_retention_always_keep=[])
    keep_tags = _keep_tags(rendered)
    pattern_rules = [r for r in keep_tags if "patterns" in r]
    assert pattern_rules == []
    count_rules = [r for r in keep_tags if "mostRecentlyPushedCount" in r]
    assert len(count_rules) == 1
