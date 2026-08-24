"""Render tests for the accessory `expose:` field (M83 / S2).

Covers:
- `expose: loopback` → `127.0.0.1:<port_pair>`
- `expose: tailnet` (and its synonym `expose: gateway`) → `<gateway_bind_ip>:<port_pair>`
- `expose: host` → `0.0.0.0:<port_pair>`
- `expose:` overrides any IP prefix present in `port:` (authoritative)
- Backwards-compat shim: link-target accessory without `expose:` still
  gets its 127.0.0.1: rewritten to 0.0.0.0: (until S7 removes the shim)
- Non-link-target accessory without `expose:` renders `port:` verbatim
  (no rewrite — the incident fix)
- JSON schema rejects invalid `expose:` values

The render harness is the same `_render_accessory` shape used by
test_log_rotation — real `_accessory.j2` + `_macros.j2` under an
Ansible-shaped Jinja2 environment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from helpers import make_ansible_env

# ── Ansible filter shims (same set as test_log_rotation) ───────────────
# Keep in sync if additional filters get used by the macro.


def _to_json_filter(value, **_kwargs):
    return json.dumps(value)


def _regex_replace_filter(value, pattern, replacement, ignorecase=False):
    flags = re.IGNORECASE if ignorecase else 0
    return re.sub(pattern, replacement, str(value), flags=flags)


def _dict2items_filter(d):
    return [{"key": k, "value": v} for k, v in (d or {}).items()]


def _combine_filter(base, *others, recursive=False, list_merge="replace"):
    result = dict(base or {})
    for other in others:
        if other is None:
            continue
        result.update(other)
    return result


def _register_filters(env):
    env.filters.setdefault("to_json", _to_json_filter)
    env.filters.setdefault("regex_replace", _regex_replace_filter)
    env.filters.setdefault("dict2items", _dict2items_filter)
    env.filters.setdefault("combine", _combine_filter)
    return env


TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "deploy_stack"
    / "templates"
)


def _render_accessory(acc_name: str, acc: dict, **ctx_overrides) -> str:
    env = _register_filters(make_ansible_env(TEMPLATE_DIR))
    ctx = {
        "stack_name": "testapp",
        "stack_dir": "/opt/testapp",
        "traefik_docker_network": "services",
        "link_targets": [],
        "log_rotation_defaults": False,
        "watchtower_enabled": False,
        "active_accessories": {acc_name: acc},
    }
    ctx.update(ctx_overrides)
    tmpl_src = (
        "{% import '_macros.j2' as macros with context %}"
        "{% for name, acc in active_accessories.items() %}"
        "{% include '_accessory.j2' %}"
        "{% endfor %}"
    )
    return env.from_string(tmpl_src).render(**ctx)


def _port_line(rendered: str) -> str | None:
    m = re.search(r'^ +- "(?P<binding>[^"]+)"\s*$', rendered, flags=re.MULTILINE)
    return m.group("binding") if m else None


# ── expose: modes ─────────────────────────────────────────────────────


def test_expose_loopback_binds_to_127_0_0_1():
    acc = {"image": "redis:7-alpine", "port": "6379:6379", "expose": "loopback"}
    rendered = _render_accessory("redis", acc)
    assert _port_line(rendered) == "127.0.0.1:6379:6379"


def test_expose_tailnet_binds_to_tailnet_ip():
    acc = {"image": "postgres:17", "port": "5432:5432", "expose": "tailnet"}
    rendered = _render_accessory(
        "postgres", acc, gateway_bind_ip="100.64.0.1"
    )
    assert _port_line(rendered) == "100.64.0.1:5432:5432"


def test_expose_host_binds_to_0_0_0_0():
    acc = {"image": "postgres:17", "port": "5432:5432", "expose": "host"}
    rendered = _render_accessory("postgres", acc)
    assert _port_line(rendered) == "0.0.0.0:5432:5432"


def test_expose_overrides_ip_prefix_in_port():
    """Operator left a literal IP in port:, expose: is authoritative."""
    acc = {
        "image": "redis:7-alpine",
        "port": "0.0.0.0:6379:6379",
        "expose": "loopback",
    }
    rendered = _render_accessory("redis", acc)
    assert _port_line(rendered) == "127.0.0.1:6379:6379"


# ── backwards-compat shim (removed in S7) ─────────────────────────────


def test_link_target_without_expose_defaults_to_loopback_after_s7():
    """After M83-S7 severed the links:-driven port rewrite, a
    link-target accessory without `expose:` defaults to loopback —
    the same as any other accessory. The old shim that rewrote
    127.0.0.1 to 0.0.0.0 for link targets is gone."""
    acc = {"image": "postgres:17", "port": "127.0.0.1:5432:5432"}
    rendered = _render_accessory("postgres", acc, link_targets=["postgres"])
    assert _port_line(rendered) == "127.0.0.1:5432:5432"


def test_no_expose_defaults_to_loopback():
    """No `expose:` + no IP prefix → safe-default loopback bind. This
    is the post-S7 behaviour: accessories communicate in-stack via
    Docker service DNS by default; a host bind is opt-in."""
    acc = {"image": "redis:7-alpine", "port": "6379:6379"}
    rendered = _render_accessory("redis", acc)
    assert _port_line(rendered) == "127.0.0.1:6379:6379"


def test_no_expose_with_ip_prefix_still_resolves_via_default():
    """If the operator leaves an IP prefix in `port:` without setting
    `expose:`, the prefix is stripped and loopback default applies.
    This is deliberately forgiving — the S5 validator flags the case
    at validate time so operators know to clean up."""
    acc = {"image": "redis:7-alpine", "port": "127.0.0.1:6379:6379"}
    rendered = _render_accessory("redis", acc)
    assert _port_line(rendered) == "127.0.0.1:6379:6379"


# ── shim sentinel comment present so S7 can grep for it ───────────────


def test_expose_shim_removed_in_s7():
    """The backwards-compat shim was removed in M83-S7. If this test
    starts failing, someone reintroduced it — do not."""
    src = (TEMPLATE_DIR / "_macros.j2").read_text()
    assert "SHIM: remove in S7" not in src
    assert "127\\\\.0\\\\.0\\\\.1:" not in src, (
        "M83-S7: no IP-rewriting regex should remain in _macros.j2; the "
        "expose: field is authoritative"
    )


# ── schema ────────────────────────────────────────────────────────────

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "bay_cli"
    / "schemas"
    / "services.schema.json"
)


def test_expose_invalid_value_rejected_by_schema():
    """Schema enumerates expose values — anything else must be rejected."""
    schema = json.loads(SCHEMA_PATH.read_text())
    accessory_schema = schema["$defs"]["accessory"]["properties"]
    assert "expose" in accessory_schema, "schema must declare `expose` on accessory"
    assert accessory_schema["expose"]["enum"] == ["loopback", "gateway", "tailnet", "host"]


def test_expose_schema_is_optional():
    """`expose` is not required — loopback is the implicit default."""
    schema = json.loads(SCHEMA_PATH.read_text())
    required = schema["$defs"]["accessory"].get("required", [])
    assert "expose" not in required
