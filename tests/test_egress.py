"""`egress = "public"` in bay.toml: a service that may reach the public internet only.

Why it exists: a headless browser (Steel) renders third-party pages. It must
never reach the tailnet, private ranges, the box, the other containers or the
fleet's own boxes. Bay runs it alone on its own Docker network
(`services-egress-<container>`, bridge `bayeg<8 hex>`), behind an nftables
table that only matches those bridges, and attaches the project's main
container to that network so the app can still call it.

The key travels: bay.toml validator -> compile -> services.yml (accessory
`egress`) -> container_lifecycle egress.yml (networks, firewall) ->
build_specs.yml -> the reconcile bundle -> the SDK create/connect/start, and
the rebuild.sh push path. The rule that matters most: a container WITHOUT
egress is untouched. Its compiled entry, its spec, its config hash, its SDK
call and its plan are the same as before, so the release recreates nothing.
"""

from __future__ import annotations

import copy
import ipaddress
import re
import shutil
import subprocess
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
from bay_reconcile import egressnet as en
from bay_reconcile.bundle import spec_from_dict
from bay_reconcile.models import ContainerSpec, Recreate
from bay_reconcile.observe import parse_state
from bay_reconcile.planner import REASON_CODES, describe
from bay_reconcile.sdk_client import SdkDockerClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLE = _REPO_ROOT / "roles" / "container_lifecycle"
_BUILD_SPECS = _ROLE / "tasks" / "build_specs.yml"
_RECONCILE = _ROLE / "tasks" / "reconcile.yml"
_MAIN = _ROLE / "tasks" / "main.yml"
_EGRESS = _ROLE / "tasks" / "egress.yml"
_PRUNE = _ROLE / "tasks" / "egress_prune.yml"
_TEMPLATE_DIR = _ROLE / "templates"
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "fleet_min"
_SHOP = Path("checkouts") / "shop" / "bay.toml"

_filter_dir = str(_REPO_ROOT / "filter_plugins")
if _filter_dir not in sys.path:
    sys.path.insert(0, _filter_dir)
_tests_dir = str(_REPO_ROOT / "tests")
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from bay_filters import (  # noqa: E402
    bay_egress_blocked_v4,
    bay_egress_bridge,
    bay_egress_client_networks,
    bay_egress_network,
    bay_egress_sidecars,
    bay_prefix_volumes,
    bay_spec_hash,
)

STEEL: dict[str, Any] = {
    "inherit": False,
    "image": "ghcr.io/steel-dev/steel-browser:latest",
    "port": 3000,
    "dns": ["1.1.1.1", "9.9.9.9"],
    "egress": "public",
}

# ── bay.toml validation ──────────────────────────────────────────────────

BASE: dict[str, Any] = {
    "name": "myapp",
    "fleet": "myfleet",
    "port": 3000,
    "access": {"mode": "public"},
    "deploy": {"production": {"domain": "app.example.com"}},
}


def _doc(steel: dict[str, Any] | None = None, **patch: Any) -> dict[str, Any]:
    doc = copy.deepcopy(BASE)
    doc["services"] = {"steel": {**STEEL, **(steel or {})}}
    doc.update(patch)
    return doc


def _without(key: str) -> dict[str, Any]:
    doc = _doc()
    del doc["services"]["steel"][key]
    return doc


def _paths(doc: dict[str, Any]) -> dict[str, str]:
    return {v.path: v.message for v in bay_toml.validate(doc)}


def test_the_steel_example_is_valid():
    assert bay_toml.validate(_doc()) == []


def test_steel_with_a_worker_client_and_loopback_expose_is_valid():
    doc = _doc({"egress_clients": ["worker"], "expose": "loopback", "update": "off"})
    doc["services"]["worker"] = {"image": "x:1", "command": "work"}
    assert bay_toml.validate(doc) == []


@pytest.mark.parametrize(
    ("doc", "path", "words"),
    [
        (_without("port"), "services.steel.port", "required"),
        (_doc({"domain": "steel.example.com"}), "services.steel.domain", "no route"),
        (_doc({"path": "/steel"}), "services.steel.path", "no route"),
        (_doc({"expose": "tailnet"}), "services.steel.expose", "loopback"),
        (_doc({"needs": ["postgres"]}), "services.steel.needs", "private address"),
        (_doc({"needs": {"redis": {}}}), "services.steel.needs", "private address"),
        (_without("inherit"), "services.steel.inherit", "must be false"),
        (_doc({"inherit": True}), "services.steel.inherit", "must be false"),
        (_without("dns"), "services.steel.dns", "100.100.100.100"),
        (_doc({"dns": ["100.100.100.100"]}), "services.steel.dns[0]", "tailnet"),
        (_doc({"dns": ["1.1.1.1", "10.0.0.2"]}), "services.steel.dns[1]", "private"),
        (_doc({"dns": ["192.168.1.1"]}), "services.steel.dns[0]", "private"),
        (_doc({"dns": ["2606:4700:4700::1111"]}), "services.steel.dns[0]", "IPv6"),
        (_doc({"update": "auto"}), "services.steel.update", "Watchtower"),
        (_doc({"egress": "private"}), "services.steel.egress", '"public"'),
        (_doc({"egress_clients": ["steel"]}), "services.steel.egress_clients[0]", "own client"),
        (_doc({"egress_clients": ["ghost"]}), "services.steel.egress_clients[0]", "no service"),
        (_doc({"egress_clients": []}), "services.steel.egress_clients", "at least one"),
    ],
)
def test_an_unusable_egress_service_is_refused_with_a_clear_message(doc, path, words):
    found = _paths(doc)
    assert path in found, found
    assert words in found[path], found[path]


def test_an_inherited_auto_update_is_refused_and_names_where_it_comes_from():
    found = _paths(_doc(update="auto"))
    assert "inherited from the top level" in found["services.steel.update"]
    doc = _doc()
    doc["deploy"]["production"]["update"] = "auto"
    assert "deploy.production" in _paths(doc)["services.steel.update"]
    # An own notify or off overrides the inherited auto.
    assert bay_toml.validate(_doc({"update": "notify"}, update="auto")) == []


