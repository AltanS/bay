"""Headscale trusts Traefik's proxy headers (2.20.0).

Traefik (host network) reaches the headscale container from the gateway of
the docker network. Without `trusted_proxies`, Headscale logs that address for
every client. The headscale role reads the gateway with `docker network
inspect` and renders it as a /32 (a /128 for IPv6) into config.yaml. The key,
its prefix-only syntax and the refusal of 0.0.0.0/0 and ::/0 are those of
headscale 0.29.7 (config-example.yaml, hscontrol/types/config.go).

Headscale reads True-Client-IP before X-Real-IP and X-Forwarded-For, and
Traefik passes True-Client-IP through. So both headscale routers carry the
middleware `headscale-client-ip`, which removes it.

The compute, validate and template tasks run here through real
ansible-playbook, so the filter chain is Ansible's, not a stand-in.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import make_ansible_env

REPO = Path(__file__).resolve().parent.parent
ROLE = REPO / "roles" / "headscale"
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
BUILD_SPECS = REPO / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
COMPOSE_DIR = REPO / "roles" / "deploy_stack" / "templates"
GATEWAY_DEFAULTS = REPO / "roles" / "access_gateway" / "defaults" / "main.yml"
STRIP = "traefik.http.middlewares.headscale-client-ip.headers.customrequestheaders.True-Client-IP"


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _task(name: str) -> dict:
    return next(t for t in _tasks() if t.get("name") == name)


def _inspect(*config: dict[str, str]) -> dict[str, Any]:
    doc = [{"Name": "services", "Driver": "bridge", "IPAM": {"Driver": "default", "Config": list(config)}}]
    return {"rc": 0, "stdout": json.dumps(doc), "changed": False}


def _play(tmp_path: Path, inspect: dict[str, Any], **override: Any) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run compute + validate + template on localhost; return the run and config.yaml."""
    names = [
        "Compute the address Traefik reaches headscale from",
        "Validate headscale_trusted_proxies",
    ]
    tasks = [_task(n) for n in names]
    out = tmp_path / "config.yaml"
    tasks.append({
        "name": "Render config",
        "ansible.builtin.template": {"src": str(ROLE / "templates" / "config.yaml.j2"), "dest": str(out)},
    })
    play_vars = {
        "_headscale_network_inspect": inspect,
        "headscale_domain": "hs.example.com",
        "headscale_tailnet_cidr": ["100.64.0.0/10"],
        "stack_name": "acme",
    }
    play = [{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars_files": [str(DEFAULTS)],
        "vars": play_vars,
        "tasks": tasks,
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    extra = ["-e", json.dumps(override)] if override else []
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,", *extra, str(tmp_path / "play.yml")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180,
    )
    return proc, out.read_text() if out.exists() else ""


def _trusted(config_text: str) -> list[str] | None:
    return yaml.safe_load(config_text).get("trusted_proxies")


def test_gateway_of_the_network_becomes_a_host_prefix(tmp_path: Path) -> None:
    proc, text = _play(tmp_path, _inspect({"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}))
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    # The gateway alone, never the subnet: every container on it would be trusted.
    assert _trusted(text) == ["172.18.0.1/32"]


def test_ipv6_gateway_is_a_128(tmp_path: Path) -> None:
    proc, text = _play(tmp_path, _inspect(
        {"Subnet": "172.20.0.0/16", "Gateway": "172.20.0.1"},
        {"Subnet": "fd00:1::/64", "Gateway": "fd00:1::1"},
    ))
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert _trusted(text) == ["172.20.0.1/32", "fd00:1::1/128"]


@pytest.mark.parametrize("inspect", [
    {"rc": 1, "stdout": "", "stderr": "Error: No such network: services"},
    {"skipped": True, "changed": False},
    _inspect({"Subnet": "172.18.0.0/16"}),
])
def test_no_readable_gateway_renders_nothing(tmp_path: Path, inspect: dict) -> None:
    proc, text = _play(tmp_path, inspect)
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert "trusted_proxies" not in text


def test_override_and_off(tmp_path: Path) -> None:
    gw = _inspect({"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"})
    proc, text = _play(tmp_path, gw, headscale_trusted_proxies=["10.9.0.1/32"])
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert _trusted(text) == ["10.9.0.1/32"]
    proc, text = _play(tmp_path, gw, headscale_trusted_proxies=[])
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert "trusted_proxies" not in text


@pytest.mark.parametrize("bad", [["172.18.0.1"], ["0.0.0.0/0"], ["::/0"], "172.18.0.1/32", ["services"]])
def test_values_headscale_refuses_stop_the_deploy(tmp_path: Path, bad: Any) -> None:
    """Headscale crash-loops on a bare IP or /0; the role must stop first."""
    proc, text = _play(tmp_path, _inspect({"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}),
                       headscale_trusted_proxies=bad)
    assert proc.returncode != 0
    assert "headscale_trusted_proxies must be a list of prefixes" in proc.stdout
    assert text == ""


def test_network_read_runs_in_check_mode_and_as_root() -> None:
    task = _task("Read the docker network between Traefik and headscale")
    assert task["check_mode"] is False
    assert task["become"] is True and task["become_user"] == "root"
    assert task["changed_when"] is False and task["failed_when"] is False
    assert "docker network inspect" in task["ansible.builtin.command"]["cmd"]


def test_network_exists_before_it_is_read_and_before_the_config() -> None:
    names = [t.get("name") for t in _tasks()]
    create = names.index("Ensure the docker network between Traefik and headscale exists")
    read = names.index("Read the docker network between Traefik and headscale")
    validate = names.index("Validate headscale_trusted_proxies")
    template = names.index("Template headscale config")
    assert create < read < validate < template


def test_template_renders_nothing_without_the_variable() -> None:
    env = make_ansible_env(ROLE / "templates")
    base = {"ansible_managed": "test", "headscale_domain": "hs.example.com",
            "headscale_tailnet_cidr": ["100.64.0.0/10"], "headscale_magic_dns_domain": "acme.tailnet.internal",
            "headscale_dns_global_nameservers": [], "headscale_oidc_issuer": "",
            "headscale_ip_allocation": "random", "groups": {"all": ["box"]},
            "inventory_hostname": "box", "hostvars": {"box": {}}}
    assert "trusted_proxies" not in env.get_template("config.yaml.j2").render(**base)
    text = env.get_template("config.yaml.j2").render(**base, headscale_trusted_proxies=["172.18.0.1/32"])
    assert yaml.safe_load(text)["trusted_proxies"] == ["172.18.0.1/32"]


# ── Traefik strips True-Client-IP ─────────────────────────────────────────


def _labels(**ctx: Any) -> dict[str, str]:
    from test_headscale_admin_api import _built_labels

    return _built_labels(**ctx)


def test_both_routers_strip_true_client_ip() -> None:
    labels = _labels()
    assert labels[STRIP] == ""
    assert labels["traefik.http.routers.headscale.middlewares"].split(",")[0] == "headscale-client-ip"
    assert labels["traefik.http.routers.headscale-admin.middlewares"].split(",")[0] == "headscale-client-ip"
    # headscale_admin_api_public: true has no admin router, but the main one still strips.
    public = _labels(headscale_admin_api_public=True)
    assert public[STRIP] == ""
    assert public["traefik.http.routers.headscale.middlewares"] == "headscale-client-ip,public-chain"


def test_compose_partial_documents_the_strip() -> None:
    from test_headscale_admin_api import _compose

    text = _compose()
    assert f'"{STRIP}="' in text
    assert '"traefik.http.routers.headscale.middlewares=headscale-client-ip,public-chain"' in text
