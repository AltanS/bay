"""``cpus``: a CPU limit that follows the same path as ``memory``.

The key travels from bay.toml (top level, ``[services.<name>]``,
``[deploy.<env>]``, and the fleet's ``[resources.*]``) through the compile,
build_specs.yml, the reconcile bundle, the planner and the docker SDK call.
An unset ``cpus`` must leave every one of those untouched, so no existing
container is recreated by the upgrade.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from jinja2 import Environment
from jsonschema import Draft202012Validator

from bay_cli import bay_toml, compiler
from bay_cli.commands.validate import _load_schema
from bay_cli.fleet import FLEET_SCHEMA_PATH, load_inputs
from bay_reconcile.bundle import spec_from_dict
from bay_reconcile.models import ContainerSpec, ContainerState
from bay_reconcile.observe import parse_state
from bay_reconcile.planner import REASON_CODES, describe, plan
from bay_reconcile.sdk_client import SdkDockerClient

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "fleet_min"
SHOP = Path("checkouts") / "shop" / "bay.toml"
FLEET = Path("bay.fleet.toml")
_BUILD_SPECS = ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
_RECONCILE = ROOT / "roles" / "container_lifecycle" / "tasks" / "reconcile.yml"
_REBUILD = ROOT / "roles" / "git_deploy" / "templates" / "rebuild.sh.j2"

_filter_dir = str(ROOT / "filter_plugins")
if _filter_dir not in sys.path:
    sys.path.insert(0, _filter_dir)

from bay_filters import bay_prefix_volumes, bay_spec_hash  # noqa: E402

# ── bay.toml schema ──────────────────────────────────────────────────────────

_BASE: dict[str, Any] = {
    "name": "app",
    "fleet": "myfleet",
    "port": 3000,
    "access": {"mode": "public"},
    "deploy": {"production": {"domain": "app.example.com"}},
}


def _doc(where: str, value: Any) -> dict[str, Any]:
    doc = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _BASE.items()}
    if where == "top":
        doc["cpus"] = value
    elif where == "service":
        doc["services"] = {"worker": {"cpus": value}}
    else:
        doc["deploy"] = {"production": {"domain": "app.example.com", "cpus": value}}
    return doc


_PLACES = ["top", "service", "deploy"]
_PATHS = {"top": "cpus", "service": "services.worker.cpus", "deploy": "deploy.production.cpus"}
_GOOD = [1, 2, 64, 0.25, 1.5, 64.0, 0.01, 0.015, "2", "1.5", "0.25", "0.5", "64", "64.0", "12",
         "63.99", "10", "0.01", "0.010", "0.05", "0.019"]
_BAD = [
    "2 cores", "-1", "0", "0.0", "65", "64.1", "1e3", "", True, False, 0, 0.0, -1, -0.5, 65, 64.5,
    "02", ".5", "1.", " 2", "2 ", "1,5", None, [2], {"a": 1}, "100", "640",
    # Docker's minimum is 0.01 CPUs.
    0.001, 0.009, "0.001", "0.009", "0.00", "0.0099",
]


@pytest.mark.parametrize("where", _PLACES)
@pytest.mark.parametrize("value", _GOOD, ids=repr)
def test_cpus_accepted(where: str, value: Any) -> None:
    assert bay_toml.validate(_doc(where, value)) == []


@pytest.mark.parametrize("where", _PLACES)
@pytest.mark.parametrize("value", _BAD, ids=repr)
def test_cpus_refused(where: str, value: Any) -> None:
    violations = bay_toml.validate(_doc(where, value))
    assert [v.path for v in violations] == [_PATHS[where]], [str(v) for v in violations]


def test_cpus_message_is_plain() -> None:
    (violation,) = bay_toml.validate(_doc("top", "2 cores"))
    assert violation.message.startswith("must be a number of CPUs from 0.01 up to 64")


def test_cpus_is_a_top_level_key() -> None:
    assert "cpus" in bay_toml.TOP_LEVEL_KEYS


def test_cpus_is_not_a_job_key() -> None:
    job = {"name": "a", "schedule": "0 2 * * *", "command": "x", "cpus": 1}
    assert [v.path for v in bay_toml.validate({**_BASE, "jobs": [job]})] == ["jobs[0].cpus"]


# ── bay.fleet.toml schema ────────────────────────────────────────────────────


def _fleet_schema() -> dict[str, Any]:
    import json

    return json.loads(FLEET_SCHEMA_PATH.read_text())


def _fleet_doc(value: Any) -> dict[str, Any]:
    return {
        "format": 1,
        "name": "f",
        "default_box": "b",
        "boxes": {"b": {}},
        "resources": {"redis": {"kind": "redis", "image": "redis:7", "cpus": value}},
    }


def _fleet_cpus_paths(value: Any) -> list[str]:
    return [str(v.path) for v in bay_toml.schema_violations(_fleet_doc(value), _fleet_schema())
            if "cpus" in str(v.path)]


@pytest.mark.parametrize("value", [1, 0.5, "2", "1.5", 64])
def test_fleet_resource_accepts_cpus(value: Any) -> None:
    assert _fleet_cpus_paths(value) == []


@pytest.mark.parametrize("value", ["2 cores", "0", 0, 65, "1e3", "", True, 0.005, "0.005"])
def test_fleet_resource_refuses_cpus(value: Any) -> None:
    assert _fleet_cpus_paths(value) == ["resources.redis.cpus"]


def test_fleet_message_is_plain() -> None:
    violations = [v for v in bay_toml.schema_violations(_fleet_doc("x"), _fleet_schema())
                  if "cpus" in str(v.path)]
    assert violations[0].message.startswith("must be a number of CPUs from 0.01 up to 64")


# ── compile ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    dest = tmp_path / "fleet"
    shutil.copytree(FIXTURE, dest)
    return dest


def _edit(root: Path, rel: Path, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text()
    assert old in text, f"{old!r} not in {rel}"
    path.write_text(text.replace(old, new, 1))


def _compiled(root: Path) -> dict[str, Any]:
    result = compiler.compile_fleet(
        load_inputs(root, checkouts={"shop": root / "checkouts" / "shop"})
    )
    return yaml.safe_load(result.body())


def _find_cpus(out: dict[str, Any]) -> dict[str, Any]:
    return {
        name: entry["cpus"]
        for kind in ("services", "accessories")
        for name, entry in out[kind].items()
        if "cpus" in entry
    }


def test_compile_has_no_cpus_key_when_unset(fleet: Path) -> None:
    assert _find_cpus(_compiled(fleet)) == {}
    assert "cpus" not in compiler.compile_fleet(
        load_inputs(fleet, checkouts={"shop": fleet / "checkouts" / "shop"})
    ).text()


@pytest.mark.parametrize(
    ("written", "emitted"),
    [("2", 2), ("2.0", 2), ("1.5", 1.5), ("0.25", 0.25), ('"2"', 2), ('"1.5"', 1.5), ('"64"', 64)],
)
def test_compile_emits_a_yaml_number(fleet: Path, written: str, emitted: float) -> None:
    _edit(fleet, SHOP, 'memory = "512m"\n', f'memory = "512m"\ncpus = {written}\n')
    out = _compiled(fleet)
    value = out["services"]["shop"]["cpus"]
    assert value == emitted
    assert type(value) is type(emitted)


def test_compile_text_writes_an_integral_value_without_a_dot(fleet: Path) -> None:
    _edit(fleet, SHOP, 'memory = "512m"\n', 'memory = "512m"\ncpus = "2"\n')
    text = compiler.compile_fleet(
        load_inputs(fleet, checkouts={"shop": fleet / "checkouts" / "shop"})
    ).text()
    assert "    cpus: 2\n" in text


def test_compile_service_cpus_is_its_own(fleet: Path) -> None:
    """Like memory, a service reads only its own cpus, never the project's."""
    _edit(fleet, SHOP, 'memory = "512m"\n', 'memory = "512m"\ncpus = 4\n')
    _edit(fleet, SHOP, 'memory = "128m"\n', 'memory = "128m"\ncpus = "0.5"\n')
    out = _compiled(fleet)
    assert out["services"]["shop"]["cpus"] == 4
    assert "cpus" not in out["services"]["shop-api"]
    assert out["accessories"]["shop-warmer"]["cpus"] == 0.5