def test_egress_clients_on_a_service_without_egress_is_refused():
    doc = _doc()
    doc["services"]["worker"] = {"image": "x:1", "egress_clients": ["steel"]}
    assert "services.worker.egress_clients" in _paths(doc)


def test_another_egress_service_cannot_be_a_client():
    doc = _doc({"egress_clients": ["scraper"]})
    doc["services"]["scraper"] = {**STEEL, "port": 3001}
    assert "egress service itself" in _paths(doc)["services.steel.egress_clients[0]"]


def test_egress_is_not_a_key_of_the_main_container_or_a_deploy_env():
    assert "egress" in _paths(_doc(egress="public"))
    doc = _doc()
    doc["deploy"]["production"]["egress"] = "public"
    assert "deploy.production.egress" in _paths(doc)


def test_the_validator_and_the_firewall_block_the_same_ranges():
    for resolver in ("1.1.1.1", "9.9.9.9", "8.8.8.8"):
        assert not en.is_blocked_v4(resolver)
    for blocked in ("100.100.100.100", "10.1.2.3", "172.18.0.5", "127.0.0.1", "169.254.169.254"):
        assert en.is_blocked_v4(blocked)
        assert any(ipaddress.ip_address(blocked) in ipaddress.ip_network(n)
                   for n in bay_egress_blocked_v4())


# ── compile ──────────────────────────────────────────────────────────────

_STEEL_TOML = """
[services.steel]
inherit = false
image = "ghcr.io/steel-dev/steel-browser:latest"
port = 3000
dns = ["1.1.1.1", "9.9.9.9"]
update = "notify"
"""


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    dest = tmp_path / "fleet"
    shutil.copytree(_FIXTURE, dest)
    return dest


def _compile(root: Path) -> compiler.CompileResult:
    checkouts = {"shop": root / "checkouts" / "shop"}
    return compiler.compile_fleet(load_inputs(root, checkouts=checkouts))


def _append(root: Path, text: str) -> None:
    path = root / _SHOP
    path.write_text(path.read_text() + text)


def test_compile_without_egress_has_no_egress_key_anywhere(fleet):
    assert "egress" not in _compile(fleet).body()


def test_compile_steel_is_an_accessory_with_its_clients(fleet):
    _append(fleet, _STEEL_TOML + 'egress = "public"\n')
    out = yaml.safe_load(_compile(fleet).body())
    assert out["accessories"]["shop-steel"]["egress"] == {
        "mode": "public", "port": 3000, "clients": ["shop"],
    }
    assert out["accessories"]["shop-staging-steel"]["egress"]["clients"] == ["shop-staging"]
    assert out["accessories"]["shop-steel"]["dns"] == ["1.1.1.1", "9.9.9.9"]
    assert "egress" not in out["services"]["shop"]


def test_compile_egress_clients_adds_the_sibling_containers(fleet):
    _append(fleet, _STEEL_TOML + 'egress = "public"\negress_clients = ["api"]\n')
    out = yaml.safe_load(_compile(fleet).body())
    assert out["accessories"]["shop-steel"]["egress"]["clients"] == ["shop", "shop-api"]
    staging = out["accessories"]["shop-staging-steel"]["egress"]["clients"]
    assert staging == ["shop-staging", "shop-staging-api"]


def test_compile_egress_only_adds_the_egress_block(fleet):
    """Control: the same service with and without egress differs by the egress lines only."""
    _append(fleet, _STEEL_TOML)
    before = _compile(fleet).body().splitlines()
    _append(fleet, 'egress = "public"\n')
    after = _compile(fleet).body().splitlines()
    # The bay.toml hash lines move with any edit of the file; skip them.
    added = [ln.strip() for ln in after if ln not in before and "sha256" not in ln]
    removed = [ln for ln in before if ln not in after and "sha256" not in ln]
    assert removed == []
    assert sorted(set(added)) == sorted(
        {"egress:", "clients:", "- shop", "- shop-staging", "mode: public", "port: 3000"}
    )


# ── services.yml schema ──────────────────────────────────────────────────


def _schema_errors(acc_extra: dict) -> list:
    data = {
        "services": {},
        "accessories": {"steel": {"image": "x:1", **acc_extra}},
    }
    return list(Draft202012Validator(_load_schema()).iter_errors(data))


def test_services_schema_accepts_egress_on_an_accessory():
    egress = {"mode": "public", "port": 3000, "clients": ["shop"]}
    assert _schema_errors({"egress": egress}) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "private", "port": 3000, "clients": ["a"]},
        {"mode": "public", "clients": ["a"]},
        {"mode": "public", "port": 3000, "clients": []},
        {"mode": "public", "port": 3000, "clients": ["a"], "extra": 1},
        "public",
    ],
)
def test_services_schema_refuses_a_bad_egress(bad):
    assert _schema_errors({"egress": bad}) != []


# ── filters ──────────────────────────────────────────────────────────────

_ACCESSORIES = {
    "shop-steel": {"image": "s:1", "egress": {"mode": "public", "port": 3000,
                                              "clients": ["shop", "shop-api"]}},
    "shop-redis": {"image": "redis:7"},
}


def test_filter_names_are_the_box_module_names():
    assert bay_egress_network("shop-steel") == en.network_name("shop-steel")
    assert bay_egress_network("shop-steel") == "services-egress-shop-steel"
    assert bay_egress_network("shop-steel") > "services"
    bridge = bay_egress_bridge("shop-steel")
    assert bridge == en.bridge_name("shop-steel")
    assert bridge.startswith("bayeg") and len(bridge) == 13  # IFNAMSIZ is 16 with the NUL


def test_filter_sidecars_and_clients():
    assert bay_egress_sidecars(_ACCESSORIES) == ["shop-steel"]
    assert bay_egress_sidecars({}) == []
    assert bay_egress_client_networks("shop", _ACCESSORIES) == ["services-egress-shop-steel"]
    assert bay_egress_client_networks("shop-api", _ACCESSORIES) == ["services-egress-shop-steel"]
    assert bay_egress_client_networks("shop-redis", _ACCESSORIES) == []
    assert bay_egress_client_networks("shop", None) == []


