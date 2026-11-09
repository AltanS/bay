"""`dns` in bay.toml: resolvers for one container (docker --dns).

Why it exists: a scraper browser must not use the box resolver, which sends
some names to the tailnet split DNS. The key travels from bay.toml through the
validator, the compile, services.yml, build_specs.yml, the reconcile bundle and
the Docker SDK call.

The rule that matters most: a service WITHOUT `dns` is untouched. Its compiled
services.yml, its container spec, its config hash and its plan are the same as
before this key existed, so no running container is recreated by the release.
The Docker SDK call is checked by capturing the kwargs passed to
`containers.run`. No daemon is needed.
"""

from __future__ import annotations

import copy
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
from bay_cli.fleet import load_inputs
from bay_reconcile import ContainerState, plan
from bay_reconcile.bundle import spec_from_dict
from bay_reconcile.models import ContainerSpec
from bay_reconcile.observe import parse_state
from bay_reconcile.planner import REASON_CODES, describe
from bay_reconcile.sdk_client import SdkDockerClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILD_SPECS = _REPO_ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
_RECONCILE = _REPO_ROOT / "roles" / "container_lifecycle" / "tasks" / "reconcile.yml"
_MACROS = _REPO_ROOT / "roles" / "deploy_stack" / "templates" / "_macros.j2"
_REBUILD = _REPO_ROOT / "roles" / "git_deploy" / "templates" / "rebuild.sh.j2"
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "fleet_min"
_SHOP = Path("checkouts") / "shop" / "bay.toml"

_filter_dir = str(_REPO_ROOT / "filter_plugins")
if _filter_dir not in sys.path:
    sys.path.insert(0, _filter_dir)

from bay_filters import bay_prefix_volumes, bay_spec_hash  # noqa: E402

RESOLVERS = ["1.1.1.1", "8.8.8.8"]

# ── bay.toml validation ──────────────────────────────────────────────────

BASE: dict[str, Any] = {
    "name": "myapp",
    "fleet": "myfleet",
    "port": 3000,
    "access": {"mode": "public"},
    "deploy": {"production": {"domain": "app.example.com"}},
}


def _doc(**patch: Any) -> dict[str, Any]:
    doc = copy.deepcopy(BASE)
    doc.update(patch)
    return doc


def _paths(doc: dict[str, Any]) -> dict[str, str]:
    return {v.path: v.message for v in bay_toml.validate(doc)}


@pytest.mark.parametrize(
    "value",
    [
        ["1.1.1.1"],
        ["1.1.1.1", "8.8.8.8"],
        ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"],
        ["::1"],
    ],
)
def test_valid_dns_lists_pass_on_the_main_table_a_service_and_a_deploy_env(value):
    assert bay_toml.validate(_doc(dns=value)) == []
    assert bay_toml.validate(_doc(services={"steel": {"inherit": False, "image": "x:1",
                                                      "dns": value}})) == []
    deploy = {"production": {"domain": "app.example.com", "dns": value}}
    assert bay_toml.validate(_doc(deploy=deploy)) == []


def test_absent_dns_is_valid():
    assert bay_toml.validate(_doc()) == []


@pytest.mark.parametrize(
    ("value", "path", "words"),
    [
        (["dns.example.com"], "dns[0]", "not an IP address"),
        (["1.1.1.1", "one.one.one.one"], "dns[1]", "domain name"),
        (["999.1.1.1"], "dns[0]", "not an IP address"),
        (["fe80::1%eth0"], "dns[0]", "not an IP address"),
        ([""], "dns[0]", "not an IP address"),
        ([], "dns", "at least one"),
        (["1.1.1.1", "1.1.1.1"], "dns", "same resolver twice"),
        (["1.1.1.1", "8.8.8.8", "9.9.9.9", "8.8.4.4"], "dns", "at most 3"),
        ("1.1.1.1", "dns", "a list"),
        ([1], "dns[0]", "a string"),
    ],
)
def test_bad_dns_is_rejected_with_a_clear_message(value, path, words):
    found = _paths(_doc(dns=value))
    assert path in found, found
    assert words in found[path] or words == "at most 3" and "3 resolvers" in found[path]


def test_the_limit_message_says_why():
    found = _paths(_doc(dns=["1.1.1.1", "8.8.8.8", "9.9.9.9", "8.8.4.4"]))
    assert "glibc" in found["dns"] and "3" in found["dns"]