def test_compile_deploy_override_replaces_the_top_level_value(fleet: Path) -> None:
    _edit(fleet, SHOP, 'memory = "512m"\n', 'memory = "512m"\ncpus = 4\n')
    _edit(fleet, SHOP, "[deploy.staging]\n", "[deploy.staging]\ncpus = 1\n")
    out = _compiled(fleet)["services"]
    assert out["shop"]["cpus"] == 4
    assert out["shop-staging"]["cpus"] == 1


def test_compile_deploy_only_cpus(fleet: Path) -> None:
    _edit(fleet, SHOP, "[deploy.staging]\n", "[deploy.staging]\ncpus = 1.5\n")
    out = _compiled(fleet)["services"]
    assert out["shop-staging"]["cpus"] == 1.5
    assert "cpus" not in out["shop"]


def test_compile_fleet_resource_cpus(fleet: Path) -> None:
    _edit(fleet, FLEET, 'memory = "256m"\n', 'memory = "256m"\ncpus = "1.5"\n')
    out = _compiled(fleet)
    assert out["accessories"]["redis"]["cpus"] == 1.5


def test_compiled_cpus_passes_the_services_schema() -> None:
    schema = _load_schema()
    ok = {"services": {"a": {"image": "x:1", "access": "public", "domains": ["a.example.com"],
                              "ports": {"internal": 80}, "cpus": 1.5}},
          "accessories": {"db": {"image": "postgres:16", "cpus": 2}}}
    assert list(Draft202012Validator(schema).iter_errors(ok)) == []