def test_blocked_set_merges_box_addresses_and_skips_names_and_ipv6():
    out = bay_egress_blocked_v4(["203.0.113.11", "box.example.com", "10.1.2.3", "2001:db8::1", ""])
    assert "203.0.113.11/32" in out
    assert "10.0.0.0/8" in out and "10.1.2.3/32" not in out
    assert not any(":" in n for n in out)
    nets = [ipaddress.ip_network(n) for n in out]
    assert not any(a.overlaps(b) for i, a in enumerate(nets) for b in nets[i + 1:])


# ── build_specs.yml and reconcile.yml ────────────────────────────────────


def _combine(base, *others, recursive=False, list_merge="replace"):
    result = dict(base or {})
    for other in others:
        if other:
            result.update(other)
    return result


def _render_optional(task_name: str, **context) -> dict:
    tasks = yaml.safe_load(_BUILD_SPECS.read_text())
    task_vars = next(t for t in tasks if t.get("name") == task_name)["vars"]
    env = Environment(trim_blocks=True, lstrip_blocks=False)
    env.filters["combine"] = _combine
    env.filters["bay_prefix_volumes"] = bay_prefix_volumes
    env.filters["bay_healthcheck"] = lambda hc, port: dict(hc)
    env.filters["bay_log_rotation_spec"] = lambda v, d: {}
    env.filters["bay_egress_client_networks"] = bay_egress_client_networks
    env.filters["bay_egress_network"] = bay_egress_network
    ctx = dict(context)
    for key in ("_egress_client_nets", "_egress_net", "_egress_addr"):
        if key in task_vars:
            text = env.from_string(task_vars[key]).render(**ctx)
            ctx[key] = eval(text) if key == "_egress_client_nets" else text  # noqa: S307
    rendered = env.from_string(task_vars["_optional"]).render(**ctx)
    return eval(rendered, {"__builtins__": {}}, {})  # noqa: S307 - test-local literal


def _service_ctx(name: str, svc: dict, accessories: dict) -> dict:
    return {
        "_svc": svc, "_name": name, "stack_dir": "/opt/test", "stack_name": "test",
        "_svc_expose": "", "_svc_port_rendered": "", "_log_rotation": {},
        "active_accessories": accessories, "traefik_docker_network": "services",
    }


def _accessory_ctx(name: str, acc: dict, accessories: dict, nets: dict | None = None) -> dict:
    ctx = {
        "_acc": acc, "_name": name, "stack_dir": "/opt/test", "stack_name": "test",
        "traefik_docker_network": "services", "_acc_port_rendered": "", "_log_rotation": {},
        "active_accessories": accessories,
    }
    if nets is not None:
        ctx["_egress_nets"] = nets
    return ctx


_NETS = {"shop-steel": {"address": "172.31.244.18"}}


def test_a_client_service_joins_the_egress_network_after_services():
    spec = _render_optional("Build service container specs",
                            **_service_ctx("shop", {"image": "x:1"}, _ACCESSORIES))
    assert spec["networks"] == ["services", "services-egress-shop-steel"]


def test_a_service_that_is_no_client_has_no_networks_override():
    """Control: the base spec's one network stays, so its config hash stays."""
    svc = {"image": "x:1", "mem_limit": "512m"}
    with_egress = _render_optional("Build service container specs",
                                   **_service_ctx("other", svc, _ACCESSORIES))
    without = _render_optional("Build service container specs",
                               **_service_ctx("other", svc, {}))
    assert "networks" not in with_egress
    assert with_egress == without


def test_the_egress_accessory_is_alone_pinned_and_hardened():
    acc = _ACCESSORIES["shop-steel"]
    spec = _render_optional("Build accessory container specs",
                            **_accessory_ctx("shop-steel", acc, _ACCESSORIES, _NETS))
    assert spec["networks"] == ["services-egress-shop-steel"]
    assert spec["network_ips"] == {"services-egress-shop-steel": "172.31.244.18"}
    assert spec["cap_drop"] == ["NET_RAW"]
    assert spec["security_opt"] == ["no-new-privileges:true"]


def test_a_partial_pass_without_addresses_builds_no_network_ips():
    acc = _ACCESSORIES["shop-steel"]
    spec = _render_optional("Build accessory container specs",
                            **_accessory_ctx("shop-steel", acc, _ACCESSORIES))
    assert spec["networks"] == ["services-egress-shop-steel"]
    assert "network_ips" not in spec


def test_an_accessory_client_joins_and_a_plain_accessory_is_untouched():
    accs = {**_ACCESSORIES, "shop": {"image": "x:1"}}
    client = _render_optional("Build accessory container specs",
                              **_accessory_ctx("shop", accs["shop"], accs, _NETS))
    assert client["networks"] == ["services", "services-egress-shop-steel"]
    plain_with = _render_optional("Build accessory container specs",
                                  **_accessory_ctx("shop-redis", accs["shop-redis"], accs, _NETS))
    plain_without = _render_optional("Build accessory container specs",
                                     **_accessory_ctx("shop-redis", accs["shop-redis"], {}))
    assert plain_with == plain_without
    assert not {"network_ips", "cap_drop", "security_opt"} & set(plain_with)


def test_reconcile_bundle_dict_carries_the_new_keys():
    text = _RECONCILE.read_text()
    for key in ("network_ips", "cap_drop", "security_opt"):
        assert f"'{key}': _spec.{key} | default(None)" in text


# ── config hash ──────────────────────────────────────────────────────────