def test_a_bad_entry_in_a_service_and_a_deploy_env_names_its_table():
    doc = _doc(services={"steel": {"image": "x:1", "dns": ["host.example.com"]}})
    assert "services.steel.dns[0]" in _paths(doc)
    deploy = {"production": {"domain": "app.example.com", "dns": ["host.example.com"]}}
    assert "deploy.production.dns[0]" in _paths(_doc(deploy=deploy))


# ── compile ──────────────────────────────────────────────────────────────


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    dest = tmp_path / "fleet"
    shutil.copytree(_FIXTURE, dest)
    return dest


def _compile(root: Path) -> compiler.CompileResult:
    checkouts = {"shop": root / "checkouts" / "shop"}
    return compiler.compile_fleet(load_inputs(root, checkouts=checkouts))


def _edit(root: Path, old: str, new: str) -> None:
    path = root / _SHOP
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new, 1))


def test_compile_without_dns_has_no_dns_key_anywhere(fleet):
    body = _compile(fleet).body()
    assert "dns" not in body


def test_compile_main_container_dns(fleet):
    _edit(fleet, 'memory = "512m"\n', 'memory = "512m"\ndns = ["1.1.1.1", "8.8.8.8"]\n')
    out = yaml.safe_load(_compile(fleet).body())
    assert out["services"]["shop"]["dns"] == RESOLVERS
    # per container: the staging main container and the sidecars do not inherit it
    assert out["services"]["shop-staging"]["dns"] == RESOLVERS, "same project value per env"
    assert "dns" not in out["services"]["shop-api"]
    assert "dns" not in out["accessories"]["shop-warmer"]


def test_compile_a_deploy_env_overrides_dns(fleet):
    _edit(fleet, 'memory = "512m"\n', 'memory = "512m"\ndns = ["1.1.1.1"]\n')
    _edit(fleet, 'branch = "develop"\n', 'branch = "develop"\ndns = ["9.9.9.9", "1.1.1.1"]\n')
    out = yaml.safe_load(_compile(fleet).body())
    assert out["services"]["shop"]["dns"] == ["1.1.1.1"]
    assert out["services"]["shop-staging"]["dns"] == ["9.9.9.9", "1.1.1.1"]


def test_compile_sidecar_with_inherit_false_gets_dns_and_keeps_order(fleet):
    _edit(fleet, 'command = "warm --every 5m"\n',
          'command = "warm --every 5m"\ndns = ["8.8.8.8", "1.1.1.1"]\n')
    out = yaml.safe_load(_compile(fleet).body())
    assert out["accessories"]["shop-warmer"]["dns"] == ["8.8.8.8", "1.1.1.1"]
    assert "dns" not in out["services"]["shop"]


def test_compile_routed_service_dns(fleet):
    _edit(fleet, "replicas = 2\n", 'replicas = 2\ndns = ["1.1.1.1"]\n')
    out = yaml.safe_load(_compile(fleet).body())
    assert out["services"]["shop-api"]["dns"] == ["1.1.1.1"]
    assert "dns" not in out["services"]["shop"]


def test_compile_only_adds_the_one_key(fleet):
    """Control: the file with dns differs from the one without by the dns lines only."""
    before = _compile(fleet).body().splitlines()
    _edit(fleet, 'command = "warm --every 5m"\n',
          'command = "warm --every 5m"\ndns = ["1.1.1.1", "8.8.8.8"]\n')
    after = _compile(fleet).body().splitlines()
    added = [line for line in after if line not in before]
    added = [ln.strip() for ln in added if "sha256" not in ln]
    # One three-line block per warmer container (one per environment), nothing else.
    warmers = sum("warm --every" in line for line in after)
    assert warmers >= 1
    assert added == ["dns:", "- 1.1.1.1", "- 8.8.8.8"] * warmers


# ── services.yml schema ──────────────────────────────────────────────────