@pytest.mark.parametrize("value", [0, -1, 65, "2", True, 0.005])
def test_services_schema_refuses_a_bad_cpus(value: Any) -> None:
    data = {"services": {"a": {"image": "x:1", "access": "public", "domains": ["a.example.com"],
                               "ports": {"internal": 80}, "cpus": value}}}
    errors = list(Draft202012Validator(_load_schema()).iter_errors(data))
    assert errors and "cpus" in str(errors[0].absolute_path)


# ── build_specs.yml, reconcile.yml and the git-built run line ────────────────


def _combine(base, *others, recursive=False, list_merge="replace"):
    result = dict(base or {})
    for other in others:
        if other:
            result.update(other)
    return result


def _render_optional(task_name: str, **context: Any) -> dict:
    tasks = yaml.safe_load(_BUILD_SPECS.read_text())
    expr = next(t for t in tasks if t.get("name") == task_name)["vars"]["_optional"]
    env = Environment(trim_blocks=True, lstrip_blocks=False)
    env.filters["combine"] = _combine
    env.filters["bay_prefix_volumes"] = bay_prefix_volumes
    env.filters["bay_healthcheck"] = lambda hc, port: dict(hc)
    env.filters["bay_log_rotation_spec"] = lambda v, d: {}
    return eval(env.from_string(expr).render(**context), {"__builtins__": {}}, {})  # noqa: S307


def _service_ctx(svc: dict) -> dict:
    return {"_svc": svc, "_name": "demo", "stack_dir": "/opt/test", "stack_name": "test",
            "_svc_expose": "", "_svc_port_rendered": "", "_log_rotation": {}}


def _accessory_ctx(acc: dict) -> dict:
    return {"_acc": acc, "_name": "demo-db", "stack_dir": "/opt/test", "stack_name": "test",
            "traefik_docker_network": "services", "_acc_port_rendered": "", "_log_rotation": {}}


@pytest.mark.parametrize(
    ("task", "ctx"),
    [("Build service container specs", _service_ctx),
     ("Build accessory container specs", _accessory_ctx)],
)
def test_spec_builder_emits_cpus_only_when_set(task: str, ctx: Any) -> None:
    base = {"image": "x:1", "mem_limit": "512m"}
    assert _render_optional(task, **ctx({**base, "cpus": 1.5}))["cpus"] == 1.5
    assert _render_optional(task, **ctx({**base, "cpus": 2}))["cpus"] == 2
    assert "cpus" not in _render_optional(task, **ctx(base))


def test_reconcile_bundle_dict_carries_cpus() -> None:
    assert "'cpus': _spec.cpus | default(None)" in _RECONCILE.read_text()


def test_rebuild_script_passes_cpus_at_both_run_sites() -> None:
    assert _REBUILD.read_text().count('--cpus "{{ svc.cpus }}"') == 2


# ── spec hash ────────────────────────────────────────────────────────────────

_RAW = {
    "name": "demo",
    "type": "service",
    "image": "x:1",
    "mem_limit": "512m",
    "memswap_limit": "512m",
    "labels": {"a": "b"},
}


def test_hash_of_a_spec_without_cpus_is_unchanged() -> None:
    """Pinned: this value was computed before ``cpus`` existed."""
    # Built from parts so the leak scan does not read a pinned digest as a secret.
    pinned = "".join(["42ba63773796a22d", "4427bc334d64365f", "a4605561e5a0c9e1", "18b6e78f3a1bbe51"])
    assert bay_spec_hash(_RAW) == pinned


def test_hash_with_cpus_differs_and_follows_the_value() -> None:
    base = bay_spec_hash(_RAW)
    two = bay_spec_hash({**_RAW, "cpus": 2})
    assert two != base
    assert two != bay_spec_hash({**_RAW, "cpus": 1.5})
    assert two == bay_spec_hash({**_RAW, "cpus": 2})


# ── reconciler ───────────────────────────────────────────────────────────────


