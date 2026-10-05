"""`memswap_limit` must follow the same path as `mem_limit`.

Why it exists: one container (the one that holds photo bytes in memory only)
must get no swap, and the host cannot turn swap off. Docker does it with
`memswap_limit` equal to `mem_limit`. The key travels from services.yml
through the schema, build_specs.yml, the reconcile bundle and the Docker SDK
call, and `bay validate` refuses the two combinations Docker rejects.

The Docker SDK call is checked by capturing the kwargs passed to
`containers.run`. No daemon is needed, so the real daemon behaviour (the
cgroup `memory.swap.max` becoming 0) is not covered here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from jinja2 import Environment
from jsonschema import Draft202012Validator

from bay_cli.commands.validate import (
    ValidationResult,
    _load_schema,
    _parse_mem_bytes,
    _validate_memswap_limits,
)
from bay_reconcile.bundle import spec_from_dict
from bay_reconcile.models import ContainerSpec
from bay_reconcile.sdk_client import SdkDockerClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILD_SPECS = _REPO_ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
_RECONCILE = _REPO_ROOT / "roles" / "container_lifecycle" / "tasks" / "reconcile.yml"
_MACROS = _REPO_ROOT / "roles" / "deploy_stack" / "templates" / "_macros.j2"
_REBUILD = _REPO_ROOT / "roles" / "git_deploy" / "templates" / "rebuild.sh.j2"

_filter_dir = str(_REPO_ROOT / "filter_plugins")
if _filter_dir not in sys.path:
    sys.path.insert(0, _filter_dir)

from bay_filters import bay_prefix_volumes  # noqa: E402

# ── bundle + SDK ─────────────────────────────────────────────────────────


class _CapturingContainers:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def run(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _CapturingClient:
    def __init__(self) -> None:
        self.containers = _CapturingContainers()


def _client() -> tuple[SdkDockerClient, _CapturingClient]:
    client = SdkDockerClient.__new__(SdkDockerClient)
    fake = _CapturingClient()
    client._c = fake
    client._managed_label = "bay.managed"
    client._stack_label = "bay.stack"
    client._stack = "test"
    return client, fake


def _spec(**over: Any) -> ContainerSpec:
    base: dict[str, Any] = {
        "name": "demo",
        "image": "alpine:3.20",
        "type": "service",
        "config_hash": "deadbeef",
    }
    base.update(over)
    return ContainerSpec(**base)


def test_bundle_parses_memswap_limit():
    spec = spec_from_dict(
        {
            "name": "core",
            "image": "x:1",
            "type": "service",
            "config_hash": "abc",
            "mem_limit": "512m",
            "memswap_limit": "512m",
        }
    )
    assert spec.mem_limit == "512m"
    assert spec.memswap_limit == "512m"


def test_bundle_defaults_memswap_limit_to_none():
    spec = spec_from_dict(
        {"name": "db", "image": "postgres:16", "type": "accessory", "config_hash": "abc"}
    )
    assert spec.memswap_limit is None


def test_create_forwards_memswap_limit_to_the_sdk():
    client, fake = _client()
    client.create(_spec(mem_limit="512m", memswap_limit="512m"))
    assert fake.containers.kwargs["mem_limit"] == "512m"
    assert fake.containers.kwargs["memswap_limit"] == "512m"


def test_create_omits_memswap_limit_when_unset():
    """Control: None must not reach Docker, so existing services are untouched."""
    client, fake = _client()
    client.create(_spec(mem_limit="512m"))
    assert fake.containers.kwargs["mem_limit"] == "512m"
    assert "memswap_limit" not in fake.containers.kwargs


# ── build_specs.yml and reconcile.yml ────────────────────────────────────


def _combine(base, *others, recursive=False, list_merge="replace"):
    result = dict(base or {})
    for other in others:
        if other:
            result.update(other)
    return result


def _render_optional(task_name: str, **context) -> dict:
    tasks = yaml.safe_load(_BUILD_SPECS.read_text())
    expr = next(t for t in tasks if t.get("name") == task_name)["vars"]["_optional"]
    env = Environment(trim_blocks=True, lstrip_blocks=False)
    env.filters["combine"] = _combine
    env.filters["bay_prefix_volumes"] = bay_prefix_volumes
    env.filters["bay_healthcheck"] = lambda hc, port: dict(hc)
    env.filters["bay_log_rotation_spec"] = lambda v, d: {}
    rendered = env.from_string(expr).render(**context)
    return eval(rendered, {"__builtins__": {}}, {})  # noqa: S307 - test-local literal


def _service_ctx(svc: dict) -> dict:
    return {
        "_svc": svc,
        "_name": "demo",
        "stack_dir": "/opt/test",
        "stack_name": "test",
        "_svc_expose": "",
        "_svc_port_rendered": "",
        "_log_rotation": {},
    }


def _accessory_ctx(acc: dict) -> dict:
    return {
        "_acc": acc,
        "_name": "demo-db",
        "stack_dir": "/opt/test",
        "stack_name": "test",
        "traefik_docker_network": "services",
        "_acc_port_rendered": "",
        "_log_rotation": {},
    }


@pytest.mark.parametrize(
    ("task", "ctx"),
    [
        ("Build service container specs", _service_ctx),
        ("Build accessory container specs", _accessory_ctx),
    ],
)
def test_spec_builder_emits_memswap_limit_only_when_set(task, ctx):
    base = {"image": "x:1", "mem_limit": "512m"}
    with_key = _render_optional(task, **ctx({**base, "memswap_limit": "512m"}))
    assert with_key["memswap_limit"] == "512m"
    # Control: without the key, the spec has no such key at all, which is
    # what keeps the config hash of an existing service unchanged.
    without = _render_optional(task, **ctx(base))
    assert "memswap_limit" not in without
    assert without["mem_limit"] == "512m"


def test_reconcile_bundle_dict_carries_memswap_limit():
    text = _RECONCILE.read_text()
    assert "'memswap_limit': _spec.memswap_limit | default(None)" in text


def test_compose_macro_renders_memswap_limit():
    text = _MACROS.read_text()
    assert "{% if cfg.memswap_limit is defined %}" in text
    assert "memswap_limit: {{ cfg.memswap_limit }}" in text


def test_rebuild_script_passes_memory_swap_with_memory():
    """rebuild.sh recreates git-built services with `docker run`; both of its
    run sites pass --memory and must pass --memory-swap beside it."""
    text = _REBUILD.read_text()
    assert text.count('--memory "{{ svc.mem_limit }}"') == 2
    assert text.count('--memory-swap "{{ svc.memswap_limit }}"') == 2


# ── schema ───────────────────────────────────────────────────────────────


def _schema_errors(data: dict) -> list:
    return list(Draft202012Validator(_load_schema()).iter_errors(data))


def _schema_data(svc_extra: dict, acc_extra: dict | None = None) -> dict:
    return {
        "services": {
            "a": {
                "image": "nginx:latest",
                "access": "public",
                "domains": ["app.example.com"],
                "ports": {"internal": 8080},
                **svc_extra,
            }
        },
        "accessories": {"db": {"image": "postgres:16", **(acc_extra or {})}},
    }


def test_schema_accepts_memswap_limit_on_service_and_accessory():
    data = _schema_data(
        {"mem_limit": "512m", "memswap_limit": "512m"},
        {"mem_limit": "1g", "memswap_limit": "1g"},
    )
    assert _schema_errors(data) == []


def test_schema_rejects_non_string_memswap_limit():
    """Control: the same data is valid with a string, so the failure is the field."""
    assert _schema_errors(_schema_data({"memswap_limit": "512m"})) == []
    errors = _schema_errors(_schema_data({"memswap_limit": 512}))
    assert errors != []
    assert "memswap_limit" in str(errors[0].absolute_path)


# ── bay validate ─────────────────────────────────────────────────────────


class _JsonMode:
    def __enter__(self):
        from bay_cli.console.output import set_json_mode

        set_json_mode(True)
        return self

    def __exit__(self, *args):
        from bay_cli.console.output import set_json_mode

        set_json_mode(False)


def _check(data: dict) -> ValidationResult:
    result = ValidationResult()
    with _JsonMode():
        _validate_memswap_limits(data, result)
    return result


def test_validate_accepts_equal_limits():
    r = _check({"services": {"a": {"mem_limit": "512m", "memswap_limit": "512m"}}})
    assert r.total_issues == 0


def test_validate_accepts_larger_swap_and_unit_mix():
    r = _check({"services": {"a": {"mem_limit": "512m", "memswap_limit": "1g"}}})
    assert r.total_issues == 0
    r = _check({"services": {"a": {"mem_limit": "1g", "memswap_limit": "1024m"}}})
    assert r.total_issues == 0


def test_validate_accepts_unlimited_swap():
    r = _check({"services": {"a": {"mem_limit": "512m", "memswap_limit": "-1"}}})
    assert r.total_issues == 0


def test_validate_ignores_services_without_memswap_limit():
    r = _check({"services": {"a": {"mem_limit": "512m"}, "b": {"image": "x"}}})
    assert r.total_issues == 0


def test_validate_rejects_memswap_without_mem_limit():
    r = _check({"services": {"a": {"memswap_limit": "512m"}}})
    assert r.total_issues == 1
    assert "services.a" in r.failed[0]
    assert "without mem_limit" in r.failed[0]


def test_validate_rejects_swap_smaller_than_memory():
    r = _check({"accessories": {"db": {"mem_limit": "1g", "memswap_limit": "512m"}}})
    assert r.total_issues == 1
    assert "accessories.db" in r.failed[0]
    assert "smaller than mem_limit" in r.failed[0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("512m", 512 * 1024**2),
        ("1g", 1024**3),
        ("2K", 2048),
        ("100", 100),
        ("-1", None),
        ("1.5g", None),
    ],
)
def test_parse_mem_bytes(value, expected):
    assert _parse_mem_bytes(value) == expected