def _schema_errors(svc_extra: dict, acc_extra: dict | None = None) -> list:
    data = {
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
    return list(Draft202012Validator(_load_schema()).iter_errors(data))


def test_services_schema_accepts_dns_on_service_and_accessory():
    assert _schema_errors({"dns": ["1.1.1.1"]}, {"dns": ["1.1.1.1", "8.8.8.8"]}) == []


@pytest.mark.parametrize("bad", [[], "1.1.1.1", ["a", "b", "c", "d"], ["1.1.1.1", "1.1.1.1"], [1]])
def test_services_schema_rejects_a_bad_dns(bad):
    assert _schema_errors({"dns": ["1.1.1.1"]}) == []
    assert _schema_errors({"dns": bad}) != []
    assert _schema_errors({}, {"dns": bad}) != []


# ── build_specs.yml, reconcile.yml, compose macro, rebuild.sh ────────────


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
def test_spec_builder_emits_dns_only_when_set(task, ctx):
    base = {"image": "x:1", "mem_limit": "512m"}
    with_key = _render_optional(task, **ctx({**base, "dns": RESOLVERS}))
    assert with_key["dns"] == RESOLVERS
    # Control: without the key the spec has no such key at all, which is what
    # keeps the config hash of every existing container unchanged.
    without = _render_optional(task, **ctx(base))
    assert "dns" not in without
    assert without == {k: v for k, v in with_key.items() if k != "dns"}


def test_reconcile_bundle_dict_carries_dns():
    assert "'dns': _spec.dns | default(None)" in _RECONCILE.read_text()


def test_compose_macro_renders_dns():
    text = _MACROS.read_text()
    assert "{% if cfg.dns is defined %}" in text
    assert "      - {{ resolver }}" in text


def test_rebuild_script_passes_dns_at_both_run_sites():
    """rebuild.sh recreates git-built services with `docker run`; without --dns
    a push build would silently drop the resolvers the deploy set."""
    text = _REBUILD.read_text()
    assert text.count('--dns "{{ resolver }}"') == 2
    assert text.count("{% for resolver in svc.dns | default([]) %}") == 2


# ── config hash ──────────────────────────────────────────────────────────

# Computed with the filter as it was BEFORE `dns` existed (git HEAD of 2.13.0).
_SPEC_NO_DNS = {
    "name": "steel",
    "type": "service",
    "image": "ghcr.io/steel-dev/steel-browser:latest",
    "restart_policy": "unless-stopped",
    "networks": ["services"],
    "labels": {"a": "b"},
    "mem_limit": "512m",
    "memswap_limit": "512m",
    "env_file": "/opt/x/env/steel.env",
}
# The first 24 hex digits of the 2.13.0 hash (a full digest trips the leak scan).
_GOLDEN_NO_DNS = "62aad7dfe5cf0c086841392e"


def test_hash_of_a_spec_without_dns_is_the_pre_dns_hash():
    assert bay_spec_hash(_SPEC_NO_DNS, env_digest="abc").startswith(_GOLDEN_NO_DNS)


def test_hash_changes_with_dns_and_with_its_order():
    plain = bay_spec_hash(_SPEC_NO_DNS, env_digest="abc")
    one = bay_spec_hash({**_SPEC_NO_DNS, "dns": RESOLVERS}, env_digest="abc")
    two = bay_spec_hash({**_SPEC_NO_DNS, "dns": RESOLVERS[::-1]}, env_digest="abc")
    assert len({plain, one, two}) == 3
    assert one == bay_spec_hash({**_SPEC_NO_DNS, "dns": list(RESOLVERS)}, env_digest="abc")


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
        "name": "steel",
        "image": "alpine:3.20",
        "type": "service",
        "config_hash": "h2",
    }
    base.update(over)
    return ContainerSpec(**base)


def _entry(**over: Any) -> dict[str, Any]:
    return {"name": "steel", "image": "x:1", "type": "service", "config_hash": "abc", **over}


def test_bundle_parses_dns_in_order():
    assert spec_from_dict(_entry(dns=["8.8.8.8", "1.1.1.1"])).dns == ("8.8.8.8", "1.1.1.1")


@pytest.mark.parametrize("absent", [None, []])
def test_bundle_without_dns_is_empty(absent):
    entry = _entry() if absent is None else _entry(dns=absent)
    assert spec_from_dict(entry).dns == ()


@pytest.mark.parametrize("bad", ["1.1.1.1", [1], [""], 5])
def test_bundle_refuses_a_malformed_dns(bad):
    with pytest.raises(ValueError, match="dns"):
        spec_from_dict(_entry(dns=bad))


def test_create_forwards_dns_to_the_sdk():
    client, fake = _client()
    client.create(_spec(dns=("1.1.1.1", "8.8.8.8")))
    assert fake.containers.kwargs["dns"] == ["1.1.1.1", "8.8.8.8"]


