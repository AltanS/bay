"""bay.fleet.toml [tailnet] allowlist is enforced (M118/04, gap 2).

`bay compile` writes the allowlist to the top-level `tailnet_allowlist:` of
services.yml. roles/access_gateway/tasks/allowlist.yml makes it
`vpn_allowed_ips` (loopback kept), before the access gateway appends the
Headscale range, and deploy.yml runs the same task on every deploy. Traefik's
vpn-only IPAllowList renders that list. `bay validate` warns when group_vars
still sets `vpn_allowed_ips`, and `bay doctor` and `bay gateway` read the
resolved list.

The set_fact tasks run in a real ansible-playbook, lifted from the role, so
nothing on the machine running the tests is touched.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from bay_cli import compiler
from bay_cli.commands import doctor
from bay_cli.commands import validate as v
from bay_cli.fleet import GENERATED_SERVICES, enforced_vpn_allowed_ips, load_inputs

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "fleet_min"
GATEWAY = ROOT / "roles" / "access_gateway" / "tasks"


def _compile(root: Path) -> compiler.CompileResult:
    return compiler.compile_fleet(
        load_inputs(root, checkouts={"shop": root / "checkouts" / "shop"})
    )


def _resolve(tmp_path: Path, variables: dict[str, Any]) -> list[str]:
    """Run the allowlist task, then the Headscale append, as the role does."""
    tasks = yaml.safe_load((GATEWAY / "allowlist.yml").read_text())
    main = yaml.safe_load((GATEWAY / "main.yml").read_text())
    append = next(t for t in main if t["name"].startswith("Ensure headscale tailnet CIDR"))
    out = tmp_path / "out.json"
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        # Play vars, not -e: on the box vpn_allowed_ips comes from group_vars,
        # which set_fact overrides; an extra var would win over set_fact.
        "vars": variables,
        "tasks": [*tasks, append, {
            "name": "Write the result",
            "ansible.builtin.copy": {"content": "{{ vpn_allowed_ips | to_json }}",
                                     "dest": str(out), "mode": "0600"},
        }],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         str(tmp_path / "play.yml")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    result: list[str] = json.loads(out.read_text())
    return result


def test_compile_tailnet_allowlist_into_traefik(tmp_path: Path) -> None:
    # ── compile: the fixture fleet sets [tailnet] allowlist
    root = tmp_path / "fleet"
    shutil.copytree(FIXTURE, root)
    result = _compile(root)
    data = yaml.safe_load(result.body())
    assert data["tailnet_allowlist"] == ["100.64.0.0/10"]
    fleet_toml = root / "bay.fleet.toml"
    fleet_toml.write_text(fleet_toml.read_text().replace('allowlist = ["100.64.0.0/10"]\n', ""))
    assert "tailnet_allowlist" not in yaml.safe_load(_compile(root).body())

    # ── the access gateway: the fleet list replaces group_vars, loopback stays
    gateway = {
        "access_gateway": "headscale",
        "headscale_tailnet_cidr": ["100.64.0.0/10"],
        "vpn_allowed_ips": ["127.0.0.1", "203.0.113.7"],
    }
    enforced = _resolve(tmp_path, {**gateway, "tailnet_allowlist": ["198.51.100.0/24"]})
    assert enforced == ["127.0.0.1", "::1", "198.51.100.0/24", "100.64.0.0/10"]
    same = _resolve(tmp_path, {**gateway, "tailnet_allowlist": data["tailnet_allowlist"]})
    assert same == ["127.0.0.1", "::1", "100.64.0.0/10"]
    # Without the fleet key nothing changes.
    assert _resolve(tmp_path, gateway) == ["127.0.0.1", "203.0.113.7", "100.64.0.0/10"]

    # ── Traefik's vpn-only middleware renders the enforced list
    spec = importlib.util.spec_from_file_location(
        "_role_filters_allowlist", ROOT / "roles/container_lifecycle/filter_plugins/bay_filters.py"
    )
    assert spec is not None and spec.loader is not None
    role = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(role)
    labels = role.bay_traefik_global_labels({"vpn_allowed_ips": enforced})
    assert labels["traefik.http.middlewares.vpn-only.ipallowlist.sourcerange"] == (
        "127.0.0.1,::1,198.51.100.0/24,100.64.0.0/10"
    )

    # ── the task runs on every deploy, before the Headscale append
    main = yaml.safe_load((GATEWAY / "main.yml").read_text())
    names = [t["name"] for t in main]
    assert names.index("Apply the fleet tailnet allowlist") < names.index(
        "Ensure headscale tailnet CIDR is allowlisted (append, not replace)"
    )
    plays = yaml.safe_load((ROOT / "deploy.yml").read_text())
    pre = next(p["pre_tasks"] for p in plays if any(
        t.get("name") == "Initialize active services" for t in p.get("pre_tasks", [])))
    task = next(t for t in pre if t["name"] == "Apply the fleet tailnet allowlist")
    assert task["ansible.builtin.include_role"] == {"name": "access_gateway", "tasks_from": "allowlist"}
    assert task["tags"] == ["always"]
    assert yaml.safe_load((GATEWAY / "allowlist.yml").read_text())[0]["tags"] == ["always"]

    # ── bay validate warns when group_vars still sets vpn_allowed_ips
    out = v.ValidationResult()
    v._validate_tailnet_allowlist(
        {"tailnet_allowlist": ["198.51.100.0/24"]},
        {"group_vars/all/vpn_access.yml": {"vpn_allowed_ips": ["203.0.113.7"]}},
        out,
    )
    assert out.warnings == [
        "[tailnet] allowlist replaces vpn_allowed_ips from group_vars "
        "(group_vars/all/vpn_access.yml)"
    ]
    quiet = v.ValidationResult()
    v._validate_tailnet_allowlist(
        {}, {"group_vars/all/vpn_access.yml": {"vpn_allowed_ips": ["203.0.113.7"]}}, quiet
    )
    assert quiet.warnings == []

    # ── bay doctor and bay gateway read the resolved list
    services = root / GENERATED_SERVICES
    services.write_text("# GENERATED\n" + yaml.safe_dump({"tailnet_allowlist": ["198.51.100.0/24"]}))
    assert enforced_vpn_allowed_ips(root, ["203.0.113.7"]) == ["127.0.0.1", "::1", "198.51.100.0/24"]
    assert doctor._check_gateway_config(
        "headscale", "hs.example.com", {"vpn_allowed_ips": []}, fleet_allowlist=["198.51.100.0/24"]
    ) == []
    assert doctor._check_gateway_config(
        "headscale", "hs.example.com", {"vpn_allowed_ips": ["1.2.3.4"]}, fleet_allowlist=[]
    ) == ["[tailnet] allowlist in bay.fleet.toml is empty, so only the box itself passes "
          "the vpn-only allowlist; add trusted IPs"]
    services.write_text("# GENERATED\n" + yaml.safe_dump({"services": {}}))
    assert enforced_vpn_allowed_ips(root, ["203.0.113.7"]) == ["203.0.113.7"]
