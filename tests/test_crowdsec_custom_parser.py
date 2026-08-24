"""Render the crowdsec custom-parser.yaml.j2 template and assert the result is
valid YAML with the body preserved and a single injected `name`.

Covers bay#23: a consumer ships a local parser purely via group_vars
(name + stage + raw body). The role injects `name: custom/<name>` and renders
the body verbatim, so an app log line (e.g. a bot-verify SPOOFED_GOOGLEBOT
marker) becomes a parsed event with evt.Meta.source_ip that a trigger scenario
can ban on — without re-deriving the verdict from raw UA strings.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = (
    Path(__file__).parent.parent / "roles" / "crowdsec" / "templates" / "parsers"
)
TEMPLATE_NAME = "custom-parser.yaml.j2"

# The canonical example from the role defaults / bay#23.
BOT_VERIFY_BODY = (
    "filter: \"evt.Line.Raw contains 'SPOOFED_GOOGLEBOT'\"\n"
    "onsuccess: next_stage\n"
    "nodes:\n"
    "  - grok:\n"
    "      pattern: 'ip=%{IP:spoof_ip}'\n"
    "      apply_on: message\n"
    "    statics:\n"
    "      - meta: source_ip\n"
    "        expression: evt.Parsed.spoof_ip\n"
    "      - meta: log_type\n"
    "        value: bot_verify_spoof\n"
)


@pytest.fixture(scope="module")
def jinja_env() -> Environment:
    # Match Ansible's templating defaults so the rendered file is faithful.
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def _render(jinja_env: Environment, item: dict) -> str:
    return jinja_env.get_template(TEMPLATE_NAME).render(item=item)


def test_parser_renders_valid_yaml(jinja_env: Environment) -> None:
    item = {"name": "bot-verify-spoof", "stage": "s01-parse", "body": BOT_VERIFY_BODY}
    rendered = _render(jinja_env, item)
    parsed = yaml.safe_load(rendered)

    assert parsed["name"] == "custom/bot-verify-spoof"
    assert "SPOOFED_GOOGLEBOT" in parsed["filter"]
    assert parsed["onsuccess"] == "next_stage"
    # The grok node maps the captured IP onto evt.Meta.source_ip — the contract
    # a `type: trigger` scenario groups by.
    statics = parsed["nodes"][0]["statics"]
    source_ip = next(s for s in statics if s["meta"] == "source_ip")
    assert source_ip["expression"] == "evt.Parsed.spoof_ip"


def test_parser_injects_name_exactly_once(jinja_env: Environment) -> None:
    """The role owns `name:` — the body must not repeat it (would be a YAML
    duplicate key). Assert exactly one top-level name line is emitted."""
    item = {"name": "bot-verify-spoof", "body": BOT_VERIFY_BODY}
    rendered = _render(jinja_env, item)
    name_lines = [ln for ln in rendered.splitlines() if ln.startswith("name:")]
    assert name_lines == ["name: custom/bot-verify-spoof"]


def test_parser_description_optional(jinja_env: Environment) -> None:
    without = _render(jinja_env, {"name": "p", "body": "filter: \"true\"\n"})
    assert "description:" not in without

    with_desc = _render(
        jinja_env,
        {"name": "p", "description": "marks spoofers", "body": "filter: \"true\"\n"},
    )
    parsed = yaml.safe_load(with_desc)
    assert parsed["description"] == "marks spoofers"
    assert parsed["filter"] == "true"


def test_multiline_body_preserved(jinja_env: Environment) -> None:
    """A multi-statics body must round-trip — no stray indentation injected."""
    item = {"name": "bot-verify-spoof", "stage": "s01-parse", "body": BOT_VERIFY_BODY}
    rendered = _render(jinja_env, item)
    parsed = yaml.safe_load(rendered)
    statics = parsed["nodes"][0]["statics"]
    metas = {s["meta"] for s in statics}
    assert metas == {"source_ip", "log_type"}