def test_create_omits_dns_when_unset():
    """Control: no `dns` kwarg reaches Docker, so existing containers are created as before."""
    client, fake = _client()
    client.create(_spec())
    assert "dns" not in fake.containers.kwargs


# ── observe + planner ────────────────────────────────────────────────────


def _attrs(**host_config: Any) -> dict[str, Any]:
    return {
        "Name": "/steel",
        "Config": {"Image": "x:1", "Labels": {"bay.managed": "true"}},
        "State": {"Status": "running"},
        "HostConfig": host_config,
    }


def test_observe_reads_host_config_dns_in_order():
    state = parse_state(_attrs(Dns=["8.8.8.8", "1.1.1.1"]), managed_label="bay.managed")
    assert state.dns == ("8.8.8.8", "1.1.1.1")


@pytest.mark.parametrize("none_set", [None, []])
def test_observe_reads_no_dns_as_empty(none_set):
    assert parse_state(_attrs(Dns=none_set), managed_label="bay.managed").dns == ()


def test_observe_unreported_dns_is_unknown():
    assert parse_state(_attrs(), managed_label="bay.managed").dns is None


def _state(**kw: Any) -> ContainerState:
    base: dict[str, Any] = {
        "name": "steel",
        "exists": True,
        "image": "alpine:3.20",
        "config_hash": "h1",
        "status": "running",
        "managed": True,
        "env": {},
        "labels": {},
        "volumes": (),
    }
    base.update(kw)
    return ContainerState(**base)


def _one(spec: ContainerSpec, state: ContainerState) -> dict[str, object]:
    observed = {state.name: state}
    out = describe(plan([spec], observed), [spec], observed)
    assert len(out) == 1
    return out[0]


def test_dns_is_a_reason_code():
    assert "dns" in REASON_CODES


def test_adding_dns_recreates_and_names_dns():
    # The hash differs (dns is hashed); the running container reports no resolvers.
    entry = _one(_spec(dns=("1.1.1.1", "8.8.8.8")), _state(dns=()))
    assert entry["action"] == "recreate"
    assert "dns: docker's default -> 1.1.1.1, 8.8.8.8" in entry["reasons"]


def test_changing_and_reordering_dns_names_dns():
    entry = _one(_spec(dns=("8.8.8.8", "1.1.1.1")), _state(dns=("1.1.1.1", "8.8.8.8")))
    assert entry["action"] == "recreate"
    assert "dns: 1.1.1.1, 8.8.8.8 -> 8.8.8.8, 1.1.1.1" in entry["reasons"]


def test_removing_dns_recreates_and_names_dns():
    entry = _one(_spec(), _state(dns=("1.1.1.1",)))
    assert entry["action"] == "recreate"
    assert "dns: 1.1.1.1 -> docker's default" in entry["reasons"]


def test_matching_dns_is_not_named_and_a_matching_hash_is_a_noop():
    spec = _spec(config_hash="h1", dns=("1.1.1.1", "8.8.8.8"))
    entry = _one(spec, _state(dns=("1.1.1.1", "8.8.8.8")))
    assert entry == {"name": "steel", "action": "noop", "reasons": []}
    changed = _one(_spec(dns=("1.1.1.1", "8.8.8.8")), _state(dns=("1.1.1.1", "8.8.8.8")))
    assert not any(str(r).startswith("dns") for r in changed["reasons"])  # type: ignore[union-attr]


def test_a_spec_without_dns_on_an_existing_container_is_unchanged():
    """No dns wanted, none running (null or [] or unreported): same plan and no dns reason."""
    for observed in (None, ()):
        spec = _spec(config_hash="h1")
        assert _one(spec, _state(dns=observed))["action"] == "noop"
        entry = _one(_spec(), _state(dns=observed))
        assert not any(str(r).startswith("dns") for r in entry["reasons"])  # type: ignore[union-attr]


def test_rebuild_dns_snippet_renders_one_flag_per_entry_and_nothing_when_absent():
    text = _REBUILD.read_text()
    start = text.index("{% for resolver in svc.dns | default([]) %}")
    end = text.index("{% endfor %}", start) + len("{% endfor %}")
    env = Environment(trim_blocks=True)
    template = env.from_string(text[start:end] + "\n")
    with_dns = template.render(svc={"dns": ["8.8.8.8", "1.1.1.1"]})
    assert with_dns == '      --dns "8.8.8.8" \\\n      --dns "1.1.1.1" \\\n'
    assert template.render(svc={}) == ""