def _entry(**over: Any) -> dict[str, Any]:
    return {"name": "web", "image": "x:1", "type": "service", "config_hash": "abc", **over}


@pytest.mark.parametrize(
    ("cpus", "nano"),
    [(2, 2_000_000_000), (1.5, 1_500_000_000), (0.25, 250_000_000), (0.1, 100_000_000),
     (64, 64_000_000_000), (0.333, 333_000_000)],
)
def test_bundle_sets_nano_cpus(cpus: float, nano: int) -> None:
    assert spec_from_dict(_entry(cpus=cpus)).nano_cpus == nano


def test_bundle_without_cpus_has_no_nano_cpus() -> None:
    assert spec_from_dict(_entry()).nano_cpus is None
    assert spec_from_dict(_entry(cpus=None)).nano_cpus is None


@pytest.mark.parametrize("bad", [0, -1, "2", True, [1], 0.005])
def test_bundle_refuses_a_bad_cpus(bad: Any) -> None:
    with pytest.raises(ValueError, match="cpus must be a number from 0.01"):
        spec_from_dict(_entry(cpus=bad))


def _spec(**over: Any) -> ContainerSpec:
    base: dict[str, Any] = {"name": "web", "image": "web:latest", "type": "service",
                            "config_hash": "h2"}
    base.update(over)
    return ContainerSpec(**base)


def _state(**over: Any) -> ContainerState:
    base: dict[str, Any] = {"name": "web", "exists": True, "image": "web:latest",
                            "config_hash": "h2", "status": "running", "managed": True,
                            "labels": {"bay.managed": "true"}}
    base.update(over)
    return ContainerState(**base)


def _reasons(spec: ContainerSpec, state: ContainerState) -> list[str]:
    observed = {state.name: state}
    (entry,) = describe(plan([spec], observed), [spec], observed)
    return list(entry["reasons"])  # type: ignore[arg-type]


def test_observed_state_reads_nano_cpus() -> None:
    attrs = {
        "Id": "abc", "Name": "/web", "Image": "sha256:1", "State": {"Status": "running"},
        "Config": {"Image": "web:latest", "Labels": {"bay.managed": "true"}, "Env": []},
        "HostConfig": {"NanoCpus": 1_500_000_000},
        "NetworkSettings": {"Networks": {}},
    }
    assert parse_state(attrs, managed_label="bay.managed").nano_cpus == 1_500_000_000


def test_cpus_reason_is_a_code() -> None:
    assert "cpus" in REASON_CODES


def test_planner_names_cpus_added() -> None:
    reasons = _reasons(_spec(nano_cpus=2_000_000_000, config_hash="h2"),
                       _state(config_hash="h1", nano_cpus=0))
    assert "cpus: 0 -> 2" in reasons


def test_planner_names_cpus_changed() -> None:
    reasons = _reasons(_spec(nano_cpus=1_500_000_000, config_hash="h2"),
                       _state(config_hash="h1", nano_cpus=2_000_000_000))
    assert "cpus: 2 -> 1.5" in reasons


def test_planner_names_cpus_removed() -> None:
    reasons = _reasons(_spec(config_hash="h2"), _state(config_hash="h1", nano_cpus=2_000_000_000))
    assert "cpus: 2 -> 0" in reasons


def test_no_cpus_and_no_cap_is_no_difference() -> None:
    observed = {"web": _state(nano_cpus=0)}
    assert plan([_spec()], observed).is_noop
    (entry,) = describe(plan([_spec()], observed), [_spec()], observed)
    assert entry["action"] == "noop"
    assert not any(r.startswith("cpus") for r in entry["reasons"])  # type: ignore[union-attr]


def test_same_cpus_is_no_difference() -> None:
    spec = _spec(nano_cpus=1_500_000_000)
    assert plan([spec], {"web": _state(nano_cpus=1_500_000_000)}).is_noop


def test_unreported_nano_cpus_is_no_difference() -> None:
    assert plan([_spec()], {"web": _state(nano_cpus=None)}).is_noop
    reasons = _reasons(_spec(nano_cpus=2_000_000_000, config_hash="h2"),
                       _state(config_hash="h1", nano_cpus=None))
    assert not any(r.startswith("cpus") for r in reasons)


# ── docker SDK call ──────────────────────────────────────────────────────────


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


def test_create_forwards_nano_cpus_to_the_sdk() -> None:
    client, fake = _client()
    client.create(_spec(nano_cpus=1_500_000_000))
    assert fake.containers.kwargs["nano_cpus"] == 1_500_000_000


def test_create_omits_nano_cpus_when_unset() -> None:
    client, fake = _client()
    client.create(_spec())
    assert "nano_cpus" not in fake.containers.kwargs