# The spec and golden prefix of test_dns.py (the 2.13.0 hash, before dns and
# egress existed): a spec without the new keys hashes as it always did.
_SPEC_PLAIN = {
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
_GOLDEN_PLAIN = "62aad7dfe5cf0c086841392e"


def test_hash_of_a_spec_without_egress_keys_is_unchanged():
    assert bay_spec_hash(_SPEC_PLAIN, env_digest="abc").startswith(_GOLDEN_PLAIN)


def test_hash_changes_with_each_egress_key():
    plain = bay_spec_hash(_SPEC_PLAIN, env_digest="abc")
    variants = [
        {**_SPEC_PLAIN, "networks": ["services", "services-egress-x"]},
        {**_SPEC_PLAIN, "network_ips": {"services": "172.31.240.2"}},
        {**_SPEC_PLAIN, "cap_drop": ["NET_RAW"]},
        {**_SPEC_PLAIN, "security_opt": ["no-new-privileges:true"]},
    ]
    hashes = {bay_spec_hash(v, env_digest="abc") for v in variants}
    assert plain not in hashes and len(hashes) == len(variants)


# ── bundle ───────────────────────────────────────────────────────────────


def _entry(**over: Any) -> dict[str, Any]:
    return {"name": "steel", "image": "x:1", "type": "accessory", "config_hash": "abc", **over}


def test_bundle_parses_the_egress_keys():
    spec = spec_from_dict(_entry(
        networks=["services-egress-steel"],
        network_ips={"services-egress-steel": "172.31.240.2"},
        cap_drop=["NET_RAW"],
        security_opt=["no-new-privileges:true"],
    ))
    assert spec.network_ips == {"services-egress-steel": "172.31.240.2"}
    assert spec.cap_drop == ("NET_RAW",)
    assert spec.security_opt == ("no-new-privileges:true",)
    assert spec.wires_networks


def test_bundle_without_the_keys_is_empty_and_not_wired():
    spec = spec_from_dict(_entry(networks=["services"], network_ips=None, cap_drop=None))
    assert spec.network_ips == {} and spec.cap_drop == () and spec.security_opt == ()
    assert not spec.wires_networks


@pytest.mark.parametrize(
    "bad",
    [
        {"network_ips": {"other-net": "172.31.240.2"}},
        {"network_ips": ["172.31.240.2"]},
        {"cap_drop": "NET_RAW"},
        {"security_opt": [""]},
    ],
)
def test_bundle_refuses_malformed_egress_keys(bad):
    with pytest.raises(ValueError):
        spec_from_dict(_entry(networks=["services-egress-steel"], **bad))


# ── SDK create ───────────────────────────────────────────────────────────


class _Ctr:
    def __init__(self, log: list) -> None:
        self.log = log

    def start(self) -> None:
        self.log.append(("start",))

    def remove(self, force: bool = False) -> None:
        self.log.append(("remove", force))


class _Net:
    def __init__(self, name: str, log: list, fail: bool = False) -> None:
        self.name, self.log, self.fail = name, log, fail

    def connect(self, ctr: Any, **kw: Any) -> None:
        if self.fail:
            raise RuntimeError(f"cannot connect {self.name}")
        self.log.append(("connect", self.name, kw.get("ipv4_address")))

    def disconnect(self, ctr: Any) -> None:
        self.log.append(("disconnect", self.name))


class _Fake:
    def __init__(self, fail_net: str | None = None) -> None:
        self.log: list = []
        self.kwargs: dict[str, Any] = {}
        fake = self

        class _Containers:
            def run(self, **kw: Any) -> None:
                fake.kwargs = kw
                fake.log.append(("run",))

            def create(self, **kw: Any) -> _Ctr:
                fake.kwargs = kw
                fake.log.append(("create", kw.get("network")))
                return _Ctr(fake.log)

        class _Networks:
            def get(self, name: str) -> _Net:
                return _Net(name, fake.log, fail=name == fail_net)

        self.containers = _Containers()
        self.networks = _Networks()


def _client(fail_net: str | None = None) -> tuple[SdkDockerClient, _Fake]:
    client = SdkDockerClient.__new__(SdkDockerClient)
    fake = _Fake(fail_net)
    client._c = fake
    client._managed_label = "bay.managed"
    client._stack_label = "bay.stack"
    client._stack = "test"
    return client, fake


def _spec(**over: Any) -> ContainerSpec:
    base: dict[str, Any] = {"name": "steel", "image": "x:1", "type": "accessory",
                            "config_hash": "h2"}
    base.update(over)
    return ContainerSpec(**base)


def test_a_single_network_spec_is_still_one_run_call():
    """Control: every existing container is created exactly as before."""
    client, fake = _client()
    client.create(_spec(networks=("services",)))
    assert fake.log == [("run",)]
    assert fake.kwargs["network"] == "services"
    assert not {"cap_drop", "security_opt"} & set(fake.kwargs)


def test_the_egress_container_is_created_pinned_then_started():
    client, fake = _client()
    net = "services-egress-steel"
    client.create(_spec(networks=(net,), network_ips={net: "172.31.240.2"},
                        cap_drop=("NET_RAW",), security_opt=("no-new-privileges:true",)))
    assert fake.log == [
        ("create", net),
        ("disconnect", net),
        ("connect", net, "172.31.240.2"),
        ("start",),
    ]
    assert "detach" not in fake.kwargs
    assert fake.kwargs["cap_drop"] == ["NET_RAW"]
    assert fake.kwargs["security_opt"] == ["no-new-privileges:true"]


def test_a_client_gets_every_network_before_it_starts():
    client, fake = _client()
    client.create(_spec(type="service", networks=("services", "services-egress-steel")),
                  name_override="shop-new")
    assert fake.log == [
        ("create", "services"),
        ("connect", "services-egress-steel", None),
        ("start",),
    ]
    assert fake.kwargs["name"] == "shop-new"


def test_a_failed_connect_removes_the_half_made_container():
    client, fake = _client(fail_net="services-egress-steel")
    with pytest.raises(RuntimeError, match="cannot connect"):
        client.create(_spec(type="service", networks=("services", "services-egress-steel")))
    assert ("start",) not in fake.log
    assert fake.log[-1] == ("remove", True)


# ── observe + planner ────────────────────────────────────────────────────


def _attrs(networks: Any = "absent") -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "Name": "/shop",
        "Config": {"Image": "x:1", "Labels": {"bay.managed": "true"}},
        "State": {"Status": "running"},
        "HostConfig": {},
        "NetworkSettings": {},
    }
    if networks != "absent":
        attrs["NetworkSettings"]["Networks"] = networks
    return attrs


def test_observe_reads_networks_and_fixed_addresses():
    nets = {
        "services": {"IPAMConfig": None, "IPAddress": "172.18.0.4"},
        "services-egress-steel": {"IPAMConfig": {"IPv4Address": "172.31.240.2"}},
    }
    state = parse_state(_attrs(nets), managed_label="bay.managed")
    assert state.networks == {"services": "", "services-egress-steel": "172.31.240.2"}


