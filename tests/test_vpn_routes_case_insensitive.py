"""GH#3: a `vpn_routes` path must require the VPN regardless of letter case.

`vpn_routes` used to render as Traefik `PathPrefix`, which compares
case-sensitively. Backends such as Express and React Router route
case-insensitively, so `/Admin` missed the VPN router, fell through to the
public catch-all router and reached the backend from the open internet.

The rule is now a case-insensitive regular expression with otherwise the same
prefix semantics. The regex is checked here with Python's `re`, which agrees
with Go's RE2 on everything the rule uses: `(?i)`, `^` and one-character
classes over ASCII.

Both copies of `bay_filters.py` are loaded: the reconciler uses the
role-local one, and they are not byte-identical.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml
from test_service_expose_field import _render_service

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COPIES = {
    "root": _REPO_ROOT / "filter_plugins" / "bay_filters.py",
    "container_lifecycle": _REPO_ROOT
    / "roles"
    / "container_lifecycle"
    / "filter_plugins"
    / "bay_filters.py",
}

_CONFIG = {
    "public_mw": ["public-chain"],
    "vpn_mw": ["vpn-chain"],
    "traefik_docker_network": "services",
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"bay_filters_gh3_{name}", _COPIES[name])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=sorted(_COPIES))
def filters(request):
    return _load(request.param)


def _public_svc(vpn_routes, **extra):
    svc = {
        "access": "public",
        "domains": ["app.example.com"],
        "ports": {"internal": 8080},
        "vpn_routes": vpn_routes,
    }
    svc.update(extra)
    return svc


def _vpn_regexes(rule: str) -> list[re.Pattern]:
    return [re.compile(p) for p in re.findall(r"PathRegexp\(`([^`]*)`\)", rule)]


def _vpn_router_matches(rule: str, path: str) -> bool:
    return any(r.search(path) for r in _vpn_regexes(rule))


class TestVpnRouteRule:
    @pytest.mark.parametrize(
        "path", ["/admin", "/Admin", "/ADMIN/some/path", "/aDmIn", "/administrator"]
    )
    def test_every_case_variant_hits_the_vpn_router(self, filters, path):
        labels = filters.bay_traefik_labels(_public_svc(["/admin"]), "app", _CONFIG)
        assert _vpn_router_matches(labels["traefik.http.routers.app-vpn.rule"], path)

    @pytest.mark.parametrize("path", ["/", "/adm", "/public/admin", "/x/ADMIN"])
    def test_other_paths_stay_public(self, filters, path):
        labels = filters.bay_traefik_labels(_public_svc(["/admin"]), "app", _CONFIG)
        assert not _vpn_router_matches(labels["traefik.http.routers.app-vpn.rule"], path)

    def test_no_case_sensitive_matcher_is_left(self, filters):
        labels = filters.bay_traefik_labels(
            _public_svc(["/admin", "/api/internal"]), "app", _CONFIG
        )
        assert "PathPrefix" not in labels["traefik.http.routers.app-vpn.rule"]

    def test_regex_characters_in_a_route_stay_literal(self, filters):
        """The schema allows `.`, `+` and `*`. Unescaped, `.` would match any
        character and `*` would repeat the one before it."""
        labels = filters.bay_traefik_labels(_public_svc(["/v1.0/a+b*"]), "app", _CONFIG)
        rule = labels["traefik.http.routers.app-vpn.rule"]
        assert "\\" not in rule, "a backslash breaks the double-quoted compose YAML"
        assert _vpn_router_matches(rule, "/V1.0/A+B*/x")
        assert not _vpn_router_matches(rule, "/v1x0/a+b*")
        assert not _vpn_router_matches(rule, "/v1.0/aab*")
        assert not _vpn_router_matches(rule, "/v1.0/a+")

    @pytest.mark.parametrize("bad", ["/a(b", "/a?b", "/a|b", "/a$", "/a\\b", "/a[b]"])
    def test_other_regex_characters_are_refused(self, filters, bad):
        with pytest.raises(ValueError, match="regular-expression meaning"):
            filters.bay_traefik_labels(_public_svc([bad]), "app", _CONFIG)

    def test_public_routes_keep_case_sensitive_prefix(self, filters):
        """On a VPN service a case variant of a public route falls through to
        the VPN catch-all, which fails closed. Left as it was on purpose."""
        svc = {
            "access": "vpn",
            "domains": ["api.example.com"],
            "ports": {"internal": 8080},
            "public_routes": ["/webhook"],
        }
        labels = filters.bay_traefik_labels(svc, "api", _CONFIG)
        assert labels["traefik.http.routers.api-public.rule"] == (
            "Host(`api.example.com`) && (PathPrefix(`/webhook`))"
        )


class TestHealthRouterAgreesWithVpnRouter:
    """The health router outranks every other router, so it must pick the
    chain of the router that would otherwise take its path. Picking the
    public chain for a path the VPN router covers serves it publicly."""

    @pytest.mark.parametrize("health", ["/Admin/health", "/administrator/health"])
    def test_health_path_under_a_vpn_route_gets_the_vpn_chain(self, filters, health):
        svc = _public_svc(
            ["/admin"],
            healthcheck_path=health,
            middleware={"basic_auth": {"users": ["user:hash"]}},
        )
        labels = filters.bay_traefik_labels(svc, "app", _CONFIG)
        chain = labels["traefik.http.routers.app-health.middlewares"]
        assert "vpn-chain" in chain
        assert "public-chain" not in chain

    def test_health_path_outside_vpn_routes_stays_public(self, filters):
        svc = _public_svc(
            ["/admin"],
            healthcheck_path="/healthz",
            middleware={"basic_auth": {"users": ["user:hash"]}},
        )
        labels = filters.bay_traefik_labels(svc, "app", _CONFIG)
        assert "public-chain" in labels["traefik.http.routers.app-health.middlewares"]


class TestComposeTemplateAgrees:
    """The compose file is documentation only, but it must not document the
    old rule. It must also stay valid YAML."""

    def test_template_renders_the_same_rule_as_the_filter(self):
        svc = _public_svc(["/admin", "/v1.0/a+b*"])
        rendered = _render_service("app", svc)
        line = next(
            ln for ln in rendered.splitlines() if "routers.app-vpn.rule=" in ln
        )
        label = yaml.safe_load(line.strip())[0]
        rule = label.split("=", 1)[1]
        expected = _load("root").bay_traefik_labels(svc, "app", _CONFIG)[
            "traefik.http.routers.app-vpn.rule"
        ]
        assert rule == expected

    @pytest.mark.parametrize(
        ("health", "chain"),
        [("/Admin/health", "vpn-chain"), ("/administrator", "vpn-chain"), ("/healthz", "public-chain")],
    )
    def test_template_health_chain_agrees_with_the_filter(self, health, chain):
        svc = _public_svc(
            ["/admin"],
            healthcheck_path=health,
            middleware={"basic_auth": {"users": ["user:hash"]}},
        )
        rendered = _render_service(
            "app", svc, public_mw="public-chain", vpn_mw="vpn-chain"
        )
        line = next(
            ln for ln in rendered.splitlines() if "routers.app-health.middlewares=" in ln
        )
        assert chain in line
