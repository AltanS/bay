"""File-provider routers use only file-provider middlewares (2.20.0).

The tailnet-proxy routers live in the Traefik file provider
(roles/traefik/templates/dynamic/tailnet-proxies.yml.j2). Before 2.20.0 they
named `vpn-only@docker`, a middleware defined by a label on the traefik
container. At each Traefik start the file provider loads first, so every one
of those routers logged "middleware vpn-only@docker does not exist" until the
docker provider had read the label.

The file now carries its own copy, `vpn-only@file`. Both copies must hold the
same source ranges, so both read vpn_allowed_ips with the same default. These
tests render both and compare.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import make_ansible_env
from jinja2 import Environment

REPO = Path(__file__).resolve().parent.parent
TRAEFIK_TEMPLATES = REPO / "roles" / "traefik" / "templates"
BUILD_SPECS = REPO / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
ACCESS_GATEWAY_TASKS = REPO / "roles" / "access_gateway" / "tasks" / "main.yml"
FILTER_COPIES = {
    "root": REPO / "filter_plugins" / "bay_filters.py",
    "container_lifecycle": REPO / "roles" / "container_lifecycle" / "filter_plugins" / "bay_filters.py",
}
LABEL = "traefik.http.middlewares.vpn-only.ipallowlist.sourcerange"

PROXIES = {
    "notes": {
        "domains": ["notes.ts.example.com"],
        "upstream": "http://laptop.example.tailnet.internal:8080",
        "identity_inject": True,
    },
    "nas": {"domains": ["nas.ts.example.com"], "upstream": "http://100.64.0.9:5000"},
}

ALLOWLISTS = [
    ["127.0.0.1", "::1", "100.64.0.0/10"],
    ["127.0.0.1", "10.8.0.0/24", "100.64.0.0/10", "fd7a:115c:a1e0::/48"],
    ["127.0.0.1"],
]


def _load_filters(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"bay_filters_{path.parent.parent.name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_file(**extra: Any) -> dict[str, Any]:
    env = make_ansible_env(TRAEFIK_TEMPLATES)
    env.filters["regex_replace"] = lambda v, p, r: re.sub(p, r, v)
    ctx = {
        "ansible_managed": "test",
        "tailnet_proxies": PROXIES,
        "traefik_dns_resolver_name": "letsencrypt_dns",
        "tailnet_ingress_cert_domain": "*.ts.example.com",
        "traefik_split_entrypoints": True,
        "tailnet_identity_enabled": True,
        "tailnet_identity_port": 9200,
        "tailnet_identity_device_header": "X-Tailnet-Device",
        "tailnet_identity_device_id_header": "X-Tailnet-Device-Id",
        **extra,
    }
    return yaml.safe_load(env.get_template("dynamic/tailnet-proxies.yml.j2").render(**ctx))


def _label_config_allowlist(**ctx: Any) -> list[str]:
    """Evaluate `_traefik_label_config.vpn_allowed_ips` of build_specs.yml."""
    tasks = yaml.safe_load(BUILD_SPECS.read_text())
    task = next(t for t in tasks if t.get("name") == "Build traefik container spec")
    expr = task["vars"]["_traefik_label_config"]["vpn_allowed_ips"].strip()
    expr = expr.removeprefix("{{").removesuffix("}}")
    return list(Environment().compile_expression(expr)(**ctx))


@pytest.mark.parametrize("copy", sorted(FILTER_COPIES))
@pytest.mark.parametrize("allowlist", ALLOWLISTS)
def test_file_copy_matches_the_docker_label(copy: str, allowlist: list[str]) -> None:
    filters = _load_filters(FILTER_COPIES[copy])
    label_ranges = filters.bay_traefik_global_labels({"vpn_allowed_ips": allowlist})[LABEL]
    file_ranges = _render_file(vpn_allowed_ips=allowlist)["http"]["middlewares"]["vpn-only"]
    assert file_ranges == {"ipAllowList": {"sourceRange": label_ranges.split(",")}}


def test_defaults_match_when_the_fact_is_unset() -> None:
    """No vpn_allowed_ips at all: both copies fall back to the same list."""
    label_default = _label_config_allowlist()
    file_ranges = _render_file()["http"]["middlewares"]["vpn-only"]["ipAllowList"]["sourceRange"]
    assert file_ranges == label_default == ["127.0.0.1"]
    # And with the fact set, build_specs passes it through unchanged.
    assert _label_config_allowlist(vpn_allowed_ips=ALLOWLISTS[1]) == ALLOWLISTS[1]


@pytest.mark.parametrize("identity", [True, False])
def test_routers_use_only_middlewares_of_this_file(identity: bool) -> None:
    http = _render_file(tailnet_identity_enabled=identity)["http"]
    defined = set(http["middlewares"])
    for name, router in http["routers"].items():
        for mw in router["middlewares"]:
            provider_name, _, provider = mw.partition("@")
            assert provider == "file", f"{name} names {mw}, not a file-provider middleware"
            assert provider_name in defined, f"{name} names {mw}, which this file does not define"
        assert "vpn-only@file" in router["middlewares"]


def test_template_names_no_docker_middleware() -> None:
    text = (TRAEFIK_TEMPLATES / "dynamic" / "tailnet-proxies.yml.j2").read_text()
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code if "@docker" in line]


def test_tailnet_range_is_appended_under_every_tag() -> None:
    """`--tags traefik` renders the file; it must see the same list as deploy_stack."""
    tasks = yaml.safe_load(ACCESS_GATEWAY_TASKS.read_text())
    task = next(
        t for t in tasks
        if t.get("name") == "Ensure headscale tailnet CIDR is allowlisted (append, not replace)"
    )
    assert "always" in task["tags"]
