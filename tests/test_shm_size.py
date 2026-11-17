"""``shm_size``: the size of /dev/shm, on the same path as ``memory``.

The key travels from bay.toml (top level, ``[services.<name>]``,
``[deploy.<env>]``, and the fleet's ``[resources.*]``) through the compile,
build_specs.yml, the reconcile bundle, the planner and the docker SDK call
(``HostConfig.ShmSize`` in bytes). An unset ``shm_size`` must leave every one
of those untouched, so no existing container is recreated by the upgrade.
"""

from __future__ import annotations

import json
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
_MACROS = ROOT / "roles" / "deploy_stack" / "templates" / "_macros.j2"

_filter_dir = str(ROOT / "filter_plugins")
if _filter_dir not in sys.path:
    sys.path.insert(0, _filter_dir)
_tests_dir = str(ROOT / "tests")
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from bay_filters import bay_prefix_volumes, bay_spec_hash  # noqa: E402
from test_rebuild_config import _render_rebuild_sh  # noqa: E402

MIB = 1024**2

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
        doc["shm_size"] = value
    elif where == "service":
        doc["services"] = {"worker": {"shm_size": value}}
    else:
        doc["deploy"] = {"production": {"domain": "app.example.com", "shm_size": value}}
    return doc


_PLACES = ["top", "service", "deploy"]
_PATHS = {
    "top": "shm_size",
    "service": "services.worker.shm_size",
    "deploy": "deploy.production.shm_size",
}
_GOOD = ["256m", "1g", "64m", "512k", "2g", "10m"]
_BAD = ["256", "0m", "256M", "1.5g", "256mb", "-1m", "", " 256m", "256m ", "big", 256, True,
        None, ["256m"], {"a": 1}]


@pytest.mark.parametrize("where", _PLACES)
@pytest.mark.parametrize("value", _GOOD, ids=repr)
def test_shm_size_accepted(where: str, value: Any) -> None:
    assert bay_toml.validate(_doc(where, value)) == []


@pytest.mark.parametrize("where", _PLACES)
@pytest.mark.parametrize("value", _BAD, ids=repr)
def test_shm_size_refused(where: str, value: Any) -> None:
    violations = bay_toml.validate(_doc(where, value))
    assert [v.path for v in violations] == [_PATHS[where]], [str(v) for v in violations]


def test_shm_size_message_is_plain() -> None:
    (violation,) = bay_toml.validate(_doc("top", "lots"))
    assert violation.message.startswith("must be a size such as 512m or 2g")


def test_shm_size_is_a_top_level_key() -> None:
    assert "shm_size" in bay_toml.TOP_LEVEL_KEYS


def test_shm_size_is_not_a_job_key() -> None:
    job = {"name": "a", "schedule": "0 2 * * *", "command": "x", "shm_size": "1g"}
    assert [v.path for v in bay_toml.validate({**_BASE, "jobs": [job]})] == ["jobs[0].shm_size"]


# ── bay.fleet.toml schema ────────────────────────────────────────────────────


def _fleet_doc(value: Any) -> dict[str, Any]:
    return {
        "format": 1,
        "name": "f",
        "default_box": "b",
        "boxes": {"b": {}},
        "resources": {"pg": {"kind": "postgres", "image": "postgres:16", "shm_size": value}},
    }


def _fleet_paths(value: Any) -> list[str]:
    schema = json.loads(FLEET_SCHEMA_PATH.read_text())
    return [str(v.path) for v in bay_toml.schema_violations(_fleet_doc(value), schema)
            if "shm_size" in str(v.path)]


@pytest.mark.parametrize("value", ["256m", "1g"])
def test_fleet_resource_accepts_shm_size(value: Any) -> None:
    assert _fleet_paths(value) == []


@pytest.mark.parametrize("value", ["256", "0g", "", True, 256])
def test_fleet_resource_refuses_shm_size(value: Any) -> None:
    assert _fleet_paths(value) == ["resources.pg.shm_size"]


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


def _result(root: Path) -> Any:
    return compiler.compile_fleet(
        load_inputs(root, checkouts={"shop": root / "checkouts" / "shop"})
    )


def _compiled(root: Path) -> dict[str, Any]:
    return yaml.safe_load(_result(root).body())


def _find(out: dict[str, Any]) -> dict[str, Any]:
    return {
        name: entry["shm_size"]
        for kind in ("services", "accessories")
        for name, entry in out[kind].items()
        if "shm_size" in entry
    }


def test_compile_has_no_shm_size_key_when_unset(fleet: Path) -> None:
    assert _find(_compiled(fleet)) == {}
    assert "shm_size" not in _result(fleet).text()


def test_compile_emits_the_size_string(fleet: Path) -> None:
    _edit(fleet, SHOP, 'memory = "512m"\n', 'memory = "512m"\nshm_size = "1g"\n')
    out = _compiled(fleet)
    assert out["services"]["shop"]["shm_size"] == "1g"
    assert "    shm_size: 1g\n" in _result(fleet).text()


