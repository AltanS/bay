"""Render tests for the per-service `log_rotation` flag (M82 / GH bay#5).

Covers:
- Default case: `logging:` block rendered from `log_rotation_defaults`
- Per-service override: partial keys merged onto global defaults
- Opt-out: `log_rotation: false` suppresses the block entirely
- Accessory path: same three cases through `_accessory.j2`
- Infra containers: traefik/watchtower/webhook/zot/headscale render the block
  from `log_rotation_defaults`, and traefik honours `traefik_log_rotation`

These tests render the real compose partials with an Ansible-shaped Jinja2
environment (trim_blocks=True, lstrip_blocks=False) to catch whitespace
regressions that a bare Environment() would miss.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml
from helpers import make_ansible_env


# ── Ansible filter shims ──────────────────────────────────────────────
# `_service.j2` references the Ansible-only `hash` filter inside the
# basic_auth branch. The filter must exist at parse time even though our
# fixtures don't trigger that branch.

def _hash_filter(value, algo="sha1"):
    return hashlib.new(algo, str(value).encode()).hexdigest()


def _combine_filter(base, *others, recursive=False, list_merge="replace"):
    """Minimal port of Ansible's `combine` filter — shallow merge is enough
    for our defaults | overrides pattern."""
    result = dict(base or {})
    for other in others:
        if other is None:
            continue
        result.update(other)
    return result


def _to_json_filter(value, **_kwargs):
    import json

    return json.dumps(value)


def _quote_filter(value):
    import shlex

    return shlex.quote(str(value))


def _regex_replace_filter(value, pattern, replacement, ignorecase=False):
    flags = re.IGNORECASE if ignorecase else 0
    return re.sub(pattern, replacement, str(value), flags=flags)


def _dict2items_filter(d):
    return [{"key": k, "value": v} for k, v in (d or {}).items()]


def _dirname_filter(path):
    import os.path

    return os.path.dirname(str(path))


def _register_ansible_filters(env):
    env.filters.setdefault("hash", _hash_filter)
    env.filters.setdefault("combine", _combine_filter)
    env.filters.setdefault("to_json", _to_json_filter)
    env.filters.setdefault("quote", _quote_filter)
    env.filters.setdefault("regex_replace", _regex_replace_filter)
    env.filters.setdefault("dict2items", _dict2items_filter)
    env.filters.setdefault("dirname", _dirname_filter)
    env.filters.setdefault("bool", lambda v: bool(v))
    env.globals.setdefault("lookup", lambda *_a, **_k: "")
    return env

TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "deploy_stack"
    / "templates"
)

DEFAULTS_YML = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "deploy_stack"
    / "defaults"
    / "main.yml"
)


# ── Fixtures ──────────────────────────────────────────────────────────

DEFAULT_LOG_ROTATION = {"driver": "json-file", "max_size": "50m", "max_file": 3}


def _base_ctx(**overrides):
    ctx = {
        "stack_name": "testapp",
        "stack_dir": "/opt/testapp",
        "traefik_docker_network": "services",
        "traefik_container_name": "traefik",
        "traefik_image": "traefik:v3.6",
        "traefik_config_path": "/opt/testapp/traefik/traefik.yml",
        "traefik_acme_path": "/opt/testapp/traefik/acme.json",
        "traefik_access_log_path": "/opt/testapp/traefik/logs/access.log",
        "traefik_security_headers_enabled": False,
        "traefik_compress_enabled": False,
        "traefik_error_pages_enabled": False,
        "link_targets": [],
        "log_rotation_defaults": DEFAULT_LOG_ROTATION,
        "watchtower_enabled": False,
        "watchtower_image": "containrrr/watchtower:latest",
        "watchtower_schedule": "0 0 4 * * *",
        "watchtower_cleanup": True,
        "watchtower_monitor_only": True,
        "watchtower_telegram_bot_token": "",
        "watchtower_telegram_chat_id": "",
        "zot_enabled": False,
        "zot_image": "ghcr.io/project-zot/zot-linux-amd64:v2.1.2",
        "zot_config_dir": "/opt/testapp/zot/config",
        "zot_data_dir": "/opt/testapp/zot/data",
        "zot_domain": "registry.example.com",
        "zot_port": 5000,
        "vpn_allowed_ips": ["10.0.0.0/8"],
        "active_services": {},
        "active_accessories": {},
        "services": {},
        "webhook": None,
    }
    ctx.update(overrides)
    return ctx


def _render_service(svc_name: str, svc: dict, **ctx_overrides) -> str:
    """Render a single service block using the real _service.j2 partial."""
    env = _register_ansible_filters(make_ansible_env(TEMPLATE_DIR))
    ctx = _base_ctx(active_services={svc_name: svc}, **ctx_overrides)
    tmpl_src = (
        "{% import '_macros.j2' as macros with context %}"
        "{% for name, svc in active_services.items() %}"
        "{% include '_service.j2' %}"
        "{% endfor %}"
    )
    tmpl = env.from_string(tmpl_src)
    return tmpl.render(**ctx)


def _render_accessory(acc_name: str, acc: dict, **ctx_overrides) -> str:
    env = _register_ansible_filters(make_ansible_env(TEMPLATE_DIR))
    ctx = _base_ctx(active_accessories={acc_name: acc}, **ctx_overrides)
    tmpl_src = (
        "{% import '_macros.j2' as macros with context %}"
        "{% for name, acc in active_accessories.items() %}"
        "{% include '_accessory.j2' %}"
        "{% endfor %}"
    )
    tmpl = env.from_string(tmpl_src)
    return tmpl.render(**ctx)


def _render_infra(partial: str, **ctx_overrides) -> str:
    env = _register_ansible_filters(make_ansible_env(TEMPLATE_DIR))
    ctx = _base_ctx(**ctx_overrides)
    tmpl_src = (
        "{% import '_macros.j2' as macros with context %}"
        f"{{% include '{partial}' %}}"
    )
    tmpl = env.from_string(tmpl_src)
    return tmpl.render(**ctx)


def _logging_block(rendered: str) -> dict | None:
    """Extract the first `logging:` block from a rendered compose snippet."""
    match = re.search(
        r"^    logging:\s*\n"
        r"^      driver:\s*(?P<driver>\S+)\s*\n"
        r"^      options:\s*\n"
        r"^        max-size:\s*\"(?P<max_size>[^\"]+)\"\s*\n"
        r"^        max-file:\s*\"(?P<max_file>[^\"]+)\"",
        rendered,
        flags=re.MULTILINE,
    )
    if not match:
        return None
    return match.groupdict()


_SIMPLE_PUBLIC_SVC = {
    "access": "public",
    "image": "nginx:latest",
    "domains": ["app.example.com"],
    "ports": {"internal": 80},
}


# ── Default case ──────────────────────────────────────────────────────


def test_service_default_emits_logging_block_with_defaults():
    rendered = _render_service("app", {**_SIMPLE_PUBLIC_SVC})
    block = _logging_block(rendered)
    assert block == {"driver": "json-file", "max_size": "50m", "max_file": "3"}


def test_accessory_default_emits_logging_block_with_defaults():
    acc = {"image": "postgres:15"}
    rendered = _render_accessory("db", acc)
    block = _logging_block(rendered)
    assert block == {"driver": "json-file", "max_size": "50m", "max_file": "3"}


# ── Per-service override ──────────────────────────────────────────────


def test_service_override_merges_partial_keys():
    svc = {**_SIMPLE_PUBLIC_SVC, "log_rotation": {"max_size": "200m", "max_file": 10}}
    rendered = _render_service("app", svc)
    block = _logging_block(rendered)
    assert block == {"driver": "json-file", "max_size": "200m", "max_file": "10"}


def test_service_override_full_replacement():
    svc = {
        **_SIMPLE_PUBLIC_SVC,
        "log_rotation": {"driver": "local", "max_size": "500m", "max_file": 20},
    }
    rendered = _render_service("app", svc)
    block = _logging_block(rendered)
    assert block == {"driver": "local", "max_size": "500m", "max_file": "20"}


def test_accessory_override_merges():
    acc = {"image": "postgres:15", "log_rotation": {"max_size": "10m"}}
    rendered = _render_accessory("db", acc)
    block = _logging_block(rendered)
    # max_size overridden, driver + max_file from defaults
    assert block == {"driver": "json-file", "max_size": "10m", "max_file": "3"}


# ── Opt-out: log_rotation: false ──────────────────────────────────────


def test_service_log_rotation_false_suppresses_block():
    svc = {**_SIMPLE_PUBLIC_SVC, "log_rotation": False}
    rendered = _render_service("app", svc)
    assert _logging_block(rendered) is None
    assert "logging:" not in rendered


def test_accessory_log_rotation_false_suppresses_block():
    acc = {"image": "postgres:15", "log_rotation": False}
    rendered = _render_accessory("db", acc)
    assert _logging_block(rendered) is None


def test_global_defaults_false_suppresses_block_everywhere():
    """If the consumer sets `log_rotation_defaults: false` globally, no
    container emits a logging block (fall back to daemon log-opts)."""
    rendered = _render_service(
        "app",
        {**_SIMPLE_PUBLIC_SVC},
        log_rotation_defaults=False,
    )
    assert _logging_block(rendered) is None


# ── Infra containers ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "partial,container_name",
    [
        ("_traefik.j2", "traefik"),
        ("_watchtower.j2", "watchtower"),
        ("_zot.j2", "zot"),
        ("_headscale.j2", "headscale"),
    ],
)
def test_infra_containers_emit_defaults(partial, container_name):
    overrides = {}
    if partial == "_watchtower.j2":
        overrides["watchtower_enabled"] = True
    elif partial == "_zot.j2":
        overrides["zot_enabled"] = True
    elif partial == "_headscale.j2":
        overrides["access_gateway"] = "headscale"
        overrides["headscale_server"] = True
        overrides["headscale_version"] = "0.28.0"
        overrides["headscale_domain"] = "hs.example.com"

    rendered = _render_infra(partial, **overrides)
    block = _logging_block(rendered)
    assert block is not None, f"{partial} did not emit logging block"
    assert block["driver"] == "json-file"
    assert block["max_size"] == "50m"
    assert block["max_file"] == "3"


def test_traefik_log_rotation_override():
    """traefik_log_rotation variable overrides the defaults for traefik only."""
    rendered = _render_infra(
        "_traefik.j2",
        traefik_log_rotation={"max_size": "500m", "max_file": 20},
    )
    block = _logging_block(rendered)
    assert block == {"driver": "json-file", "max_size": "500m", "max_file": "20"}


def test_traefik_log_rotation_false_suppresses():
    rendered = _render_infra(
        "_traefik.j2",
        traefik_log_rotation=False,
    )
    assert _logging_block(rendered) is None


# ── Compose YAML stays parseable ──────────────────────────────────────


def test_full_compose_fragment_parses_as_yaml():
    """The logging block must produce valid YAML even when combined with the
    surrounding labels/router blocks."""
    rendered = _render_service("app", {**_SIMPLE_PUBLIC_SVC})
    wrapped = "services:\n" + rendered
    parsed = yaml.safe_load(wrapped)
    assert "services" in parsed
    assert "app" in parsed["services"]
    assert parsed["services"]["app"]["logging"] == {
        "driver": "json-file",
        "options": {"max-size": "50m", "max-file": "3"},
    }


def test_override_compose_fragment_parses_as_yaml():
    svc = {**_SIMPLE_PUBLIC_SVC, "log_rotation": {"max_size": "200m", "max_file": 10}}
    rendered = _render_service("app", svc)
    wrapped = "services:\n" + rendered
    parsed = yaml.safe_load(wrapped)
    assert parsed["services"]["app"]["logging"]["options"] == {
        "max-size": "200m",
        "max-file": "10",
    }


# ── Role defaults shape ───────────────────────────────────────────────


def test_role_defaults_file_defines_log_rotation_defaults():
    with DEFAULTS_YML.open() as f:
        data = yaml.safe_load(f)
    assert "log_rotation_defaults" in data
    assert data["log_rotation_defaults"] == DEFAULT_LOG_ROTATION


def test_role_defaults_max_size_matches_size_pattern():
    with DEFAULTS_YML.open() as f:
        data = yaml.safe_load(f)
    assert re.match(
        r"^\d+[kmg]?$",
        data["log_rotation_defaults"]["max_size"],
        re.IGNORECASE,
    )


# ── CLI JSON schema accepts log_rotation ──────────────────────────────


SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "bay_cli"
    / "schemas"
    / "services.schema.json"
)


def _load_schema():
    import json

    with SCHEMA_PATH.open() as f:
        return json.load(f)


def test_cli_schema_defines_log_rotation():
    schema = _load_schema()
    assert "log_rotation" in schema["$defs"]


def test_cli_schema_service_references_log_rotation():
    schema = _load_schema()
    svc_props = schema["$defs"]["service"]["properties"]
    assert "log_rotation" in svc_props
    assert svc_props["log_rotation"]["$ref"] == "#/$defs/log_rotation"


def test_cli_schema_accessory_references_log_rotation():
    schema = _load_schema()
    acc_props = schema["$defs"]["accessory"]["properties"]
    assert "log_rotation" in acc_props


@pytest.mark.parametrize(
    "value",
    [
        False,
        {"driver": "json-file", "max_size": "50m", "max_file": 3},
        {"max_size": "200m", "max_file": 10},
        {"max_size": "2g"},
        {"driver": "local"},
        {},
    ],
)
def test_cli_schema_accepts_valid_log_rotation(value):
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load_schema()
    validator = jsonschema.Draft202012Validator(schema["$defs"]["log_rotation"])
    errors = list(validator.iter_errors(value))
    assert not errors, f"expected {value!r} to validate, got {errors}"


@pytest.mark.parametrize(
    "value,reason",
    [
        (True, "`true` is not a valid opt-out"),
        ("json-file", "string not allowed at top level"),
        ({"driver": "syslog"}, "unsupported driver"),
        ({"max_size": "50 MB"}, "unsupported size literal"),
        ({"max_size": 50}, "int not allowed for max_size"),
        ({"max_file": 0}, "zero files not allowed"),
        ({"max_file": -1}, "negative files not allowed"),
        ({"max_file": "3"}, "string not allowed for max_file"),
        ({"unknown_key": "x"}, "unknown key rejected"),
    ],
)
def test_cli_schema_rejects_invalid_log_rotation(value, reason):
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load_schema()
    validator = jsonschema.Draft202012Validator(schema["$defs"]["log_rotation"])
    errors = list(validator.iter_errors(value))
    assert errors, f"expected {value!r} to fail ({reason})"


# ── validate.yml (role-level) sanity ──────────────────────────────────

VALIDATE_YML = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "deploy_stack"
    / "tasks"
    / "validate.yml"
)


def test_role_validate_yml_covers_log_rotation():
    """Regression: the role-level validate.yml must emit checks for the
    log_rotation shape, driver, max_size, and max_file. Covers the deploy-time
    guard rail, independent of the CLI schema."""
    content = VALIDATE_YML.read_text()
    assert "log_rotation" in content
    # Each of the four guard rails below must exist
    for needle in ("log_rotation.driver", "log_rotation.max_size", "log_rotation.max_file"):
        assert needle in content, f"validate.yml missing check for {needle}"


def test_role_validate_yml_uses_combine_for_services_and_accessories():
    """The checks should apply to both services AND accessories — enforced via
    `active_services | combine(active_accessories)`. Regression guard against
    someone copying the services-only pattern when fixing this later."""
    content = VALIDATE_YML.read_text()
    assert "active_services | combine(active_accessories)" in content


# ── Infra compose template must import macros ────────────────────────


INFRA_COMPOSE = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "deploy_stack"
    / "templates"
    / "docker-compose.infra.yml.j2"
)

MAIN_COMPOSE = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "deploy_stack"
    / "templates"
    / "docker-compose.yml.j2"
)


def test_infra_compose_imports_macros():
    """Regression: docker-compose.infra.yml.j2 includes the _traefik/_watchtower/
    _webhook partials which now call macros.logging_block. The parent template
    must import _macros.j2 or every deploy fails with 'macros is undefined'."""
    content = INFRA_COMPOSE.read_text()
    assert "import '_macros.j2'" in content, (
        "docker-compose.infra.yml.j2 must import _macros.j2 — the infra "
        "partials call macros.logging_block()."
    )


def test_main_compose_imports_macros():
    content = MAIN_COMPOSE.read_text()
    assert "import '_macros.j2'" in content


def test_infra_compose_renders_end_to_end():
    """Render docker-compose.infra.yml.j2 with minimal context and parse the
    output as YAML. Catches the kind of 'macros is undefined' failure that
    only surfaces during real deploys if we don't test the top-level partials."""
    env = _register_ansible_filters(make_ansible_env(TEMPLATE_DIR))
    ctx = _base_ctx(watchtower_enabled=True)
    tmpl = env.get_template("docker-compose.infra.yml.j2")
    rendered = tmpl.render(**ctx)
    parsed = yaml.safe_load(rendered)
    assert "services" in parsed
    assert "traefik" in parsed["services"]
    assert parsed["services"]["traefik"]["logging"] == {
        "driver": "json-file",
        "options": {"max-size": "50m", "max-file": "3"},
    }