def test_observe_unreported_networks_is_unknown():
    assert parse_state(_attrs(), managed_label="bay.managed").networks is None
    assert parse_state(_attrs(None), managed_label="bay.managed").networks == {}


def _state(**kw: Any) -> ContainerState:
    base: dict[str, Any] = {"name": "shop", "exists": True, "image": "x:1",
                            "config_hash": "h1", "status": "running", "managed": True,
                            "env": {}, "labels": {}, "volumes": ()}
    base.update(kw)
    return ContainerState(**base)


def _one(spec: ContainerSpec, state: ContainerState) -> dict[str, object]:
    observed = {state.name: state}
    out = describe(plan([spec], observed), [spec], observed)
    assert len(out) == 1
    return out[0]


_CLIENT_NETS = ("services", "services-egress-steel")


def test_networks_is_a_reason_code():
    assert "networks" in REASON_CODES


def test_a_plain_spec_ignores_its_networks_when_the_hash_matches():
    """Control: no existing container recreates over a network it was attached to by hand."""
    spec = _spec(name="shop", type="service", config_hash="h1", networks=("services",))
    state = _state(networks={"services": "", "debug-net": ""})
    assert _one(spec, state)["action"] == "noop"


def test_a_client_that_lost_the_egress_network_is_recreated():
    spec = _spec(name="shop", type="service", config_hash="h1", networks=_CLIENT_NETS)
    entry = _one(spec, _state(networks={"services": ""}))
    assert entry["action"] == "recreate"
    assert "networks: not attached to services-egress-steel" in entry["reasons"]


def test_a_client_with_both_networks_is_a_noop():
    spec = _spec(name="shop", type="service", config_hash="h1", networks=_CLIENT_NETS)
    entry = _one(spec, _state(networks={"services": "", "services-egress-steel": ""}))
    assert entry == {"name": "shop", "action": "noop", "reasons": []}


def test_a_zero_downtime_client_takes_the_canary_path_on_network_drift():
    spec = _spec(name="shop", type="service", config_hash="h1", networks=_CLIENT_NETS,
                 zero_downtime=True)
    the_plan = plan([spec], {"shop": _state(networks={"services": ""})})
    assert type(the_plan.actions[0]).__name__ == "CanarySwap"


def test_the_egress_container_recreates_when_its_address_moved():
    net = "services-egress-steel"
    spec = _spec(name="steel", config_hash="h1", networks=(net,),
                 network_ips={net: "172.31.240.2"})
    state = _state(name="steel", networks={net: "172.31.240.9"})
    the_plan = plan([spec], {"steel": state})
    assert isinstance(the_plan.actions[0], Recreate)
    reasons = _one(spec, state)["reasons"]
    assert f"networks: address on {net} 172.31.240.9 -> 172.31.240.2" in reasons


def test_a_hash_change_names_the_network_difference_for_any_spec():
    spec = _spec(name="shop", type="service", config_hash="h2", networks=("services",))
    entry = _one(spec, _state(networks={"services": "", "services-egress-steel": ""}))
    assert entry["action"] == "recreate"
    assert "networks: also attached to services-egress-steel" in entry["reasons"]


def test_network_mode_specs_are_never_compared():
    spec = _spec(name="traefik", type="infra", config_hash="h1", network_mode="host")
    assert _one(spec, _state(name="traefik", networks={"host": ""}))["action"] == "noop"


# ── egressnet: subnet choice, readback, prune ────────────────────────────

POOL = ipaddress.IPv4Network("172.31.240.0/20")


def test_allocate_is_deterministic_and_inside_the_pool():
    a = en.allocate("services-egress-shop-steel", POOL, [])
    assert a == en.allocate("services-egress-shop-steel", POOL, [])
    assert a.subnet_of(POOL) and a.prefixlen == 28
    others = {en.allocate(f"services-egress-n{i}", POOL, []) for i in range(20)}
    assert len(others) > 1, "the start slot depends on the name"


def test_allocate_skips_used_subnets_and_wraps():
    name = "services-egress-shop-steel"
    first = en.allocate(name, POOL, [])
    second = en.allocate(name, POOL, [first])
    assert second != first and not second.overlaps(first)
    # A route that covers the whole pool but one /28 leaves exactly that one.
    slots = en.candidates(POOL)
    free = slots[0]
    assert en.allocate(name, POOL, slots[1:]) == free


def test_allocate_fails_when_the_pool_is_full():
    with pytest.raises(en.EgressNetError, match="no free"):
        en.allocate("x", POOL, [POOL])


def test_layout_pins_the_container_outside_the_dynamic_range():
    lay = en.layout(ipaddress.IPv4Network("172.31.240.16/28"))
    assert lay == {"subnet": "172.31.240.16/28", "gateway": "172.31.240.17",
                   "address": "172.31.240.18", "ip_range": "172.31.240.24/29"}
    assert ipaddress.ip_address(lay["address"]) not in ipaddress.ip_network(lay["ip_range"])


def test_host_routes_parses_proc_net_route():
    text = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "br-1\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "wg0\t00F01FAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    )
    routes = en.host_routes(text)
    assert routes == [ipaddress.IPv4Network("172.18.0.0/16"),
                      ipaddress.IPv4Network("172.31.240.0/24")]


def _net_attrs(name: str, subnet: str, *, container: str | None = None,
               stack: str = "test", bridge: str | None = None) -> dict[str, Any]:
    labels = {en.LABEL: container, en.STACK_LABEL: stack} if container else {}
    options = {en.BRIDGE_OPTION: bridge or (en.bridge_name(container) if container else "br-x")}
    return {"Name": name, "Labels": labels, "Options": options, "EnableIPv6": False,
            "IPAM": {"Config": [{"Subnet": subnet}]}}


def test_plan_reads_an_existing_network_back_and_never_moves_it():
    existing = [_net_attrs("services-egress-steel", "172.31.250.32/28", container="steel")]
    out = en.plan_networks(["steel"], existing, [], POOL, "test")
    assert out["steel"]["subnet"] == "172.31.250.32/28"
    assert out["steel"]["address"] == "172.31.250.34"
    assert out["steel"]["create"] is False
    assert out["steel"]["bridge"] == en.bridge_name("steel")