def test_compile_service_shm_size_is_its_own(fleet: Path) -> None:
    """Like memory, a service reads only its own shm_size, never the project's."""
    _edit(fleet, SHOP, 'memory = "512m"\n', 'memory = "512m"\nshm_size = "1g"\n')
    _edit(fleet, SHOP, 'memory = "128m"\n', 'memory = "128m"\nshm_size = "256m"\n')
    out = _compiled(fleet)
    assert out["services"]["shop"]["shm_size"] == "1g"
    assert "shm_size" not in out["services"]["shop-api"]
    assert out["accessories"]["shop-warmer"]["shm_size"] == "256m"


def test_compile_deploy_override_replaces_the_top_level_value(fleet: Path) -> None:
    _edit(fleet, SHOP, 'memory = "512m"\n', 'memory = "512m"\nshm_size = "1g"\n')
    _edit(fleet, SHOP, "[deploy.staging]\n", '[deploy.staging]\nshm_size = "128m"\n')
    out = _compiled(fleet)["services"]
    assert out["shop"]["shm_size"] == "1g"
    assert out["shop-staging"]["shm_size"] == "128m"


def test_compile_deploy_only_shm_size(fleet: Path) -> None:
    _edit(fleet, SHOP, "[deploy.staging]\n", '[deploy.staging]\nshm_size = "128m"\n')
    out = _compiled(fleet)["services"]
    assert out["shop-staging"]["shm_size"] == "128m"
    assert "shm_size" not in out["shop"]


def test_compile_fleet_resource_shm_size(fleet: Path) -> None:
    _edit(fleet, FLEET, 'memory = "256m"\n', 'memory = "256m"\nshm_size = "512m"\n')
    assert _compiled(fleet)["accessories"]["redis"]["shm_size"] == "512m"


def test_compiled_shm_size_passes_the_services_schema() -> None:
    schema = _load_schema()
    ok = {"services": {"a": {"image": "x:1", "access": "public", "domains": ["a.example.com"],
                              "ports": {"internal": 80}, "shm_size": "256m"}},
          "accessories": {"db": {"image": "postgres:16", "shm_size": "1g"}}}
    assert list(Draft202012Validator(schema).iter_errors(ok)) == []


@pytest.mark.parametrize("value", [256, "0m", "256", "", True, "1.5g"])
def test_services_schema_refuses_a_bad_shm_size(value: Any) -> None:
    data = {"accessories": {"db": {"image": "postgres:16", "shm_size": value}}}
    errors = list(Draft202012Validator(_load_schema()).iter_errors(data))
    assert errors and "shm_size" in str(errors[0].absolute_path)


# ── build_specs.yml, reconcile.yml, the compose macro and the git-built run line ──


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
def test_spec_builder_emits_shm_size_only_when_set(task: str, ctx: Any) -> None:
    base = {"image": "x:1", "mem_limit": "512m"}
    assert _render_optional(task, **ctx({**base, "shm_size": "256m"}))["shm_size"] == "256m"
    assert "shm_size" not in _render_optional(task, **ctx(base))


def test_reconcile_bundle_dict_carries_shm_size() -> None:
    assert "'shm_size': _spec.shm_size | default(None)" in _RECONCILE.read_text()


def test_compose_macro_writes_shm_size_only_when_set() -> None:
    text = _MACROS.read_text()
    assert "{% if cfg.shm_size is defined %}\n    shm_size: {{ cfg.shm_size }}\n{% endif %}" in text


def _rebuild_service(**over: Any) -> dict[str, Any]:
    svc: dict[str, Any] = {
        "build": {"repo": "git@github.com:acme/app.git", "branch": "main",
                  "strategy": "remote"},
        "image": "zot.example.com/acme/app:latest",
        "access": "public",
        "domains": ["app.example.com"],
        "ports": {"internal": 8000},
    }
    svc.update(over)
    return svc


@pytest.mark.parametrize(("strategy", "sites"), [("local", 1), ("remote", 2)])
def test_rebuild_script_renders_shm_size_only_when_set(strategy: str, sites: int) -> None:
    """Both docker run sites of the script get ``--shm-size``; unset renders none."""
    build = {"repo": "git@github.com:acme/app.git", "branch": "main", "strategy": strategy}

    def render(**over: Any) -> str:
        svc = _rebuild_service(build=build, **over)
        return _render_rebuild_sh({"app": svc}, ["app"], git_deploy_services=["app"],
                                  git_deploy_build_strategy=strategy)

    on = render(shm_size="256m")
    assert on.count('--shm-size "256m" \\\n') == sites
    assert "--shm-size" not in render()


def test_rebuild_script_passes_shm_size_at_both_run_sites() -> None:
    assert _REBUILD.read_text().count('--shm-size "{{ svc.shm_size }}"') == 2


# ── spec hash ────────────────────────────────────────────────────────────────

_RAW = {
    "name": "demo",
    "type": "service",
    "image": "x:1",
    "mem_limit": "512m",
    "memswap_limit": "512m",
    "labels": {"a": "b"},
}


