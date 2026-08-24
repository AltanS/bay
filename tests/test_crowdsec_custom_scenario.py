"""Render the crowdsec custom-scenario.yaml.j2 template with single-line and
multi-line filters and assert the result is valid YAML with the filter content
preserved.

Regression test for bay#8: a multi-line filter passed via a YAML literal
block scalar (`|`) used to render as

    filter: line1 or
    line2 or
    line3

which CrowdSec rejected with `yaml: line N: could not find expected ':'`.
The fix renders `filter:` as its own block scalar so multi-line filters work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = (
    Path(__file__).parent.parent / "roles" / "crowdsec" / "templates" / "scenarios"
)
TEMPLATE_NAME = "custom-scenario.yaml.j2"


@pytest.fixture(scope="module")
def jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        keep_trailing_newline=True,
    )


def _render(jinja_env: Environment, item: dict) -> str:
    return jinja_env.get_template(TEMPLATE_NAME).render(item=item)


def test_single_line_filter_renders_valid_yaml(jinja_env: Environment) -> None:
    item = {
        "name": "wp-admin-exploit",
        "description": "Detect WP admin exploitation",
        "type": "leaky",
        "filter": "evt.Meta.http_path contains '/wp-login.php'",
        "groupby": "evt.Meta.source_ip",
        "capacity": 1,
        "leakspeed": "24h",
        "blackhole": "24h",
        "labels": {"service": "wordpress", "type": "exploit", "remediation": True},
    }
    rendered = _render(jinja_env, item)
    parsed = yaml.safe_load(rendered)
    assert parsed["name"] == "custom/wp-admin-exploit"
    assert parsed["filter"].strip() == item["filter"]
    assert parsed["capacity"] == 1
    assert parsed["leakspeed"] == "24h"


def test_multiline_filter_renders_valid_yaml(jinja_env: Environment) -> None:
    """The bug: an unquoted multi-line filter renders as broken YAML with
    second/subsequent lines parsed as bare keys lacking colons."""
    multiline = (
        "evt.Meta.http_path == '/admin/config.php' or\n"
        "evt.Meta.http_path == '/wp-login.php' or\n"
        "endsWith(evt.Meta.http_path, '.env')\n"
    )
    item = {
        "name": "wp-decoy-probe",
        "description": "Detect probing of common WP-decoy paths",
        "type": "leaky",
        "filter": multiline,
        "groupby": "evt.Meta.source_ip",
        "capacity": 3,
        "leakspeed": "1h",
        "blackhole": "5m",
        "labels": {"service": "http", "type": "scan", "remediation": True},
    }
    rendered = _render(jinja_env, item)
    parsed = yaml.safe_load(rendered)
    assert parsed["name"] == "custom/wp-decoy-probe"
    # Block scalar preserves all three OR clauses; check each is present.
    filter_text = parsed["filter"]
    assert "/admin/config.php" in filter_text
    assert "/wp-login.php" in filter_text
    assert "endsWith(evt.Meta.http_path, '.env')" in filter_text
    # And no Jinja-introduced corruption (no doubled spaces, no stray quotes).
    assert "  evt." not in filter_text  # no double-indent leaking into content


def test_trigger_type_omits_capacity_block(jinja_env: Environment) -> None:
    """Trigger scenarios are stateless; capacity/leakspeed/blackhole shouldn't
    be emitted."""
    item = {
        "name": "trigger-example",
        "description": "Trigger scenario",
        "type": "trigger",
        "filter": "evt.Meta.http_path == '/sensitive'",
        "groupby": "evt.Meta.source_ip",
        "labels": {"service": "http", "type": "exploit", "remediation": True},
    }
    rendered = _render(jinja_env, item)
    parsed = yaml.safe_load(rendered)
    assert parsed["type"] == "trigger"
    assert "capacity" not in parsed
    assert "leakspeed" not in parsed
    assert "blackhole" not in parsed