def test_plan_skips_subnets_of_other_networks_and_routes():
    name = en.network_name("steel")
    first = en.allocate(name, POOL, [])
    existing = [_net_attrs("other", str(first))]
    out = en.plan_networks(["steel"], existing, [], POOL, "test")
    assert out["steel"]["create"] is True
    assert not ipaddress.IPv4Network(out["steel"]["subnet"]).overlaps(first)
    routed = en.plan_networks(["steel"], [], [first], POOL, "test")
    assert routed["steel"]["subnet"] == out["steel"]["subnet"]


def test_two_new_networks_in_one_run_never_collide():
    out = en.plan_networks([f"c{i}" for i in range(40)], [], [], POOL, "test")
    subnets = [ipaddress.IPv4Network(e["subnet"]) for e in out.values()]
    assert len(set(subnets)) == 40


@pytest.mark.parametrize(
    "attrs",
    [
        _net_attrs("services-egress-steel", "172.31.240.0/28"),  # no labels
        _net_attrs("services-egress-steel", "172.31.240.0/28", container="other"),
        _net_attrs("services-egress-steel", "172.31.240.0/28", container="steel", stack="b"),
        _net_attrs("services-egress-steel", "172.31.240.0/28", container="steel",
                   bridge="br-123"),
        _net_attrs("services-egress-steel", "172.31.240.0/24", container="steel"),
        {**_net_attrs("services-egress-steel", "172.31.240.0/28", container="steel"),
         "EnableIPv6": True},
    ],
)
def test_a_network_bay_cannot_trust_stops_the_deploy(attrs):
    with pytest.raises(en.EgressNetError):
        en.plan_networks(["steel"], [attrs], [], POOL, "test")


def test_prunable_names_only_this_stacks_unwanted_networks():
    existing = [
        _net_attrs("services-egress-a", "172.31.240.0/28", container="a"),
        _net_attrs("services-egress-b", "172.31.240.16/28", container="b"),
        _net_attrs("services-egress-c", "172.31.240.32/28", container="c", stack="other"),
        _net_attrs("services", "172.18.0.0/16"),
    ]
    assert en.prunable(existing, ["a"], "test") == ["services-egress-b"]


class _NetObj:
    def __init__(self, attrs: dict[str, Any], store: _NetStore) -> None:
        self.attrs, self.store = attrs, store

    def remove(self) -> None:
        self.store.removed.append(self.attrs["Name"])


class _NetStore:
    def __init__(self, attrs: list[dict[str, Any]]) -> None:
        self.items = {a["Name"]: a for a in attrs}
        self.created: list[dict[str, Any]] = []
        self.removed: list[str] = []

    def list(self) -> list[_NetObj]:
        return [_NetObj(a, self) for a in self.items.values()]

    def get(self, name: str) -> _NetObj:
        return _NetObj(self.items[name], self)

    def create(self, name: str, **kw: Any) -> None:
        self.created.append({"name": name, **kw})
        self.items[name] = {"Name": name, "Labels": kw["labels"], "Options": kw["options"],
                            "EnableIPv6": kw["enable_ipv6"], "IPAM": dict(kw["ipam"])}


class _NetClient:
    def __init__(self, attrs: list[dict[str, Any]] | None = None) -> None:
        self.networks = _NetStore(attrs or [])


def test_ensure_creates_the_network_with_bridge_name_labels_and_range():
    client = _NetClient()
    out = en.ensure(client, ["shop-steel"], POOL, "test", [])
    entry = out["shop-steel"]
    assert entry["created"] is True
    made = client.networks.created[0]
    assert made["name"] == "services-egress-shop-steel"
    assert made["options"] == {en.BRIDGE_OPTION: en.bridge_name("shop-steel")}
    assert made["labels"] == {en.LABEL: "shop-steel", en.STACK_LABEL: "test"}
    assert made["enable_ipv6"] is False
    pool_cfg = dict(made["ipam"])["Config"][0]
    assert pool_cfg["Subnet"] == entry["subnet"]
    assert pool_cfg["Gateway"] == entry["gateway"]
    assert pool_cfg["IPRange"] == entry["ip_range"]
    # A second run reads it back and creates nothing.
    again = en.ensure(client, ["shop-steel"], POOL, "test", [])
    assert again["shop-steel"]["created"] is False
    assert again["shop-steel"]["subnet"] == entry["subnet"]
    assert len(client.networks.created) == 1


def test_ensure_dry_run_creates_nothing_and_says_would():
    client = _NetClient()
    out = en.ensure(client, ["shop-steel"], POOL, "test", [], dry_run=True)
    assert out["shop-steel"]["created"] == "would"
    assert client.networks.created == []


def test_prune_removes_unused_and_keeps_networks_in_use():
    a = _net_attrs("services-egress-a", "172.31.240.0/28", container="a")
    b = {**_net_attrs("services-egress-b", "172.31.240.16/28", container="b"),
         "Containers": {"id": {"Name": "b"}}}
    client = _NetClient([a, b])
    assert en.prune(client, [], "test") == {"removed": ["services-egress-a"],
                                             "kept": ["services-egress-b"]}


# ── the nftables file ────────────────────────────────────────────────────

_RULES = [
    {"container": "anchorgenius-steel", "bridge": en.bridge_name("anchorgenius-steel"),
     "address": "172.31.244.18", "port": 3000},
    {"container": "other-steel", "bridge": en.bridge_name("other-steel"),
     "address": "172.31.250.2", "port": 9222},
]


def _render_nft(rules: list[dict[str, Any]] = _RULES) -> str:
    from helpers import make_ansible_env

    env = make_ansible_env(_TEMPLATE_DIR)
    return env.get_template("bay-egress.conf.j2").render(
        ansible_managed="test",
        _egress_rules=rules,
        _egress_blocked=bay_egress_blocked_v4(["203.0.113.11", "203.0.113.12"]),
    )


def test_nft_file_replaces_only_its_own_tables():
    text = _render_nft()
    assert "flush ruleset" not in text
    for family in ("inet", "bridge"):
        assert f"table {family} bay_egress {{}}\ndelete table {family} bay_egress" in text
    tables = [ln for ln in text.splitlines() if ln.startswith("table ")]
    assert {t.split()[1] + " " + t.split()[2] for t in tables} == {
        "inet bay_egress", "bridge bay_egress"}