# ── bay_log_rotation_spec filter ─────────────────────────────────────


def _load_lifecycle_filter():
    import importlib.util

    path = (
        Path(__file__).resolve().parent.parent
        / "roles"
        / "container_lifecycle"
        / "filter_plugins"
        / "bay_filters.py"
    )
    spec = importlib.util.spec_from_file_location("lifecycle_bay_filters", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.bay_log_rotation_spec


def test_filter_default_produces_log_driver_and_options():
    fn = _load_lifecycle_filter()
    out = fn(None, DEFAULT_LOG_ROTATION)
    assert out == {
        "log_driver": "json-file",
        "log_options": {"max-size": "50m", "max-file": "3"},
    }


def test_filter_partial_override_merges():
    fn = _load_lifecycle_filter()
    out = fn({"max_size": "200m", "max_file": 10}, DEFAULT_LOG_ROTATION)
    assert out == {
        "log_driver": "json-file",
        "log_options": {"max-size": "200m", "max-file": "10"},
    }


def test_filter_full_override_replaces_driver():
    fn = _load_lifecycle_filter()
    out = fn(
        {"driver": "local", "max_size": "500m", "max_file": 20},
        DEFAULT_LOG_ROTATION,
    )
    assert out["log_driver"] == "local"
    assert out["log_options"] == {"max-size": "500m", "max-file": "20"}


def test_filter_explicit_false_opts_out():
    fn = _load_lifecycle_filter()
    assert fn(False, DEFAULT_LOG_ROTATION) == {}


def test_filter_global_defaults_false_opts_out():
    fn = _load_lifecycle_filter()
    assert fn(None, False) == {}


def test_filter_non_dict_override_is_ignored():
    """Defensive: if validate.yml is bypassed and someone passes garbage, the
    filter returns {} rather than producing a broken docker kwargs dict."""
    fn = _load_lifecycle_filter()
    assert fn("nope", DEFAULT_LOG_ROTATION) == {}
    assert fn(42, DEFAULT_LOG_ROTATION) == {}


# ── reconciler carries log_driver/log_options through to docker ──


LIFECYCLE_TASKS_DIR = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "container_lifecycle"
    / "tasks"
)

