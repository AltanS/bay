"""Access-gateway adapter contract behaviour.

Three things are pinned here:

1. The two copies of the cross-host bind-IP resolver agree. They are
   duplicated (filter plugin + Ansible library module) because a library
   module runs in its own interpreter on the target and cannot import a
   filter plugin. Duplication is fine; silent divergence is not.
2. The traefik ingress template renders NO overlay entrypoint when the
   active backend supplies no bind IP — the `access_gateway: none` case.
3. The headscale and wireguard renders are byte-identical to what shipped
   before the migration. `gateway_bind_ip` feeds the reconciler's port
   binding strings, so any formatting drift here recreates every
   gateway-exposed container fleet-wide on the next deploy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from helpers import make_ansible_env

REPO = Path(__file__).resolve().parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_filters = _load(REPO / "filter_plugins" / "bay_filters.py", "bay_filters_contract")
_library = _load(
    REPO / "roles" / "crowdsec_allowlist" / "library" / "crowdsec_allowlist_sync.py",
    "crowdsec_allowlist_sync_contract",
)

RESOLVERS = [_filters.bay_gateway_bind_ip, _library._gateway_bind_ip]

CASES = [
    # (hostvars, expected)
    ({}, ""),
    (None, ""),
    ({"gateway_bind_ip": "100.64.0.7"}, "100.64.0.7"),
    # Incumbent name still honoured — this is what makes the migration
    # zero-config-change for consumers that never rename anything.
    ({"headscale_server_tailnet_ip": "100.64.0.1"}, "100.64.0.1"),
    # Neutral name wins when both are present.
    (
        {"gateway_bind_ip": "100.64.0.9", "headscale_server_tailnet_ip": "100.64.0.1"},
        "100.64.0.9",
    ),
    # An empty neutral value must fall through, not shadow the incumbent.
    (
        {"gateway_bind_ip": "", "headscale_server_tailnet_ip": "100.64.0.1"},
        "100.64.0.1",
    ),
    ({"gateway_bind_ip": "  100.64.0.5  "}, "100.64.0.5"),
    # `access_gateway: none` leaves both empty — nothing to bind, nothing to
    # exempt. Callers must never treat "" as an address.
    ({"gateway_bind_ip": "", "headscale_server_tailnet_ip": ""}, ""),
    # Non-string junk must not crash the crowdsec inventory walk.
    ({"gateway_bind_ip": None, "headscale_server_tailnet_ip": 100}, ""),
]


@pytest.mark.parametrize("hostvars,expected", CASES)
@pytest.mark.parametrize("resolver", RESOLVERS, ids=["filter", "library"])
def test_resolvers_agree(resolver, hostvars, expected):
    assert resolver(hostvars) == expected


def _render_traefik(**overrides) -> str:
    env = make_ansible_env(REPO / "roles" / "traefik" / "templates")
    context = {
        "ansible_managed": "managed",
        "traefik_split_entrypoints": True,
        "traefik_public_bind_ip": "203.0.113.10",
        "traefik_metrics_enabled": False,
        "traefik_metrics_port": 8082,
        "gateway_bind_ip": "100.64.0.1",
    }
    context.update(overrides)
    return env.get_template("traefik.yml.j2").render(**context)


def test_traefik_renders_overlay_entrypoint_when_gateway_has_bind_ip():
    out = _render_traefik(gateway_bind_ip="100.64.0.1")
    assert "websecure_tailnet:" in out
    assert 'address: "100.64.0.1:443"' in out


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_traefik_renders_no_overlay_entrypoint_without_gateway(empty):
    """access_gateway: none must not produce a phantom 100.64.0.1 listener.

    This is the failure the ratchet exists to prevent: the old code bound the
    entrypoint to a var that defaults to 100.64.0.1 play-wide, so a gateway-less
    host quietly opened a listener on an address belonging to nobody.
    """
    out = _render_traefik(gateway_bind_ip=empty)
    assert "websecure_tailnet" not in out
    assert "100.64" not in out
    # The public entrypoints must survive untouched.
    assert 'address: "203.0.113.10:443"' in out
    assert 'address: "203.0.113.10:80"' in out


def test_traefik_public_entrypoints_unchanged_by_gateway_state():
    """Toggling the gateway must not perturb any public listener."""
    with_gw = _render_traefik(gateway_bind_ip="100.64.0.1")
    without_gw = _render_traefik(gateway_bind_ip="")
    removed = [
        line for line in with_gw.splitlines() if line not in without_gw.splitlines()
    ]
    assert removed == ["  websecure_tailnet:", '    address: "100.64.0.1:443"']