def test_nft_file_rules():
    text = _render_nft()
    for rule in _RULES:
        bridge, addr, port = rule["bridge"], rule["address"], rule["port"]
        assert (f'iifname "{bridge}" oifname "{bridge}" ip daddr {addr} tcp dport {port} '
                "ct state new accept") in text
        assert f'meta ibrname "{bridge}" ip daddr {addr} tcp dport {port} accept' in text
        assert (f'meta ibrname "{bridge}" ip saddr {addr} tcp sport {port} '
                "tcp flags & (syn | ack) != syn accept") in text
    assert "type filter hook forward priority -5; policy accept;" in text
    assert "type filter hook input priority -5; policy accept;" in text
    assert 'iifname "bayeg*" jump from_egress' in text
    assert "203.0.113.11/32" in text and "100.64.0.0/10" in text
    assert text.count("meta nfproto ipv6 counter drop") == 2
    assert "ether type ip6 counter drop" in text


def test_nft_base_chains_jump_only_from_egress_bridges():
    """Every base chain has policy accept and only jumps on a bayeg* match, so no other packet changes."""
    text = _render_nft()
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith("type filter hook"):
            assert "policy accept" in line
            body = []
            for nxt in lines[i + 1:]:
                if nxt == "}":
                    break
                body.append(nxt)
            assert body and all("bayeg*" in b and " jump " in f" {b}" for b in body), body


def _userns_nft() -> bool:
    if not shutil.which("nft") or not shutil.which("unshare"):
        return False
    probe = subprocess.run(["unshare", "-rn", "nft", "list", "ruleset"],
                           capture_output=True, text=True, check=False)
    return probe.returncode == 0


needs_nft = pytest.mark.skipif(not _userns_nft(),
                               reason="needs nft and an unprivileged user+net namespace")