RECONCILE_PKG = Path(__file__).resolve().parent.parent / "src" / "bay_reconcile"


def test_reconcile_bundle_carries_log_driver_and_options():
    """The reconcile bundle entry must include log_driver/log_options so the
    log-rotation fields build_specs injects reach the server-side engine.
    Regression guard after the v0.83 sandbox deploy surprise: the per-container
    Ansible deploy tasks used to pass these kwargs to docker_container, but
    M85-S8 retired that path, so the guard now lives on the reconciler bundle."""
    content = (LIFECYCLE_TASKS_DIR / "reconcile.yml").read_text()
    assert "'log_driver': _spec.log_driver" in content, (
        "reconcile.yml bundle entry is missing log_driver"
    )
    assert "'log_options': _spec.log_options" in content, (
        "reconcile.yml bundle entry is missing log_options"
    )


def test_sdk_client_maps_log_driver_to_log_config():
    """The SDK client must translate spec.log_driver/log_options into docker's
    log_config kwarg — otherwise a container silently stays on the daemon log
    defaults (the v0.83 surprise)."""
    content = (RECONCILE_PKG / "sdk_client.py").read_text()
    assert 'kwargs["log_config"]' in content
    assert "spec.log_driver" in content
    assert "spec.log_options" in content


def test_build_specs_injects_log_rotation_for_services():
    content = (LIFECYCLE_TASKS_DIR / "build_specs.yml").read_text()
    assert "bay_log_rotation_spec" in content, (
        "build_specs.yml must apply bay_log_rotation_spec to each container spec"
    )


def test_build_specs_also_wires_infra_containers():
    """traefik/watchtower/webhook/zot/headscale/error-pages specs each need
    the log_rotation fields injected, otherwise lifecycle bypasses logging."""
    content = (LIFECYCLE_TASKS_DIR / "build_specs.yml").read_text()
    # The filter should be referenced multiple times (once per infra spec + once
    # per loop for services/accessories).
    assert content.count("bay_log_rotation_spec") >= 6, (
        "bay_log_rotation_spec should be wired into services, accessories, "
        "and every infra spec (traefik, watchtower, headscale, webhook, zot, "
        "error-pages). Count suggests one or more sites were missed."
    )
