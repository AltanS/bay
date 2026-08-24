"""Tests for filter_plugins/bay_filters.py — Traefik labels, watchtower,
healthcheck, volume prefixing, repo slugs, and build deduplication."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# filter_plugins is outside src/, add it to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "filter_plugins"))

from bay_filters import (
    bay_build_dedup_map,
    bay_healthcheck,
    bay_prefix_volumes,
    bay_repo_groups,
    bay_repo_slug,
    bay_traefik_global_labels,
    bay_traefik_labels,
    bay_watchtower_labels,
    parse_env_file,
    to_docker_networks,
)


# ── Minimal config for traefik label tests ─────────────────────────────

_BASE_CONFIG = {
    "traefik_docker_network": "services",
    "traefik_security_headers_enabled": True,
    "traefik_compress_enabled": True,
    "traefik_error_pages_enabled": False,
    "traefik_rate_limit_average": 100,
    "traefik_rate_limit_burst": 50,
    "traefik_rate_limit_period": "1s",
    "traefik_in_flight_req_amount": 100,
    "traefik_retry_attempts": 4,
    "traefik_circuit_breaker_expression": "NetworkErrorRatio() > 0.5",
}


# ── parse_env_file ─────────────────────────────────────────────────────


class TestParseEnvFile:
    def test_simple_key_value(self) -> None:
        assert parse_env_file("FOO=bar") == {"FOO": "bar"}

    def test_value_with_equals(self) -> None:
        assert parse_env_file("DSN=host=db port=5432") == {"DSN": "host=db port=5432"}

    def test_comments_and_blanks_skipped(self) -> None:
        content = "# comment\n\nFOO=1\n  # another\nBAR=2"
        assert parse_env_file(content) == {"FOO": "1", "BAR": "2"}

    def test_dollar_unescaping(self) -> None:
        assert parse_env_file("PASS=$$ecret$$") == {"PASS": "$ecret$"}


# ── bay_prefix_volumes ────────────────────────────────────────────────


class TestPrefixVolumes:
    def test_named_volume_gets_prefixed(self) -> None:
        result = bay_prefix_volumes(["data:/var/data"], "myapp")
        assert result == ["myapp_data:/var/data"]

    def test_bind_mount_unchanged(self) -> None:
        result = bay_prefix_volumes(["/host/path:/container"], "myapp")
        assert result == ["/host/path:/container"]

    def test_dot_path_unchanged(self) -> None:
        result = bay_prefix_volumes(["./local:/app"], "myapp")
        assert result == ["./local:/app"]

    def test_none_input(self) -> None:
        assert bay_prefix_volumes(None, "myapp") == []

    def test_multiple_volumes(self) -> None:
        vols = ["db_data:/data", "/etc/conf:/conf", "./src:/app"]
        result = bay_prefix_volumes(vols, "stack")
        assert result == ["stack_db_data:/data", "/etc/conf:/conf", "./src:/app"]


# ── to_docker_networks ─────────────────────────────────────────────────


class TestToDockerNetworks:
    def test_converts_list(self) -> None:
        assert to_docker_networks(["services"]) == [{"name": "services"}]

    def test_empty_list(self) -> None:
        assert to_docker_networks([]) == []

    def test_none_input(self) -> None:
        assert to_docker_networks(None) == []


# ── bay_traefik_labels — public service ───────────────────────────────


class TestTraefikLabelsPublic:
    def test_basic_public_service(self) -> None:
        svc = {
            "access": "public",
            "domains": ["app.example.com"],
            "ports": {"internal": 8080},
        }
        labels = bay_traefik_labels(svc, "app", _BASE_CONFIG)
        assert labels["traefik.enable"] == "true"
        assert labels["traefik.docker.network"] == "services"
        assert labels["traefik.http.routers.app.rule"] == "Host(`app.example.com`)"
        assert labels["traefik.http.routers.app.entrypoints"] == "websecure"
        assert labels["traefik.http.routers.app.tls.certresolver"] == "letsencrypt"
        assert "public-chain" in labels["traefik.http.routers.app.middlewares"]
        assert labels["traefik.http.services.app.loadbalancer.server.port"] == "8080"

    def test_multi_domain(self) -> None:
        svc = {
            "access": "public",
            "domains": ["a.example.com", "b.example.com"],
            "ports": {"internal": 80},
        }
        labels = bay_traefik_labels(svc, "web", _BASE_CONFIG)
        rule = labels["traefik.http.routers.web.rule"]
        assert "Host(`a.example.com`)" in rule
        assert "Host(`b.example.com`)" in rule
        assert "||" in rule

    def test_public_with_vpn_routes_creates_dual_router(self) -> None:
        svc = {
            "access": "public",
            "domains": ["app.example.com"],
            "ports": {"internal": 8080},
            "vpn_routes": ["/admin", "/api/internal"],
        }
        labels = bay_traefik_labels(svc, "app", _BASE_CONFIG)
        # Primary router (public, lower priority)
        assert labels["traefik.http.routers.app.priority"] == "10"
        assert "public-chain" in labels["traefik.http.routers.app.middlewares"]
        # Secondary router (vpn, higher priority, path-matched)
        assert labels["traefik.http.routers.app-vpn.priority"] == "20"
        assert "vpn-chain" in labels["traefik.http.routers.app-vpn.middlewares"]
        rule = labels["traefik.http.routers.app-vpn.rule"]
        assert "PathPrefix(`/admin`)" in rule
        assert "PathPrefix(`/api/internal`)" in rule
        # Shared service backend
        assert labels["traefik.http.services.app.loadbalancer.server.port"] == "8080"


# ── bay_traefik_labels — VPN service ──────────────────────────────────


class TestTraefikLabelsVpn:
    def test_basic_vpn_service(self) -> None:
        svc = {
            "access": "vpn",
            "domains": ["internal.example.com"],
            "ports": {"internal": 3000},
        }
        labels = bay_traefik_labels(svc, "dash", _BASE_CONFIG)
        assert "vpn-chain" in labels["traefik.http.routers.dash-vpn.middlewares"]
        assert labels["traefik.http.routers.dash-vpn.rule"] == "Host(`internal.example.com`)"

    def test_vpn_with_public_routes_creates_dual_router(self) -> None:
        svc = {
            "access": "vpn",
            "domains": ["api.example.com"],
            "ports": {"internal": 8080},
            "public_routes": ["/webhook", "/health"],
        }
        labels = bay_traefik_labels(svc, "api", _BASE_CONFIG)
        # Primary = vpn (lower priority)
        assert labels["traefik.http.routers.api-vpn.priority"] == "10"
        assert "vpn-chain" in labels["traefik.http.routers.api-vpn.middlewares"]
        # Secondary = public routes (higher priority)
        assert labels["traefik.http.routers.api-public.priority"] == "20"
        assert "public-chain" in labels["traefik.http.routers.api-public.middlewares"]


# ── bay_traefik_labels — dual-router multi-domain (GH#31) ─────────────
# Regression: _add_dual_router_labels took a singular domain and both call
# sites passed domains[0], so every domain after the first got no router and
# no cert — a silent 404 (surfaced as Googlebot 404s on a www. host).


class TestTraefikLabelsDualRouterMultiDomain:
    def test_single_domain_output_unchanged(self) -> None:
        """One domain → no stray parens, byte-identical to pre-fix labels."""
        svc = {
            "access": "public",
            "domains": ["app.example.com"],
            "ports": {"internal": 8080},
            "vpn_routes": ["/admin"],
        }
        labels = bay_traefik_labels(svc, "app", _BASE_CONFIG)
        assert labels["traefik.http.routers.app.rule"] == "Host(`app.example.com`)"
        assert (
            labels["traefik.http.routers.app-vpn.rule"]
            == "Host(`app.example.com`) && (PathPrefix(`/admin`))"
        )

    def test_public_vpn_routes_all_domains_routed(self) -> None:
        svc = {
            "access": "public",
            "domains": ["blogco.com", "www.blogco.com", "blogco.de"],
            "ports": {"internal": 8080},
            "vpn_routes": ["/super"],
        }
        labels = bay_traefik_labels(svc, "blogco", _BASE_CONFIG)
        host_expr = "Host(`blogco.com`) || Host(`www.blogco.com`) || Host(`blogco.de`)"
        # Primary catch-all router lists every domain
        assert labels["traefik.http.routers.blogco.rule"] == host_expr
        # Secondary path-matched router does too — OR-group parenthesised
        assert (
            labels["traefik.http.routers.blogco-vpn.rule"]
            == f"({host_expr}) && (PathPrefix(`/super`))"
        )

    def test_vpn_public_routes_all_domains_routed(self) -> None:
        svc = {
            "access": "vpn",
            "domains": ["api.example.com", "api2.example.com"],
            "ports": {"internal": 8080},
            "public_routes": ["/webhook", "/health"],
        }
        labels = bay_traefik_labels(svc, "api", _BASE_CONFIG)
        host_expr = "Host(`api.example.com`) || Host(`api2.example.com`)"
        assert labels["traefik.http.routers.api-vpn.rule"] == host_expr
        assert labels["traefik.http.routers.api-public.rule"] == (
            f"({host_expr}) && (PathPrefix(`/webhook`) || PathPrefix(`/health`))"
        )

    def test_multi_domain_or_group_is_parenthesised(self) -> None:
        """`&&` binds tighter than `||`: an unparenthesised OR-group would
        make the path-matched router swallow the first domain wholesale,
        exposing VPN-only paths publicly (or vice versa)."""
        svc = {
            "access": "public",
            "domains": ["a.example.com", "b.example.com"],
            "ports": {"internal": 80},
            "vpn_routes": ["/admin"],
        }
        rule = bay_traefik_labels(svc, "app", _BASE_CONFIG)[
            "traefik.http.routers.app-vpn.rule"
        ]
        assert rule.startswith("(Host(")
        assert rule.index(")) &&") > rule.index("||")

    def test_single_router_multi_domain_unchanged(self) -> None:
        """No vpn_routes → single-router path keeps its joined rule."""
        svc = {
            "access": "public",
            "domains": ["a.example.com", "b.example.com", "c.example.com"],
            "ports": {"internal": 80},
        }
        labels = bay_traefik_labels(svc, "web", _BASE_CONFIG)
        assert labels["traefik.http.routers.web.rule"] == (
            "Host(`a.example.com`) || Host(`b.example.com`) || Host(`c.example.com`)"
        )
        assert "traefik.http.routers.web-vpn.rule" not in labels


# ── bay_traefik_labels — entrypoints split (tailnet ingress) ──────────
# Regression: the reconciler builds labels via this filter, NOT _service.j2.
# A split host sets vpn_entrypoints so VPN routers also bind the tailnet
# listener; without this the reconciler hardcoded "websecure" and a VPN
# service (e.g. gatus) fell off the tailnet on a split host.


class TestTraefikLabelsEntrypoints:
    _SPLIT = {
        **_BASE_CONFIG,
        "vpn_entrypoints": "websecure,websecure_tailnet",
        "public_entrypoints": "websecure",
    }

    def test_default_entrypoints_unchanged(self) -> None:
        """No split vars → byte-identical 'websecure' for every router class."""
        vpn = {"access": "vpn", "domains": ["d.example.com"], "ports": {"internal": 80}}
        pub = {"access": "public", "domains": ["p.example.com"], "ports": {"internal": 80}}
        assert (
            bay_traefik_labels(vpn, "d", _BASE_CONFIG)[
                "traefik.http.routers.d-vpn.entrypoints"
            ]
            == "websecure"
        )
        assert (
            bay_traefik_labels(pub, "p", _BASE_CONFIG)[
                "traefik.http.routers.p.entrypoints"
            ]
            == "websecure"
        )

    def test_split_vpn_router_binds_tailnet(self) -> None:
        svc = {"access": "vpn", "domains": ["status.example.com"], "ports": {"internal": 8080}}
        labels = bay_traefik_labels(svc, "gatus", self._SPLIT)
        assert (
            labels["traefik.http.routers.gatus-vpn.entrypoints"]
            == "websecure,websecure_tailnet"
        )

    def test_split_public_router_stays_public(self) -> None:
        svc = {"access": "public", "domains": ["app.example.com"], "ports": {"internal": 80}}
        labels = bay_traefik_labels(svc, "app", self._SPLIT)
        assert labels["traefik.http.routers.app.entrypoints"] == "websecure"

    def test_split_dual_router_each_class_correct(self) -> None:
        """vpn-access service with public_routes: vpn router → tailnet, public → public."""
        svc = {
            "access": "vpn",
            "domains": ["api.example.com"],
            "ports": {"internal": 8080},
            "public_routes": ["/webhook"],
        }
        labels = bay_traefik_labels(svc, "api", self._SPLIT)
        assert (
            labels["traefik.http.routers.api-vpn.entrypoints"]
            == "websecure,websecure_tailnet"
        )
        assert labels["traefik.http.routers.api-public.entrypoints"] == "websecure"


# ── bay_traefik_labels — per-service middleware ───────────────────────


class TestTraefikLabelsMiddleware:
    def test_rate_limit(self) -> None:
        svc = {
            "access": "public",
            "domains": ["app.example.com"],
            "ports": {"internal": 80},
            "middleware": {"rate_limit": {"average": 200, "burst": 100}},
        }
        labels = bay_traefik_labels(svc, "app", _BASE_CONFIG)
        pfx = "traefik.http.middlewares.app-ratelimit.ratelimit"
        assert labels[f"{pfx}.average"] == "200"
        assert labels[f"{pfx}.burst"] == "100"
        assert labels[f"{pfx}.period"] == "1s"  # default from config
        assert "app-ratelimit" in labels["traefik.http.routers.app.middlewares"]

    def test_in_flight_req(self) -> None:
        svc = {
            "access": "public",
            "domains": ["app.example.com"],
            "ports": {"internal": 80},
            "middleware": {"in_flight_req": {"amount": 50}},
        }
        labels = bay_traefik_labels(svc, "app", _BASE_CONFIG)
        pfx = "traefik.http.middlewares.app-inflightreq.inflightreq"
        assert labels[f"{pfx}.amount"] == "50"

    def test_basic_auth_pre_hashed(self) -> None:
        """Pre-hashed users must be stored with single $, not doubled $$.

        Labels are set via Docker API (community.docker.docker_container), not
        via docker compose, so no $$->$ interpolation occurs. Traefik expects
        raw APR1 hashes starting with $apr1$; $$ causes all credentials to
        fail silently.
        """
        svc = {
            "access": "public",
            "domains": ["app.example.com"],
            "ports": {"internal": 80},
            "middleware": {"basic_auth": {"users": ["admin:$apr1$xyz$hash"]}},
        }
        labels = bay_traefik_labels(svc, "app", _BASE_CONFIG)
        pfx = "traefik.http.middlewares.app-basicauth.basicauth"
        users_value = labels[f"{pfx}.users"]
        assert "admin:" in users_value
        assert labels[f"{pfx}.removeheader"] == "true"
        # Critical: no $$ in label — Docker API stores values as-is
        assert "$$" not in users_value, (
            f"basicauth label contains $$ (Compose escape): {users_value!r}. "
            "Docker API passes labels verbatim to Docker; $$ is an invalid "
            "APR1 separator and causes Traefik to reject all credentials."
        )
        # The single-$ APR1 hash must be preserved intact
        assert "$apr1$xyz$hash" in users_value

    @patch("bay_filters.subprocess.run")
    def test_basic_auth_with_credentials(self, mock_run: MagicMock) -> None:
        """Credential-based basic_auth must emit single $ in APR1 hash.

        The Python filter calls openssl passwd -apr1 which returns a hash with
        single-$ separators (e.g. $apr1$salt$hash). The label must store this
        as-is — no $->$$ escaping — because Docker API does not interpolate.
        """
        mock_run.return_value = MagicMock(stdout="$apr1$salt$hashed\n")
        svc = {
            "access": "public",
            "domains": ["app.example.com"],
            "ports": {"internal": 80},
            "middleware": {"basic_auth": {"credentials": [{"username": "admin", "password": "secret"}]}},
        }
        labels = bay_traefik_labels(svc, "app", {**_BASE_CONFIG, "stack_name": "myapp"})
        pfx = "traefik.http.middlewares.app-basicauth.basicauth"
        users_value = labels[f"{pfx}.users"]
        assert "admin:" in users_value
        mock_run.assert_called_once()
        # Critical: no $$ in label — openssl returns single-$ APR1, must stay single-$
        assert "$$" not in users_value, (
            f"basicauth label contains $$ (should be single $): {users_value!r}"
        )
        assert "admin:$apr1$salt$hashed" == users_value

    def test_circuit_breaker(self) -> None:
        svc = {
            "access": "public",
            "domains": ["app.example.com"],
            "ports": {"internal": 80},
            "middleware": {"circuit_breaker": {"expression": "NetworkErrorRatio() > 0.3"}},
        }
        labels = bay_traefik_labels(svc, "app", _BASE_CONFIG)
        key = "traefik.http.middlewares.app-circuitbreaker.circuitbreaker.expression"
        assert labels[key] == "NetworkErrorRatio() > 0.3"

    def test_retry(self) -> None:
        svc = {
            "access": "public",
            "domains": ["app.example.com"],
            "ports": {"internal": 80},
            "middleware": {"retry": {"attempts": 3}},
        }
        labels = bay_traefik_labels(svc, "app", _BASE_CONFIG)
        key = "traefik.http.middlewares.app-retry.retry.attempts"
        assert labels[key] == "3"

    def test_custom_chain_when_headers_disabled(self) -> None:
        svc = {
            "access": "public",
            "domains": ["app.example.com"],
            "ports": {"internal": 80},
            "middleware": {"security_headers": False},
        }
        labels = bay_traefik_labels(svc, "app", _BASE_CONFIG)
        # Should create custom chain instead of using public-chain
        chain_key = "traefik.http.middlewares.app-chain.chain.middlewares"
        assert chain_key in labels
        assert "app-chain" in labels["traefik.http.routers.app.middlewares"]


# ── bay_traefik_global_labels ─────────────────────────────────────────


class TestTraefikGlobalLabels:
    def test_all_features_enabled(self) -> None:
        config = {
            "vpn_allowed_ips": ["10.0.0.0/8", "192.168.1.0/24"],
            "traefik_security_headers_enabled": True,
            "traefik_security_headers": {"sts_seconds": 31536000},
            "traefik_compress_enabled": True,
            "traefik_error_pages_enabled": True,
            "traefik_error_pages_status": "500-599",
            "traefik_error_pages_query": "/{status}.html",
        }
        labels = bay_traefik_global_labels(config)
        assert labels["traefik.enable"] == "true"
        assert labels["traefik.http.services.traefik-noop.loadbalancer.server.port"] == "0"
        assert "10.0.0.0/8" in labels["traefik.http.middlewares.vpn-only.ipallowlist.sourcerange"]
        assert "secure-headers" in labels["traefik.http.middlewares.public-chain.chain.middlewares"]
        assert "compress" in labels["traefik.http.middlewares.public-chain.chain.middlewares"]
        assert "errors" in labels["traefik.http.middlewares.public-chain.chain.middlewares"]
        assert "vpn-only" in labels["traefik.http.middlewares.vpn-chain.chain.middlewares"]

    def test_all_features_disabled(self) -> None:
        config = {
            "traefik_security_headers_enabled": False,
            "traefik_compress_enabled": False,
            "traefik_error_pages_enabled": False,
        }
        labels = bay_traefik_global_labels(config)
        # Chains should be minimal
        pub_chain = labels["traefik.http.middlewares.public-chain.chain.middlewares"]
        vpn_chain = labels["traefik.http.middlewares.vpn-chain.chain.middlewares"]
        assert "secure-headers" not in pub_chain
        assert "compress" not in pub_chain
        assert vpn_chain == "vpn-only"

    def test_security_headers_values(self) -> None:
        config = {
            "traefik_security_headers_enabled": True,
            "traefik_security_headers": {
                "sts_seconds": 31536000,
                "frame_deny": False,
            },
        }
        labels = bay_traefik_global_labels(config)
        pfx = "traefik.http.middlewares.secure-headers.headers"
        assert labels[f"{pfx}.stsSeconds"] == "31536000"
        assert labels[f"{pfx}.frameDeny"] == "false"


# ── bay_watchtower_labels ─────────────────────────────────────────────


class TestWatchtowerLabels:
    def test_auto_mode(self) -> None:
        labels = bay_watchtower_labels("auto")
        assert labels["com.centurylinklabs.watchtower.enable"] == "true"
        assert labels["com.centurylinklabs.watchtower.monitor-only"] == "false"

    def test_monitor_mode(self) -> None:
        labels = bay_watchtower_labels("monitor")
        assert labels["com.centurylinklabs.watchtower.enable"] == "true"
        assert labels["com.centurylinklabs.watchtower.monitor-only"] == "true"

    def test_disabled_mode(self) -> None:
        labels = bay_watchtower_labels(False)
        assert labels["com.centurylinklabs.watchtower.enable"] == "false"

    def test_globally_disabled(self) -> None:
        labels = bay_watchtower_labels("auto", enabled=False)
        assert labels == {}


# ── bay_healthcheck ───────────────────────────────────────────────────


class TestHealthcheck:
    def test_with_path(self) -> None:
        hc = {"path": "/health"}
        result = bay_healthcheck(hc, port=8080)
        assert result["test"] == ["CMD", "wget", "--spider", "-q", "http://localhost:8080/health"]
        assert result["interval"] == "30s"
        assert result["retries"] == 3

    def test_with_custom_test(self) -> None:
        hc = {"test": ["CMD", "curl", "-f", "http://localhost/"]}
        result = bay_healthcheck(hc)
        assert result["test"] == ["CMD", "curl", "-f", "http://localhost/"]

    def test_none_input(self) -> None:
        assert bay_healthcheck(None) is None

    def test_custom_intervals(self) -> None:
        hc = {"path": "/", "interval": "10s", "timeout": "5s", "retries": 5, "start_period": "60s"}
        result = bay_healthcheck(hc)
        assert result["interval"] == "10s"
        assert result["timeout"] == "5s"
        assert result["retries"] == 5
        assert result["start_period"] == "60s"


# ── bay_repo_slug ─────────────────────────────────────────────────────


class TestRepoSlug:
    def test_deterministic(self) -> None:
        slug1 = bay_repo_slug("https://github.com/org/repo.git", "main")
        slug2 = bay_repo_slug("https://github.com/org/repo.git", "main")
        assert slug1 == slug2

    def test_different_branches_different_slugs(self) -> None:
        slug1 = bay_repo_slug("https://github.com/org/repo.git", "main")
        slug2 = bay_repo_slug("https://github.com/org/repo.git", "develop")
        assert slug1 != slug2

    def test_contains_repo_name(self) -> None:
        slug = bay_repo_slug("https://github.com/org/my-app.git", "main")
        assert "my-app" in slug

    def test_filesystem_safe(self) -> None:
        slug = bay_repo_slug("https://github.com/org/My App!.git", "main")
        assert all(c.isalnum() or c == "-" for c in slug)


# ── bay_repo_groups ───────────────────────────────────────────────────


class TestRepoGroups:
    def test_groups_by_repo_branch(self) -> None:
        services = {
            "api": {"build": {"repo": "https://github.com/org/app.git", "branch": "main"}},
            "worker": {"build": {"repo": "https://github.com/org/app.git", "branch": "main"}},
            "docs": {"build": {"repo": "https://github.com/org/docs.git", "branch": "main"}},
        }
        groups = bay_repo_groups(services, ["api", "worker", "docs"])
        assert len(groups) == 2
        # Find the app group
        app_group = next(g for g in groups if len(g["services"]) == 2)
        assert "api" in app_group["services"]
        assert "worker" in app_group["services"]

    def test_token_detection(self) -> None:
        services = {
            "api": {"build": {"repo": "https://github.com/org/app.git", "token": "ghp_xxx"}},
        }
        groups = bay_repo_groups(services, ["api"])
        assert groups[0]["has_token"] is True
        assert "x-access-token" in groups[0]["clone_url"]


# ── bay_build_dedup_map ──────────────────────────────────────────────


class TestBuildDedupMap:
    def test_identical_builds_share_primary(self) -> None:
        services = {
            "api": {"build": {"repo": "r", "branch": "main", "dockerfile": "Dockerfile"}},
            "worker": {"build": {"repo": "r", "branch": "main", "dockerfile": "Dockerfile"}},
        }
        dedup = bay_build_dedup_map(services, ["api", "worker"])
        assert dedup["api"]["primary"] is True
        assert dedup["worker"]["primary"] is False
        assert dedup["worker"]["primary_svc"] == "api"
        assert dedup["api"]["group_size"] == 2

    def test_different_dockerfiles_separate_groups(self) -> None:
        services = {
            "api": {"build": {"repo": "r", "branch": "main", "dockerfile": "Dockerfile"}},
            "worker": {"build": {"repo": "r", "branch": "main", "dockerfile": "Dockerfile.worker"}},
        }
        dedup = bay_build_dedup_map(services, ["api", "worker"])
        assert dedup["api"]["primary"] is True
        assert dedup["worker"]["primary"] is True
        assert dedup["api"]["group_size"] == 1