@needs_nft
def test_nft_file_passes_nft_check_and_loads_twice(tmp_path):
    path = tmp_path / "bay-egress.conf"
    path.write_text(_render_nft())
    check = subprocess.run(["unshare", "-rn", "nft", "-c", "-f", str(path)],
                           capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stderr
    # In a throwaway netns: load, load again (the replace idiom), then remove
    # with the same stdin the prune task sends, twice (it must be idempotent).
    removal = next(t for t in yaml.safe_load(_PRUNE.read_text())
                   if t.get("block"))["block"][2]["ansible.builtin.command"]["stdin"]
    script = (
        f"nft -f {path} && nft -f {path} && nft list table inet bay_egress >/dev/null "
        f"&& printf '%s' \"$REMOVAL\" | nft -f - && printf '%s' \"$REMOVAL\" | nft -f - "
        "&& ! nft list table inet bay_egress 2>/dev/null"
    )
    run = subprocess.run(["unshare", "-rn", "sh", "-c", script], capture_output=True,
                         text=True, check=False, env={"REMOVAL": removal, "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    assert run.returncode == 0, run.stderr


# ── tasks wiring ─────────────────────────────────────────────────────────


def _names(path: Path) -> list[str]:
    return [t["name"] for t in yaml.safe_load(path.read_text())]


def test_main_runs_egress_before_specs_and_reconcile_and_prune_after():
    tasks = yaml.safe_load(_MAIN.read_text())
    includes = [t.get("ansible.builtin.include_tasks") for t in tasks]
    assert includes == ["egress.yml", "build_specs.yml", "reconcile.yml", "egress_prune.yml"]
    full_pass = "container_lifecycle_only | default([]) | length == 0"
    assert tasks[0]["when"] == full_pass
    prune_when = tasks[3]["when"]
    assert full_pass in prune_when and "not ansible_check_mode" in prune_when


def test_egress_tasks_create_networks_then_load_and_check_the_firewall():
    tasks = yaml.safe_load(_EGRESS.read_text())
    names = [t["name"] for t in tasks]
    assert names.index("Ensure the egress networks") < names.index("Install the egress firewall")
    fw = next(t for t in tasks if t["name"] == "Install the egress firewall")
    assert fw["become"] is True and fw["become_user"] == "root"
    inner = [t["name"] for t in fw["block"]]
    order = ["Render the egress firewall", "Load the egress firewall",
             "Check that the egress firewall is live for every egress container"]
    assert [inner.index(n) for n in order] == sorted(inner.index(n) for n in order)
    render = next(t for t in fw["block"] if t["name"] == "Render the egress firewall")
    assert render["ansible.builtin.template"]["validate"] == "nft -c -f %s"
    assert render["ansible.builtin.template"]["dest"] == "{{ _egress_conf }}"
    assert fw["vars"]["_egress_conf"] == "/etc/nftables.d/bay-egress.conf"
    ensure = next(t for t in tasks if t["name"] == "Ensure the egress networks")
    assert ensure["check_mode"] is False
    assert "{{ _egress_dry_flag }}" in ensure["ansible.builtin.script"]["cmd"]
    assert "--dry-run" in ensure["vars"]["_egress_dry_flag"]
    assert "ansible_check_mode" in ensure["vars"]["_egress_dry_flag"]


def _walk_tasks(tasks):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk_tasks(task.get(key))


def test_no_script_or_command_line_breaks_outside_jinja():
    """A folded scalar keeps a newline after a more-indented line. In a shell
    command that newline starts a second command: a live test box ran
    `--dry-run` as its own command, so `bay plan --remote` created egress
    networks."""
    role_tasks = Path(_EGRESS).parent
    for path in sorted(role_tasks.glob("*.yml")):
        for task in _walk_tasks(yaml.safe_load(path.read_text())):
            # `script` hands its line to a shell, so a newline splits it.
            # (`shell` tasks may hold a multi-line script on purpose.)
            body = task.get("ansible.builtin.script")
            cmd = body.get("cmd") if isinstance(body, dict) else body
            if not isinstance(cmd, str):
                continue
            outside = re.sub(r"\{\{.*?\}\}", "", cmd.rstrip("\n"), flags=re.S)
            assert "\n" not in outside, f"{path.name}: {task.get('name')}"


def test_every_root_task_of_the_prune_names_root():
    tasks = yaml.safe_load(_PRUNE.read_text())
    stat = tasks[0]
    assert stat["become_user"] == "root"
    block = tasks[1]["block"]
    for task in block:
        if "ansible.builtin.script" in task or "ansible.builtin.debug" in task:
            continue
        assert task.get("become_user") == "root", task["name"]


# ── rebuild.sh push path ─────────────────────────────────────────────────


def _rebuild(accessories: dict) -> str:
    from test_rebuild_config import _mixed_services, _render_rebuild_sh

    return _render_rebuild_sh(
        _mixed_services(), ["animals", "localapp"], git_deploy_services=["localapp"],
        accessories=accessories,
    )


def test_rebuild_attaches_the_egress_network_before_start_at_both_run_sites():
    accs = {"steel": {"image": "s:1", "egress": {"mode": "public", "port": 3000,
                                                 "clients": ["animals", "localapp"]}}}
    text = _rebuild(accs)
    for svc in ("animals", "localapp"):
        connect = f'docker network connect "services-egress-steel" "{svc}"'
        start = f'docker start "{svc}" >/dev/null'
        assert text.count(connect) == 1 and text.count(start) == 1
        assert text.index(connect) < text.index(start)
    assert text.count("    docker create \\") == 2
    assert text.count(" || return 1\n") >= 2
    subprocess.run(["bash", "-n"], input=text, text=True, check=True)


def test_rebuild_without_egress_is_byte_identical_to_no_accessories():
    """Control: a push of a container that is no client renders exactly as before."""
    accs = {"redis": {"image": "redis:7"}}
    assert _rebuild(accs) == _rebuild({})
    text = _rebuild({})
    assert "docker network connect" not in text
    assert "docker create" not in text


# ── the rules on real packets (throwaway network namespaces) ─────────────

_NETNS_SCRIPT = r"""
set -e
mount -t tmpfs none /run
mkdir -p /run/netns
ip link set lo up
sysctl -qw net.ipv4.ip_forward=1
for n in steel app priv pub; do ip netns add $n; done
ip link add "$BR" type bridge
ip addr add 172.31.240.1/28 dev "$BR"
ip link set "$BR" up
for n in steel app; do
  ip link add v$n type veth peer name eth0 netns $n
  ip link set v$n master "$BR" up
  ip -n $n link set eth0 up
  ip -n $n link set lo up
done
ip -n steel addr add 172.31.240.2/28 dev eth0
ip -n steel route add default via 172.31.240.1
ip -n app addr add 172.31.240.8/28 dev eth0
for n in priv:10.9.0 pub:198.51.100; do
  name=${n%%:*}; net=${n#*:}
  ip link add v$name type veth peer name eth0 netns $name
  ip addr add $net.1/24 dev v$name
  ip link set v$name up
  ip -n $name addr add $net.2/24 dev eth0
  ip -n $name link set eth0 up
  ip -n $name route add default via $net.1
done
for n in priv pub app; do
  ip netns exec $n python3 -m http.server 8080 --bind 0.0.0.0 >/dev/null 2>&1 &
done
for port in 3000 8080; do
  ip netns exec steel python3 -m http.server $port --bind 0.0.0.0 >/dev/null 2>&1 &
done
python3 -m http.server 9999 --bind 0.0.0.0 >/dev/null 2>&1 &
sleep 1.5
probe() {
  if ip netns exec $1 timeout 1 bash -c "cat < /dev/null > /dev/tcp/$2/$3" 2>/dev/null
  then echo "open $1 $2:$3"; else echo "blocked $1 $2:$3"; fi
}
# Control: before the rules everything is open, so each "blocked" below is the rules' doing.
for target in "steel 10.9.0.2 8080" "steel 172.31.240.1 9999" "steel 172.31.240.8 8080" \
              "app 172.31.240.2 8080"; do
  probe $target | sed 's/^/before /'
done
nft -f "$FW"
probe steel 198.51.100.2 8080
probe steel 10.9.0.2 8080
probe steel 172.31.240.1 9999
probe steel 172.31.240.8 8080
probe app 172.31.240.2 3000
probe app 172.31.240.2 8080
kill $(jobs -p) 2>/dev/null || true
"""


def _netns_ok() -> bool:
    if not all(shutil.which(t) for t in ("nft", "unshare", "ip", "timeout", "bash")):
        return False
    probe = subprocess.run(
        ["unshare", "-rnm", "--fork", "sh", "-c",
         "mount -t tmpfs none /run && mkdir -p /run/netns && ip netns add t && nft list ruleset"],
        capture_output=True, text=True, check=False,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _netns_ok(), reason="needs unprivileged user, net and mount namespaces")
def test_the_rules_hold_on_real_packets_without_br_netfilter(tmp_path):
    """A bridge, an egress container, a client, a private and a public host.

    br_netfilter is not loaded in a fresh namespace, so this also proves the
    bridge table alone keeps the egress container away from its client.
    """
    bridge = "bayegtest0"
    rule = {"container": "steel", "bridge": bridge, "address": "172.31.240.2", "port": 3000}
    fw = tmp_path / "fw.nft"
    fw.write_text(_render_nft([rule]))
    run = subprocess.run(
        ["unshare", "-rnm", "--fork", "bash", "-c", _NETNS_SCRIPT],
        capture_output=True, text=True, check=False, timeout=90,
        env={"BR": bridge, "FW": str(fw), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    assert run.returncode == 0, run.stderr
    seen = set(run.stdout.split("\n"))
    assert {
        "open steel 198.51.100.2:8080",  # public internet
        "blocked steel 10.9.0.2:8080",  # private range
        "blocked steel 172.31.240.1:9999",  # the box, through the gateway
        "blocked steel 172.31.240.8:8080",  # its own client
        "open app 172.31.240.2:3000",  # the client calls it on its port
        "blocked app 172.31.240.2:8080",  # and on no other port
        "before open steel 10.9.0.2:8080",
        "before open steel 172.31.240.1:9999",
        "before open steel 172.31.240.8:8080",
        "before open app 172.31.240.2:8080",
    } <= seen, run.stdout
