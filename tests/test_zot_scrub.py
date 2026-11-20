"""The zot scrub extension is a switch, off by default on the s3 driver.

Scrub re-reads every blob of every tag. On the s3 driver that is a full
download from the bucket on each run (paid data-out). So the default is:
on for filesystem, off for s3. `zot_scrub_enabled` overrides it and
`zot_scrub_interval` sets the interval.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from helpers import make_ansible_env

_REPO_ROOT = Path(__file__).parent.parent
_ZOT_DEFAULTS = _REPO_ROOT / "roles" / "zot" / "defaults" / "main.yml"
_ZOT_CONFIG_TPL = _REPO_ROOT / "roles" / "zot" / "templates"


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "yes", "1", "on"}
    return bool(value)


def _render_config(**overrides) -> str:
    """Render config.json.j2 with the real role defaults.

    The default of `zot_scrub_enabled` is a Jinja expression on
    `zot_storage_driver`. Resolve it the way Ansible does, so the test
    covers the shipped default and not a copy of it.
    """
    env = make_ansible_env(_ZOT_CONFIG_TPL)
    env.filters["to_json"] = json.dumps
    env.filters["bool"] = _bool
    base = yaml.safe_load(_ZOT_DEFAULTS.read_text())
    base.update(
        {
            "zot_storage_root": "/var/lib/registry",
            "zot_domain": "registry.example.com",
            "zot_s3_bucket": "bucket",
            "zot_s3_region": "eu-central-1",
        }
    )
    base.update(overrides)
    for key in ("zot_scrub_enabled",):
        value = base[key]
        if isinstance(value, str) and "{{" in value:
            base[key] = env.from_string(value).render(**base)
    return env.get_template("config.json.j2").render(**base)


def _extensions(**overrides) -> dict:
    return json.loads(_render_config(**overrides))["extensions"]


def test_filesystem_default_has_scrub_every_24h() -> None:
    ext = _extensions(zot_storage_driver="filesystem")
    assert ext["scrub"] == {"enable": True, "interval": "24h"}
    assert ext["search"] == {"enable": True}


def test_s3_default_has_no_scrub() -> None:
    ext = _extensions(zot_storage_driver="s3")
    assert "scrub" not in ext
    assert ext["search"] == {"enable": True}


def test_s3_with_scrub_enabled_has_scrub() -> None:
    ext = _extensions(zot_storage_driver="s3", zot_scrub_enabled=True)
    assert ext["scrub"] == {"enable": True, "interval": "24h"}


def test_filesystem_with_scrub_disabled_has_no_scrub() -> None:
    ext = _extensions(zot_storage_driver="filesystem", zot_scrub_enabled=False)
    assert "scrub" not in ext


@pytest.mark.parametrize("driver", ["filesystem", "s3"])
def test_custom_interval_is_rendered(driver: str) -> None:
    ext = _extensions(
        zot_storage_driver=driver, zot_scrub_enabled=True, zot_scrub_interval="168h"
    )
    assert ext["scrub"]["interval"] == "168h"


@pytest.mark.parametrize("driver", ["filesystem", "s3"])
@pytest.mark.parametrize("enabled", [True, False, "true", "false"])
def test_output_is_valid_json_in_every_case(driver: str, enabled) -> None:
    json.loads(_render_config(zot_storage_driver=driver, zot_scrub_enabled=enabled))
