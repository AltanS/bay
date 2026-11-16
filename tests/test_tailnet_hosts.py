"""Tailnet-only host names (bay.toml `tailnet_hosts`, compiled `tailnet_domains`).

One predicate, `bay_split_dns_domains`, decides which names split DNS sends
to a box's tailnet address. The Headscale templates render their records from
it and `bay_traefik_labels` binds `websecure_tailnet` from it. If the two
disagreed, a tailnet client would resolve a name to the tailnet address and
find no router there (Traefik's 404 for the whole site). These tests pin them
together, and pin the tailnet router to the tailnet listener only.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from helpers import make_ansible_env, run_role_tasks

from bay_cli import bay_toml, compiler
from bay_cli import healthcheck as hc
from bay_cli import plan as planmod
from bay_cli import routes as routesmod
from bay_cli.fleet import FleetError, load_inputs

ROOT = Path(__file__).parent.parent
HEADSCALE_TEMPLATES = ROOT / "roles" / "headscale" / "templates"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TOP = _load("filter_plugins/bay_filters.py", "_tailnet_hosts_top")
ROLE = _load("roles/container_lifecycle/filter_plugins/bay_filters.py", "_tailnet_hosts_role")
COPIES = pytest.mark.parametrize("mod", [TOP, ROLE], ids=["top", "role"])

SPLIT = {
    "traefik_split_entrypoints": True,
    "tailnet_entrypoint_enabled": True,
    "nftables_tailnet_ip_guard": True,
}
NOT_SPLIT = {
    "traefik_split_entrypoints": False,
    "tailnet_entrypoint_enabled": True,
    "nftables_tailnet_ip_guard": True,
}

SERVICES = {
    "plain": {"access": "public", "domains": ["plain.example.com"], "ports": {"internal": 80}},
    "locked": {
        "access": "public",
        "domains": ["locked.example.com", "www.locked.example.com"],
        "vpn_routes": ["/super"],
        "ports": {"internal": 80},
    },
    "vpnonly": {"access": "vpn", "domains": ["vpn.example.com"], "ports": {"internal": 80}},
    "vpnhook": {
        "access": "vpn",
        "domains": ["hook.example.com"],
        "public_routes": ["/hook"],
        "ports": {"internal": 80},
    },
    "admin": {
        "access": "public",
        "domains": ["app.com"],
        "tailnet_domains": ["admin.app.com"],
        "ports": {"internal": 8000},
    },
    "adminlocked": {
        "access": "public",
        "domains": ["ag.example.com", "www.ag.example.com"],
        "vpn_routes": ["/super"],
        "tailnet_domains": ["admin.ag.example.com"],
        "healthcheck_path": "/up",
        "middleware": {"basic_auth": {"users": ["u:h"]}},
        "ports": {"internal": 8000},
    },
}


def _routers(labels: dict) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for key, value in labels.items():
        parts = key.split(".")
        if parts[:3] == ["traefik", "http", "routers"]:
            out.setdefault(parts[3], {})[".".join(parts[4:])] = value
    return out


def _tailnet_bound_hosts(labels: dict) -> set[str]:
    """Every Host() name of a router that binds websecure_tailnet."""
    import re

    hosts: set[str] = set()
    for router in _routers(labels).values():
        if "websecure_tailnet" in router.get("entrypoints", "").split(","):
            hosts.update(re.findall(r"Host\(`([^`]+)`\)", router["rule"]))
    return hosts


# ── the predicate ───────────────────────────────────────────────────────


@COPIES
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("plain", []),
        ("locked", ["locked.example.com", "www.locked.example.com"]),
        ("vpnonly", ["vpn.example.com"]),
        ("vpnhook", ["hook.example.com"]),
        ("admin", ["admin.app.com"]),
        ("adminlocked", ["ag.example.com", "www.ag.example.com", "admin.ag.example.com"]),
    ],
)
def test_split_dns_names(mod, name, expected):
    assert mod.bay_split_dns_domains(SERVICES[name]) == expected


def test_both_copies_define_the_same_predicate():
    import inspect

    assert inspect.getsource(TOP.bay_split_dns_domains) == inspect.getsource(
        ROLE.bay_split_dns_domains
    )


# ── the filter and the Headscale template agree ─────────────────────────


def _records(services: dict) -> set[str]:
    env = make_ansible_env(HEADSCALE_TEMPLATES)
    ctx = {
        "ansible_managed": "x",
        "groups": {"all": ["box"]},
        "inventory_hostname": "box",
        "hostvars": {
            "box": {"region": "", "services": services, "headscale_server_tailnet_ip": "100.64.0.6"}
        },
        "tailnet_proxies": {},
        "headscale_extra_dns_records": [],
    }
    rendered = json.loads(env.get_template("extra-records.json.j2").render(**ctx))
    assert all(r["value"] == "100.64.0.6" for r in rendered)
    return {r["name"] for r in rendered}


def _split_routes(services: dict) -> set[str]:
    env = make_ansible_env(HEADSCALE_TEMPLATES)
    ctx = {
        "ansible_managed": "x",
        "headscale_domain": "hs.example.com",
        "headscale_tailnet_cidr": ["100.64.0.0/10"],
        "headscale_magic_dns_domain": "t.internal",
        "headscale_server_tailnet_ip": "100.64.0.1",
        "groups": {"all": ["box"]},
        "inventory_hostname": "box",
        "hostvars": {
            "box": {"region": "", "services": services, "headscale_server_tailnet_ip": "100.64.0.6"}
        },
        "tailnet_proxies": {},
        "headscale_extra_dns_records": [],
    }
    conf = yaml.safe_load(env.get_template("config.yaml.j2").render(**ctx))
    return set(((conf["dns"].get("nameservers") or {}).get("split") or {}).keys())


@COPIES
@pytest.mark.parametrize("name", sorted(SERVICES))
def test_tailnet_listener_serves_exactly_the_split_dns_names(mod, name):
    svc = SERVICES[name]
    labels = mod.bay_traefik_labels(svc, name, SPLIT)
    assert _tailnet_bound_hosts(labels) == _records({name: svc})
    assert _records({name: svc}) == _split_routes({name: svc}) or not _records({name: svc})


# ── routers on a split host ──────────────────────────────────────────────


@COPIES
def test_public_service_with_locked_paths_binds_the_tailnet_listener(mod):
    r = _routers(mod.bay_traefik_labels(SERVICES["locked"], "locked", SPLIT))
    assert r["locked"]["entrypoints"] == "websecure,websecure_tailnet"
    assert r["locked-vpn"]["entrypoints"] == "websecure,websecure_tailnet"


@COPIES
def test_plain_public_service_stays_public_only(mod):
    r = _routers(mod.bay_traefik_labels(SERVICES["plain"], "plain", SPLIT))
    assert r["plain"]["entrypoints"] == "websecure"


@COPIES
def test_existing_vpn_entrypoints_are_kept_in_order(mod):
    config = {**SPLIT, "vpn_entrypoints": "websecure,websecure_tailnet"}
    r = _routers(mod.bay_traefik_labels(SERVICES["vpnonly"], "vpnonly", config))
    assert r["vpnonly-vpn"]["entrypoints"] == "websecure,websecure_tailnet"


@COPIES
@pytest.mark.parametrize("name", ["plain", "locked", "vpnonly", "vpnhook"])
def test_off_a_split_host_nothing_changes(mod, name):
    """Without split entrypoints the labels are what they were before the
    feature: same keys, same values (no config hash change, no recreate)."""
    before = mod.bay_traefik_labels(SERVICES[name], name, {})
    assert mod.bay_traefik_labels(SERVICES[name], name, NOT_SPLIT) == before
    no_ip = {**SPLIT, "tailnet_entrypoint_enabled": False}
    assert mod.bay_traefik_labels(SERVICES[name], name, no_ip) == before


# ── the tailnet router ───────────────────────────────────────────────────


@COPIES
def test_tailnet_router_binds_only_the_tailnet_listener(mod):
    labels = mod.bay_traefik_labels(SERVICES["admin"], "admin", SPLIT)
    r = _routers(labels)
    assert r["admin-tailnet"] == {
        "rule": "Host(`admin.app.com`)",
        "service": "admin",
        "entrypoints": "websecure_tailnet",
        "tls.certresolver": "letsencrypt",
        "middlewares": "vpn-chain",
    }
    # The public router does not learn the tailnet host.
    assert "admin.app.com" not in r["admin"]["rule"]
    assert r["admin"]["entrypoints"] == "websecure"


@COPIES
def test_tailnet_router_keeps_the_service_middlewares(mod):
    r = _routers(mod.bay_traefik_labels(SERVICES["adminlocked"], "ag", SPLIT))
    assert r["ag-tailnet"]["middlewares"] == "vpn-chain,ag-basicauth"
    assert r["ag-tailnet"]["entrypoints"] == "websecure_tailnet"
    assert "admin.ag.example.com" not in r["ag"]["rule"]
    assert "admin.ag.example.com" not in r["ag-vpn"]["rule"]
    assert "admin.ag.example.com" not in r["ag-health"]["rule"]


@COPIES
def test_tailnet_router_of_a_vpn_service_points_at_its_backend(mod):
    svc = {**SERVICES["vpnonly"], "tailnet_domains": ["admin.vpn.example.com"]}
    labels = mod.bay_traefik_labels(svc, "v", SPLIT)
    ref = labels["traefik.http.routers.v-tailnet.service"]
    assert f"traefik.http.services.{ref}.loadbalancer.server.port" in labels


@COPIES
@pytest.mark.parametrize(
    "config",
    [
        {},
        NOT_SPLIT,
        {**SPLIT, "tailnet_entrypoint_enabled": False},
        {**SPLIT, "nftables_tailnet_ip_guard": False},
    ],
    ids=["nothing", "not-split", "no-tailnet-ip", "guard-off"],
)
def test_tailnet_router_refuses_instead_of_falling_back(mod, config):
    with pytest.raises(ValueError, match="tailnet_domains"):
        mod.bay_traefik_labels(SERVICES["admin"], "admin", config)


@COPIES
def test_string_booleans_from_ansible_are_read(mod):
    config = {k: str(v) for k, v in SPLIT.items()}
    labels = mod.bay_traefik_labels(SERVICES["admin"], "admin", config)
    assert labels["traefik.http.routers.admin-tailnet.entrypoints"] == "websecure_tailnet"
    off = {**config, "nftables_tailnet_ip_guard": "False"}
    with pytest.raises(ValueError):
        mod.bay_traefik_labels(SERVICES["admin"], "admin", off)


# ── bay.toml and the compile ─────────────────────────────────────────────

FIXTURE = ROOT / "tests" / "fixtures" / "fleet_min"
SHOP = Path("checkouts") / "shop" / "bay.toml"
SHOP_DEPLOY = 'domain = "shop.example.com"\n'
SPLIT_EU = "traefik_split_entrypoints: true\nnftables_tailnet_ip_guard: true\n"


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    dest = tmp_path / "fleet"
    shutil.copytree(FIXTURE, dest)
    return dest


def _set_hosts(root: Path, hosts: str = '["admin.shop.example.com"]') -> None:
    path = root / SHOP
    text = path.read_text()
    assert SHOP_DEPLOY in text
    path.write_text(text.replace(SHOP_DEPLOY, SHOP_DEPLOY + f"tailnet_hosts = {hosts}\n", 1))


def _group_vars(root: Path, group: str, text: str, name: str = "main.yml") -> None:
    d = root / "group_vars" / group
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("---\n" + text)


def _compile(root: Path) -> dict:
    result = compiler.compile_fleet(
        load_inputs(root, checkouts={"shop": root / "checkouts" / "shop"})
    )
    return yaml.safe_load(result.body())


def _problems(root: Path) -> str:
    with pytest.raises((compiler.CompileError, FleetError)) as info:
        _compile(root)
    return "\n".join(info.value.lines)


def test_unset_compiles_without_the_key(fleet):
    out = _compile(fleet)
    assert all("tailnet_domains" not in s for s in out["services"].values())


def test_compiles_to_tailnet_domains_on_the_main_container(fleet):
    _set_hosts(fleet)
    _group_vars(fleet, "eu", SPLIT_EU)
    shop = _compile(fleet)["services"]["shop"]
    assert shop["tailnet_domains"] == ["admin.shop.example.com"]
    assert "admin.shop.example.com" not in shop["domains"]


def test_flags_may_come_from_the_env_or_all(fleet):
    _set_hosts(fleet)
    _group_vars(fleet, "production", "traefik_split_entrypoints: true\n")
    _group_vars(fleet, "all", "nftables_tailnet_ip_guard: 'yes'\n", name="flags.yml")
    assert _compile(fleet)["services"]["shop"]["tailnet_domains"]


@pytest.mark.parametrize(
    ("gv", "needle"),
    [
        (
            "",
            "traefik_split_entrypoints is not set in "
            "group_vars/eu, group_vars/production, group_vars/all",
        ),
        ("nftables_tailnet_ip_guard: true\n", "traefik_split_entrypoints is not set"),
        ("traefik_split_entrypoints: true\n", "nftables_tailnet_ip_guard is not set"),
        ("traefik_split_entrypoints: true\nnftables_tailnet_ip_guard: false\n",
         "nftables_tailnet_ip_guard is false in group_vars/eu/main.yml"),
        ("traefik_split_entrypoints: \"{{ x }}\"\nnftables_tailnet_ip_guard: true\n",
         "is not a literal true"),
    ],
)
def test_refuses_a_box_that_is_not_ready(fleet, gv, needle):
    _set_hosts(fleet)
    _group_vars(fleet, "eu", gv)
    msg = _problems(fleet)
    assert "deploy.production.tailnet_hosts: box eu-1" in msg
    assert needle in msg


def test_a_vault_file_is_never_read(fleet):
    _set_hosts(fleet)
    (fleet / "group_vars" / "eu").mkdir(parents=True)
    (fleet / "group_vars" / "eu" / "main.yml").write_text("$ANSIBLE_VAULT;1.1;AES256\n6162\n")
    assert "traefik_split_entrypoints is not set" in _problems(fleet)


def test_refuses_a_box_without_a_tailnet_ip(fleet):
    _set_hosts(fleet)
    _group_vars(fleet, "eu", SPLIT_EU)
    path = fleet / "bay.fleet.toml"
    path.write_text(path.read_text().replace('tailnet_ip = "100.64.0.3"\n', "", 1))
    assert "box eu-1 has no tailnet_ip in bay.fleet.toml" in _problems(fleet)


def test_refuses_a_host_another_project_routes(fleet):
    _set_hosts(fleet, '["status.example.com"]')
    _group_vars(fleet, "eu", SPLIT_EU)
    assert "status.example.com" in _problems(fleet)


@pytest.mark.parametrize(
    ("hosts", "needle"),
    [
        ('["shop.example.com"]', "already used"),
        ('["www.shop.example.com"]', "already used"),
        ('["https://admin.shop.example.com"]', "domain"),
        ('["admin.shop.example.com:443"]', "domain"),
        ('["Admin.shop.example.com"]', "domain"),
        ('["*.shop.example.com"]', "domain"),
        ('["100.64.0.3"]', "domain"),
        ('["admin.shop.example.com/x"]', "domain"),
        ("[]", "tailnet_hosts"),
        ('["a.shop.example.com", "a.shop.example.com"]', "tailnet_hosts"),
    ],
)
def test_validator_refuses_bad_hosts(fleet, hosts, needle):
    _set_hosts(fleet, hosts)
    violations = bay_toml.validate_file(fleet / SHOP)
    assert violations, hosts
    assert any(needle in f"{v.path} {v.message}" for v in violations), violations


def test_validator_refuses_tailnet_hosts_on_an_internal_main_container(fleet):
    path = fleet / SHOP
    text = path.read_text()
    text = text.replace(
        SHOP_DEPLOY,
        SHOP_DEPLOY + 'tailnet_hosts = ["admin.shop.example.com"]\n',
        1,
    )
    path.write_text(text)
    doc = tomllib.loads(path.read_text())
    doc["deploy"]["production"].setdefault("access", {})["mode"] = "internal"
    violations = bay_toml.validate(doc)
    assert any(
        v.path == "deploy.production.tailnet_hosts" and "internal" in v.message
        for v in violations
    )


def test_validator_refuses_the_key_outside_deploy_env(fleet):
    doc = tomllib.loads((fleet / SHOP).read_text())
    doc.setdefault("access", {})["tailnet_hosts"] = ["admin.shop.example.com"]
    assert any("tailnet_hosts" in f"{v.path} {v.message}" for v in bay_toml.validate(doc))


# ── deploy-time gates (roles/deploy_stack/tasks/validate.yml) ──────────

VALIDATE = ROOT / "roles" / "deploy_stack" / "tasks" / "validate.yml"


def _validate_task(name: str) -> dict:
    for task in yaml.safe_load(VALIDATE.read_text()):
        if task.get("name") == name:
            return task
    raise AssertionError(name)


def test_deploy_refuses_tailnet_hosts_without_listener_or_guard():
    task = _validate_task("Validate tailnet hosts have the tailnet listener and the address guard")
    when = task["when"]
    for var in ("traefik_split_entrypoints", "gateway_bind_ip", "nftables_tailnet_ip_guard"):
        assert var in when
    assert "tailnet_domains" in task["loop"]


PREFLIGHT = ROOT / "roles" / "deploy_stack" / "tasks" / "host_preflight.yml"


def _preflight_task(name: str) -> dict:
    for task in yaml.safe_load(PREFLIGHT.read_text()):
        if task.get("name") == name:
            return task
    raise AssertionError(name)


def test_deploy_reads_the_live_guard_even_in_check_mode():
    read = _preflight_task("Read the live pre-routing table for the tailnet address guard")
    assert read["ansible.builtin.command"]["cmd"] == "nft list table inet bay_prerouting"
    assert read["check_mode"] is False
    assert read["changed_when"] is False
    assert read["become_user"] == "root"


def test_preflight_runs_before_rebuild_sh_is_rendered_and_in_validate():
    """The guard check must run before git_deploy renders rebuild.sh: a run
    that fails later in deploy_stack must not leave a rebuild.sh that serves
    a tailnet host with no guard (review finding 1a)."""
    tasks = yaml.safe_load((ROOT / "roles" / "git_deploy" / "tasks" / "main.yml").read_text())
    names = [t.get("name") for t in tasks]
    first = tasks[0]
    assert first["name"] == "Check the live tailnet address guard and the CPU limits"
    assert first["ansible.builtin.include_tasks"]["file"].endswith(
        "deploy_stack/tasks/host_preflight.yml"
    )
    assert set(first["tags"]) == {"deploy_stack", "build", "git_deploy"}
    assert names.index(first["name"]) < names.index(
        "Re-render rebuild.sh (runs under deploy_stack too)"
    )
    inc = _validate_task("Check the live tailnet address guard and the CPU limits")
    assert inc["ansible.builtin.include_tasks"] == "host_preflight.yml"


# ── bay plan ─────────────────────────────────────────────────────────────

_OLD = {"access": "public", "domains": ["ag.example.com"], "image": "x:1", "ports": {"internal": 8000}}
_NEW = {**_OLD, "tailnet_domains": ["admin.ag.example.com"]}


def _steps(old, new):
    return planmod._container_steps("ag", old, new, project="ag", running=None)


def test_adding_a_tailnet_host_is_its_own_shared_step():
    steps = _steps(_OLD, _NEW)
    update = [s for s in steps if s["kind"] == "container"]
    assert update[0]["risk"] == "safe"
    assert "tailnet_domains" in update[0]["reason"]
    [host] = [s for s in steps if s["kind"] == "tailnet_host"]
    assert host["action"] == "tailnet_host_added"
    assert host["risk"] == "shared"
    assert host["resource"] == "admin.ag.example.com"
    assert "Headscale restarts" in host["reason"]
    assert "A record" in host["reason"]


def test_removing_a_tailnet_host_is_shared_too():
    [host] = [s for s in _steps(_NEW, _OLD) if s["kind"] == "tailnet_host"]
    assert host["action"] == "tailnet_host_removed"
    assert host["risk"] == "shared"


def test_a_new_container_with_a_tailnet_host_and_a_removed_one():
    assert [s["action"] for s in _steps(None, _NEW)] == ["create", "tailnet_host_added"]
    assert [s["action"] for s in _steps(_NEW, None)] == ["remove", "tailnet_host_removed"]


def test_no_change_no_step():
    assert not [s for s in _steps(_NEW, dict(_NEW)) if s["kind"] == "tailnet_host"]


def test_the_verdict_is_approve():
    plan = {"steps": _steps(_OLD, _NEW), "blockers": []}
    planmod.decide(plan)
    assert plan["verdict"] == "approve"
    assert plan["exit_code"] == 10


def test_bay_up_adds_the_headscale_and_traefik_tags():
    steps = _steps(_OLD, _NEW)
    assert routesmod.deploy_tags(steps, "deploy_stack") == "deploy_stack,headscale,traefik"


def test_the_step_kind_is_in_the_plan_schema():
    schema = json.loads((ROOT / "src/bay_cli/schemas/plan.schema.json").read_text())
    assert "tailnet_host" in json.dumps(schema)


# ── health checks ────────────────────────────────────────────────────────

@pytest.mark.parametrize("include_vpn", [False, True])
def test_tailnet_hosts_are_listed_as_skipped(include_vpn):
    services = {"ag": {**_NEW, "domains": ["ag.example.com"]}}
    targets = hc._collect_targets(services, include_vpn=include_vpn)
    assert [(n, d, s) for n, d, _p, s, _r in targets] == [
        ("ag", "ag.example.com", False),
        ("ag", "admin.ag.example.com", True),
    ]
    assert targets[1][4] == hc.TAILNET_HOST_REASON


def test_run_healthcheck_never_probes_a_tailnet_host(monkeypatch):
    probed = []

    def fake(url_domain, *a, **k):
        probed.append(url_domain)
        return hc.CheckResult(service="ag", domain=url_domain, status=200, ok=True)

    monkeypatch.setattr(hc, "check_domain", lambda name, domain, *a, **k: fake(domain))
    results = hc.run_healthcheck({"ag": dict(_NEW)})
    assert probed == ["ag.example.com"]
    skipped = [r for r in results if r.skipped]
    assert [(r.domain, r.skip_reason) for r in skipped] == [
        ("admin.ag.example.com", hc.TAILNET_HOST_REASON)
    ]
    assert hc.summarize(results)["skipped"] == 1


# The gates run under ansible-core's strict conditionals (a string result
# fails the play with "Conditionals must have a boolean result"). These run
# the real tasks with ansible-playbook on localhost.
_GATE = "Validate tailnet hosts have the tailnet listener and the address guard"
_SVC = {"web": {"tailnet_domains": ["admin.example.com"]}}


@pytest.mark.parametrize(
    ("variables", "message"),
    [
        ({"traefik_split_entrypoints": True, "gateway_bind_ip": "100.64.0.6",
          "nftables_tailnet_ip_guard": True}, None),
        ({"traefik_split_entrypoints": False, "gateway_bind_ip": "100.64.0.6",
          "nftables_tailnet_ip_guard": True}, "does not run traefik_split_entrypoints"),
        ({"traefik_split_entrypoints": True, "gateway_bind_ip": "",
          "nftables_tailnet_ip_guard": True}, "has no tailnet address"),
        ({"traefik_split_entrypoints": True, "gateway_bind_ip": "100.64.0.6",
          "nftables_tailnet_ip_guard": False}, "has nftables_tailnet_ip_guard off"),
    ],
)
def test_listener_gate_runs_under_strict_conditionals(
    tmp_path: Path, variables: dict, message: str | None
) -> None:
    result = run_role_tasks(tmp_path, VALIDATE, [_GATE], {**variables, "active_services": _SVC})
    out = result.stdout + result.stderr
    assert "Conditionals must have a boolean result" not in out
    if message is None:
        assert result.returncode == 0, out
    else:
        assert result.returncode != 0
        assert message in out


_GUARD_TABLE = """table inet bay_prerouting {
	chain tailnet_dst {
		iifname "tailscale0" return
		iifname "lo" return
		counter packets 0 bytes 0 drop
	}

	chain prerouting {
		type filter hook prerouting priority -350; policy accept;
		ip daddr 100.64.0.6 jump tailnet_dst
		ip protocol tcp ip hdrlength != 5 counter packets 0 bytes 0 drop
	}
}
"""
_REFUSE_GUARD = "Refuse tailnet hosts without the live guard for the tailnet address"


@pytest.mark.parametrize(
    ("live", "address", "passes"),
    [
        ({"rc": 0, "stdout": _GUARD_TABLE}, "100.64.0.6", True),
        # The guard is there, but for another address (review finding 2).
        ({"rc": 0, "stdout": _GUARD_TABLE}, "100.64.0.7", False),
        ({"rc": 0, "stdout": _GUARD_TABLE.replace("100.64.0.6", "100.64.0.60")},
         "100.64.0.6", False),
        # The chain is there but the jump is gone.
        ({"rc": 0, "stdout": _GUARD_TABLE.replace(
            "\t\tip daddr 100.64.0.6 jump tailnet_dst\n", "")}, "100.64.0.6", False),
        ({"rc": 0, "stdout": _GUARD_TABLE.replace("counter packets 0 bytes 0 drop", "accept")},
         "100.64.0.6", False),
        ({"rc": 1, "stdout": ""}, "100.64.0.6", False),
        ({"rc": 0, "stdout": _GUARD_TABLE}, "", False),
        ({"skipped": True}, "100.64.0.6", True),
    ],
)
def test_live_guard_gate_checks_the_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: dict, address: str, passes: bool
) -> None:
    monkeypatch.setenv("ANSIBLE_FILTER_PLUGINS", str(ROOT / "filter_plugins"))
    result = run_role_tasks(
        tmp_path, PREFLIGHT, [_REFUSE_GUARD],
        {"_preflight_guard": live, "_preflight_tailnet_services": ["web"],
         "gateway_bind_ip": address},
    )
    out = result.stdout + result.stderr
    assert "Conditionals must have a boolean result" not in out
    assert (result.returncode == 0) is passes, out
    if not passes:
        assert "no tailnet address guard for" in out


_REFUSE_CPUS = "Refuse a CPU limit above the host's CPU count"


@pytest.mark.parametrize(
    ("cpus", "nproc", "passes"),
    [(2, "8\n", True), (8, "8\n", True), (8.5, "8\n", False), (16, "8\n", False)],
)
def test_cpus_above_the_host_cpu_count_stops_the_deploy(
    tmp_path: Path, cpus: float, nproc: str, passes: bool
) -> None:
    result = run_role_tasks(
        tmp_path, PREFLIGHT, [_REFUSE_CPUS],
        {"_preflight_cpus": [{"key": "steel", "value": {"cpus": cpus}}],
         "_preflight_nproc": {"rc": 0, "stdout": nproc}},
    )
    out = result.stdout + result.stderr
    assert "Conditionals must have a boolean result" not in out
    assert (result.returncode == 0) is passes, out
    if not passes:
        assert "Docker would refuse" in out


# ── webhook rebuilds build the same labels as the deploy ────────────────
# rebuild.sh recreates a container after a push with its own copy of the
# Traefik config (`_tc`). A key missing there gave the push a different label
# set than the deploy: on a split host the routers lost websecure_tailnet, so
# tailnet clients got 404 until the next deploy, and a tailnet host failed to
# render at all. A live test-box run caught it.

_REBUILD = ROOT / "roles" / "git_deploy" / "templates" / "rebuild.sh.j2"
_BUILD_SPECS = ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"


def _deploy_config_keys() -> set[str]:
    tasks = yaml.safe_load(_BUILD_SPECS.read_text())
    task = next(t for t in tasks if t.get("name") == "Assemble traefik config for label generation")
    return set(task["ansible.builtin.set_fact"]["_traefik_config"])


def test_rebuild_traefik_config_has_the_deploy_keys() -> None:
    import re

    text = _REBUILD.read_text()
    blocks = re.findall(r"\{% set _tc = \{(.*?)\} %\}", text, re.DOTALL)
    assert len(blocks) == 2
    for block in blocks:
        assert set(re.findall(r"^\s*'(\w+)':", block, re.MULTILINE)) == _deploy_config_keys()


def test_rebuild_labels_bind_the_tailnet_listener_on_a_split_host() -> None:
    from test_rebuild_config import _render_rebuild_sh

    services = {
        "app": {
            "build": {"repo": "https://github.com/acme/app.git", "branch": "main",
                      "strategy": "remote"},
            "image": "zot.example.com/acme/app:latest",
            "access": "public",
            "vpn_routes": ["/locked"],
            "domains": ["app.example.com"],
            "tailnet_domains": ["admin.example.com"],
            "ports": {"internal": 8000},
        }
    }
    rendered = _render_rebuild_sh(
        services, ["app"], gateway_bind_ip="100.64.0.6",
        traefik_split_entrypoints=True, nftables_tailnet_ip_guard=True,
    )
    block = rendered[rendered.index("_run_container()"):]
    assert "-l 'traefik.http.routers.app.entrypoints=websecure,websecure_tailnet'" in block
    assert "-l 'traefik.http.routers.app-vpn.entrypoints=websecure,websecure_tailnet'" in block
    assert "-l 'traefik.http.routers.app-tailnet.entrypoints=websecure_tailnet'" in block
    assert "-l 'traefik.http.routers.app-tailnet.rule=Host(`admin.example.com`)'" in block


def test_rebuild_labels_unchanged_on_a_host_that_is_not_split() -> None:
    from test_rebuild_config import _render_rebuild_sh

    services = {
        "app": {
            "build": {"repo": "https://github.com/acme/app.git", "branch": "main",
                      "strategy": "remote"},
            "image": "zot.example.com/acme/app:latest",
            "access": "vpn",
            "domains": ["app.example.com"],
            "ports": {"internal": 8000},
        }
    }
    rendered = _render_rebuild_sh(services, ["app"], gateway_bind_ip="100.64.0.6")
    assert "-l 'traefik.http.routers.app-vpn.entrypoints=websecure' " in rendered
    assert "websecure_tailnet" not in rendered


# ── Review fixes: port 80/443 on the tailnet address, IPv6 in split mode ─────

sys.path.insert(0, str(ROOT / "filter_plugins"))
from bay_filters import bay_host_tailnet_services, bay_tailnet_port_clashes  # noqa: E402


def test_host_tailnet_services_follow_the_regions() -> None:
    services = {
        "a": {"tailnet_domains": ["a.example.com"]},
        "b": {"tailnet_domains": ["b.example.com"], "regions": ["eu2"]},
        "c": {"tailnet_domains": ["c.example.com"], "regions": ["na"]},
        "d": {"domains": ["d.example.com"]},
    }
    assert bay_host_tailnet_services(services, ["eu2", "production"]) == ["a", "b"]
    assert bay_host_tailnet_services(services, ["infra"]) == ["a"]
    assert bay_host_tailnet_services(None, []) == []


def test_tailnet_port_clashes() -> None:
    services = {
        "web": {"ports": {"internal": 80, "expose": "gateway"}},
        "tls": {"ports": {"internal": 443, "expose": "tailnet"}},
        "app": {"ports": {"internal": 8080, "expose": "gateway"}},
        "lo": {"ports": {"internal": 80, "expose": "loopback"}},
        "none": {"ports": {"internal": 80}},
    }
    accessories = {
        "dns": {"port": "443:8443", "expose": "gateway"},
        "pg": {"port": "5432", "expose": "tailnet"},
        "edge": {"port": "8443:443", "expose": "gateway"},
    }
    assert bay_tailnet_port_clashes(services, accessories) == ["dns:443", "tls:443", "web:80"]
    assert bay_tailnet_port_clashes({}, None) == []


_CLASH = "Validate no container publishes port 80 or 443 on the tailnet address"


@pytest.mark.parametrize(
    ("split", "bind", "port", "passes"),
    [
        (True, "100.64.0.6", 80, False),
        (True, "100.64.0.6", 443, False),
        (True, "100.64.0.6", 8080, True),
        (False, "100.64.0.6", 80, True),
        (True, "", 80, True),
    ],
)
def test_split_refuses_port_80_or_443_on_the_tailnet_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, split: bool, bind: str, port: int,
    passes: bool,
) -> None:
    monkeypatch.setenv("ANSIBLE_FILTER_PLUGINS", str(ROOT / "filter_plugins"))
    result = run_role_tasks(
        tmp_path, VALIDATE, [_CLASH],
        {"traefik_split_entrypoints": split, "gateway_bind_ip": bind,
         "active_services": {"web": {"ports": {"internal": port, "expose": "gateway"}}},
         "active_accessories": {}},
    )
    out = result.stdout + result.stderr
    assert "Conditionals must have a boolean result" not in out
    assert (result.returncode == 0) is passes, out
    if not passes:
        assert f"web:{port}" in out


_IPV6_READ = "Read the global IPv6 addresses of this host"
_IPV6_WARN = "Warn that split entrypoints do not serve IPv6"


@pytest.mark.parametrize(
    ("split", "ip_out", "warns"),
    [
        (True, "2: eth0    inet6 2a01:4f8::1/64 scope global \\       valid_lft forever", True),
        (True, "", False),
        (False, "2: eth0    inet6 2a01:4f8::1/64 scope global", False),
    ],
)
def test_split_warns_but_does_not_fail_on_a_host_with_ipv6(
    tmp_path: Path, split: bool, ip_out: str, warns: bool
) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ip").write_text(f"#!/bin/sh\nprintf '%s' '{ip_out}'\n")
    (fake / "ip").chmod(0o755)
    result = run_role_tasks(
        tmp_path, VALIDATE, [_IPV6_READ, _IPV6_WARN],
        {"traefik_split_entrypoints": split, "traefik_public_bind_ip": "203.0.113.7"},
        path_dir=fake,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert ("WARNING: this host has a global IPv6 address" in out) is warns
    if warns:
        assert "203.0.113.7" in out