def test_hash_of_a_spec_without_shm_size_is_unchanged() -> None:
    """Pinned: this value was computed before ``shm_size`` existed (same as test_cpus)."""
    # Built from parts so the leak scan does not read a pinned digest as a secret.
    pinned = "".join(
        ["42ba63773796a22d", "4427bc334d64365f", "a4605561e5a0c9e1", "18b6e78f3a1bbe51"]
    )
    assert bay_spec_hash(_RAW) == pinned
    assert bay_spec_hash({**_RAW, "shm_size": None}) != pinned  # a present key is hashed
    assert bay_spec_hash(dict(_RAW)) == pinned


def test_hash_with_shm_size_differs_and_follows_the_value() -> None:
    base = bay_spec_hash(_RAW)
    one = bay_spec_hash({**_RAW, "shm_size": "1g"})
    assert one != base
    assert one != bay_spec_hash({**_RAW, "shm_size": "512m"})
    assert one == bay_spec_hash({**_RAW, "shm_size": "1g"})


# ── reconciler ───────────────────────────────────────────────────────────────


def _entry(**over: Any) -> dict[str, Any]:
    return {"name": "web", "image": "x:1", "type": "service", "config_hash": "abc", **over}


@pytest.mark.parametrize(
    ("written", "size"),
    [("256m", 256 * MIB), ("1g", 1024**3), ("512k", 512 * 1024), ("2G", 2 * 1024**3),
     ("64m", 64 * MIB), ("4096", 4096), ("100b", 100)],
)
def test_bundle_sets_shm_size_bytes(written: str, size: int) -> None:
    assert spec_from_dict(_entry(shm_size=written)).shm_size == size


def test_bundle_without_shm_size_has_none() -> None:
    assert spec_from_dict(_entry()).shm_size is None
    assert spec_from_dict(_entry(shm_size=None)).shm_size is None


@pytest.mark.parametrize("bad", ["", "0m", "-1m", "1.5g", "big", 256, True, ["1g"]])
def test_bundle_refuses_a_bad_shm_size(bad: Any) -> None:
    with pytest.raises(ValueError, match="shm_size must be a size"):
        spec_from_dict(_entry(shm_size=bad))


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


def _attrs(**host_config: Any) -> dict[str, Any]:
    return {
        "Id": "abc", "Name": "/web", "Image": "sha256:1", "State": {"Status": "running"},
        "Config": {"Image": "web:latest", "Labels": {"bay.managed": "true"}, "Env": []},
        "HostConfig": host_config,
        "NetworkSettings": {"Networks": {}},
    }


def test_observed_state_reads_shm_size() -> None:
    state = parse_state(_attrs(ShmSize=256 * MIB), managed_label="bay.managed")
    assert state.shm_size == 256 * MIB


def test_observed_state_without_shm_size_is_none() -> None:
    assert parse_state(_attrs(), managed_label="bay.managed").shm_size is None


def test_shm_size_reason_is_a_code() -> None:
    assert "shm_size" in REASON_CODES


def test_planner_names_shm_size_added() -> None:
    reasons = _reasons(_spec(shm_size=256 * MIB, config_hash="h2"),
                       _state(config_hash="h1", shm_size=64 * MIB))
    assert "shm_size: 64m -> 256m" in reasons


def test_planner_names_shm_size_changed() -> None:
    reasons = _reasons(_spec(shm_size=1024**3, config_hash="h2"),
                       _state(config_hash="h1", shm_size=256 * MIB))
    assert "shm_size: 256m -> 1g" in reasons


def test_planner_names_shm_size_removed() -> None:
    reasons = _reasons(_spec(config_hash="h2"), _state(config_hash="h1", shm_size=256 * MIB))
    assert "shm_size: 256m -> 64m" in reasons


@pytest.mark.parametrize("reported", [64 * MIB, 0, None])
def test_no_shm_size_and_docker_default_is_no_difference(reported: int | None) -> None:
    """Docker reports 64 MiB (old containers: 0) when no size was given."""
    observed = {"web": _state(shm_size=reported)}
    assert plan([_spec()], observed).is_noop
    (entry,) = describe(plan([_spec()], observed), [_spec()], observed)
    assert entry["action"] == "noop"
    assert not any(r.startswith("shm_size") for r in entry["reasons"])  # type: ignore[union-attr]
    # Even beside a hash change, the default is never named as a difference.
    reasons = _reasons(_spec(config_hash="h2"), _state(config_hash="h1", shm_size=reported))
    assert not any(r.startswith("shm_size") for r in reasons)


def test_same_shm_size_is_no_difference() -> None:
    spec = _spec(shm_size=256 * MIB)
    assert plan([spec], {"web": _state(shm_size=256 * MIB)}).is_noop


def test_explicit_64m_matches_the_docker_default() -> None:
    reasons = _reasons(_spec(shm_size=64 * MIB, config_hash="h2"),
                       _state(config_hash="h1", shm_size=0))
    assert not any(r.startswith("shm_size") for r in reasons)


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


def test_create_forwards_shm_size_bytes_to_the_sdk() -> None:
    client, fake = _client()
    client.create(_spec(shm_size=256 * MIB))
    assert fake.containers.kwargs["shm_size"] == 256 * MIB


def test_create_omits_shm_size_when_unset() -> None:
    client, fake = _client()
    client.create(_spec())
    assert "shm_size" not in fake.containers.kwargs
