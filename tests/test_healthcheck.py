"""Tests for the post-deploy reachability audit (M83-S11).

Covers the pure-logic layer in `src/bay_cli/healthcheck.py`:
- VPN-only services are skipped by default, included with --include-vpn
- Services with `public_routes` are NOT skipped even if access=vpn
- Service filter (`only`) narrows to one service
- Parallel probe + summarize
- CheckResult.to_dict() matches the JSON schema in the spec

HTTP calls are mocked — tests never hit real endpoints.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bay_cli.healthcheck import (
    DEFINITIVE_BUDGET_S,
    STARTUP_BUDGET_S,
    CheckResult,
    _attempt_cap,
    _collect_targets,
    check_domain,
    classify_failure,
    expand_domain,
    readiness_note,
    run_healthcheck,
    should_skip_vpn_only,
    startup_budget_for,
    summarize,
)


# ── VPN skip logic ────────────────────────────────────────────────────


class TestShouldSkipVpnOnly:
    def test_public_service_not_skipped(self):
        svc = {"access": "public", "domains": ["example.com"]}
        skip, reason = should_skip_vpn_only(svc, include_vpn=False)
        assert skip is False
        assert reason is None

    def test_vpn_only_skipped_by_default(self):
        svc = {"access": "vpn", "domains": ["vpn.example.com"]}
        skip, reason = should_skip_vpn_only(svc, include_vpn=False)
        assert skip is True
        assert reason == "VPN-only"

    def test_vpn_public_routes_probe_path_covered(self):
        """access=vpn + public_routes where healthcheck_path IS covered
        by a public route → probe it."""
        svc = {
            "access": "vpn",
            "domains": ["vpn.example.com"],
            "public_routes": ["/api"],
            "healthcheck_path": "/api/health",
        }
        skip, reason = should_skip_vpn_only(svc, include_vpn=False)
        assert skip is False
        assert reason is None

    def test_vpn_public_routes_probe_path_not_covered(self):
        """access=vpn + public_routes where healthcheck_path is NOT
        covered by any public route → skip to avoid a false-positive
        403 from Traefik's IPAllowList (M85-S12 regression from
        demo email-service 2026-04-23)."""
        svc = {
            "access": "vpn",
            "domains": ["email.example.com"],
            "public_routes": ["/v1/webhooks/ses"],
            "healthcheck_path": "/healthz",
        }
        skip, reason = should_skip_vpn_only(svc, include_vpn=False)
        assert skip is True
        assert reason == "VPN-only (healthcheck_path not in public_routes)"

    def test_vpn_public_routes_default_path_root_not_covered(self):
        """access=vpn + public_routes but no healthcheck_path → default
        probe is `/`, which is not covered by any non-root public
        route — skip."""
        svc = {
            "access": "vpn",
            "domains": ["vpn.example.com"],
            "public_routes": ["/api/health"],
        }
        skip, reason = should_skip_vpn_only(svc, include_vpn=False)
        assert skip is True
        assert reason == "VPN-only (healthcheck_path not in public_routes)"

    def test_vpn_public_routes_exact_match(self):
        """Exact path match (healthcheck_path == one of public_routes)
        is treated as covered."""
        svc = {
            "access": "vpn",
            "domains": ["vpn.example.com"],
            "public_routes": ["/healthz"],
            "healthcheck_path": "/healthz",
        }
        skip, reason = should_skip_vpn_only(svc, include_vpn=False)
        assert skip is False

    def test_vpn_public_routes_prefix_boundary(self):
        """`/foo` covers `/foo/bar` but NOT `/food` (PathPrefix
        semantics — boundary is on a `/`, not a substring)."""
        svc = {
            "access": "vpn",
            "domains": ["vpn.example.com"],
            "public_routes": ["/foo"],
            "healthcheck_path": "/food",
        }
        skip, _ = should_skip_vpn_only(svc, include_vpn=False)
        assert skip is True

    def test_vpn_empty_public_routes_still_skipped(self):
        svc = {"access": "vpn", "domains": ["vpn.example.com"], "public_routes": []}
        skip, reason = should_skip_vpn_only(svc, include_vpn=False)
        assert skip is True

    def test_vpn_included_when_flag_set(self):
        svc = {"access": "vpn", "domains": ["vpn.example.com"]}
        skip, reason = should_skip_vpn_only(svc, include_vpn=True)
        assert skip is False

    def test_no_access_field_treated_as_public(self):
        """Legacy services without `access:` are assumed public."""
        svc = {"domains": ["legacy.example.com"]}
        skip, reason = should_skip_vpn_only(svc, include_vpn=False)
        assert skip is False


# ── Target collection ─────────────────────────────────────────────────


class TestCollectTargets:
    def test_flattens_services_and_domains(self):
        services = {
            "svc-a": {"access": "public", "domains": ["a.example.com"]},
            "svc-b": {"access": "public", "domains": ["b.example.com", "b2.example.com"]},
        }
        targets = _collect_targets(services, include_vpn=False)
        assert [(n, d, s) for n, d, _p, s, _r in targets] == [
            ("svc-a", "a.example.com", False),
            ("svc-b", "b.example.com", False),
            ("svc-b", "b2.example.com", False),
        ]

    def test_skip_vpn_default(self):
        services = {
            "public-svc": {"access": "public", "domains": ["a.example.com"]},
            "vpn-svc": {"access": "vpn", "domains": ["v.example.com"]},
        }
        targets = _collect_targets(services, include_vpn=False)
        vpn_target = next(t for t in targets if t[0] == "vpn-svc")
        assert vpn_target[3] is True  # skip=True
        assert vpn_target[4] == "VPN-only"

    def test_only_filter_narrows_to_single_service(self):
        services = {
            "svc-a": {"access": "public", "domains": ["a.example.com"]},
            "svc-b": {"access": "public", "domains": ["b.example.com"]},
        }
        targets = _collect_targets(services, include_vpn=False, only="svc-a")
        assert len(targets) == 1
        assert targets[0][0] == "svc-a"

    def test_services_without_domains_ignored(self):
        services = {
            "svc-a": {"access": "public"},  # no domains
            "svc-b": {"access": "public", "domains": ["b.example.com"]},
        }
        targets = _collect_targets(services, include_vpn=False)
        assert len(targets) == 1
        assert targets[0][0] == "svc-b"


# ── CheckResult → dict (JSON schema) ──────────────────────────────────


class TestCheckResultDict:
    def test_dict_matches_spec_schema(self):
        """Spec JSON schema: service, domain, status, ok, redirect_chain,
        error, attempts, elapsed_ms, skipped, skip_reason."""
        r = CheckResult(
            service="svc",
            domain="example.com",
            status=200,
            ok=True,
            redirect_chain=["https://example.com/login"],
            attempts=1,
            elapsed_ms=124,
        )
        d = r.to_dict()
        assert set(d.keys()) == {
            "service", "domain", "status", "ok", "redirect_chain",
            "error", "attempts", "elapsed_ms", "skipped", "skip_reason",
            "probed_url",
        }
        assert d["ok"] is True
        assert d["status"] == 200

    def test_failed_result_shape(self):
        r = CheckResult(
            service="svc", domain="example.com", status=502, ok=False,
        )
        d = r.to_dict()
        assert d["ok"] is False
        assert d["status"] == 502


# ── run_healthcheck end-to-end with mocked HTTP ───────────────────────


class TestRunHealthcheck:
    def test_empty_services_returns_empty(self):
        assert run_healthcheck({}) == []

    def test_skipped_services_marked(self):
        services = {
            "public-svc": {"access": "public", "domains": ["a.example.com"]},
            "vpn-svc": {"access": "vpn", "domains": ["v.example.com"]},
        }
        with patch("bay_cli.healthcheck.check_domain") as probe:
            probe.return_value = CheckResult(
                service="public-svc", domain="a.example.com", status=200, ok=True
            )
            results = run_healthcheck(services)
        assert len(results) == 2
        skipped = [r for r in results if r.skipped]
        assert len(skipped) == 1
        assert skipped[0].service == "vpn-svc"
        # check_domain only invoked for the non-skipped target
        probe.assert_called_once_with(
            "public-svc", "a.example.com", "/", startup_budget_s=STARTUP_BUDGET_S
        )

    def test_result_order_matches_target_order(self):
        services = {
            "svc-a": {"access": "public", "domains": ["a.example.com"]},
            "svc-b": {"access": "public", "domains": ["b.example.com"]},
        }
        # return results out of order — parallel futures don't preserve order
        def fake_check(name, domain, path="/", **_kw):
            return CheckResult(service=name, domain=domain, status=200, ok=True)

        with patch("bay_cli.healthcheck.check_domain", side_effect=fake_check):
            results = run_healthcheck(services)
        assert [r.domain for r in results] == ["a.example.com", "b.example.com"]

    def test_service_filter_narrows_to_one(self):
        services = {
            "svc-a": {"access": "public", "domains": ["a.example.com"]},
            "svc-b": {"access": "public", "domains": ["b.example.com"]},
        }
        with patch("bay_cli.healthcheck.check_domain") as probe:
            probe.return_value = CheckResult(
                service="svc-a", domain="a.example.com", status=200, ok=True
            )
            results = run_healthcheck(services, only="svc-a")
        assert len(results) == 1
        assert results[0].service == "svc-a"


# ── summarize ─────────────────────────────────────────────────────────


class TestSummarize:
    def test_counts(self):
        results = [
            CheckResult(service="a", domain="x.com", status=200, ok=True),
            CheckResult(service="b", domain="y.com", status=502, ok=False),
            CheckResult(service="c", domain="z.com", status=None, ok=True, skipped=True),
        ]
        s = summarize(results)
        assert s == {"total": 3, "passed": 1, "failed": 1, "skipped": 1}

    def test_empty(self):
        assert summarize([]) == {"total": 0, "passed": 0, "failed": 0, "skipped": 0}


# ── CLI exit-code / nonzero tests ─────────────────────────────────────


class TestCliNonzeroExit:
    """The `healthcheck` subcommand must exit non-zero when any probe
    fails, so `bin/bay deploy` (which calls it post-deploy) surfaces
    user-visible outages loud."""

    def test_healthcheck_nonzero_on_failure(self):
        import typer.testing

        from bay_cli.cli import app

        services = {
            "svc": {"access": "public", "domains": ["example.com"]},
        }

        with patch("bay_cli.commands.healthcheck.paths.find_bay_dir", return_value="/tmp/bay"), \
             patch("bay_cli.commands.healthcheck.paths.consumer_root", return_value="/tmp/consumer"), \
             patch("bay_cli.commands.healthcheck.StackConfig") as sc_cls, \
             patch("bay_cli.commands.healthcheck.run_healthcheck") as run:
            instance = sc_cls.return_value
            instance.get_services.return_value = services
            run.return_value = [
                CheckResult(
                    service="svc", domain="example.com", status=502, ok=False
                )
            ]
            result = typer.testing.CliRunner().invoke(app, ["healthcheck", "testing"])
        assert result.exit_code == 1

    def test_healthcheck_zero_exit_on_all_pass(self):
        import typer.testing

        from bay_cli.cli import app

        services = {
            "svc": {"access": "public", "domains": ["example.com"]},
        }

        with patch("bay_cli.commands.healthcheck.paths.find_bay_dir", return_value="/tmp/bay"), \
             patch("bay_cli.commands.healthcheck.paths.consumer_root", return_value="/tmp/consumer"), \
             patch("bay_cli.commands.healthcheck.StackConfig") as sc_cls, \
             patch("bay_cli.commands.healthcheck.run_healthcheck") as run:
            instance = sc_cls.return_value
            instance.get_services.return_value = services
            run.return_value = [
                CheckResult(
                    service="svc", domain="example.com", status=200, ok=True
                )
            ]
            result = typer.testing.CliRunner().invoke(app, ["healthcheck", "testing"])
        assert result.exit_code == 0

    def test_healthcheck_json_output_shape(self):
        """--json mode must emit the schema specified in S11."""
        import json
        import typer.testing

        from bay_cli.cli import app

        services = {
            "svc": {"access": "public", "domains": ["example.com"]},
        }

        with patch("bay_cli.commands.healthcheck.paths.find_bay_dir", return_value="/tmp/bay"), \
             patch("bay_cli.commands.healthcheck.paths.consumer_root", return_value="/tmp/consumer"), \
             patch("bay_cli.commands.healthcheck.StackConfig") as sc_cls, \
             patch("bay_cli.commands.healthcheck.run_healthcheck") as run:
            instance = sc_cls.return_value
            instance.get_services.return_value = services
            run.return_value = [
                CheckResult(
                    service="svc", domain="example.com", status=200, ok=True
                )
            ]
            result = typer.testing.CliRunner().invoke(
                app, ["--json", "healthcheck", "testing"]
            )
        assert result.exit_code == 0
        # emit_result wraps the payload as {ok, command, data, messages}.
        envelope = json.loads(result.stdout)
        assert envelope["ok"] is True
        assert envelope["command"] == "healthcheck"
        payload = envelope["data"]
        assert set(payload.keys()) >= {"env", "timestamp", "results", "summary"}
        assert payload["summary"]["passed"] == 1
        assert payload["summary"]["failed"] == 0


# ── --dry-run / --check skip test (deploy integration, not CLI) ──────


class TestDryRunSkip:
    """The healthcheck module itself doesn't know about dry-run — the
    deploy command is responsible for not invoking it. This test
    documents the invariant by checking the deploy integration point.

    Kept as a smoke test: run_healthcheck on an empty service dict
    returns [] without making any HTTP requests."""

    def test_dry_run_skip_no_http(self):
        with patch("bay_cli.healthcheck.check_domain") as probe:
            results = run_healthcheck({})
        assert results == []
        probe.assert_not_called()


# ── healthcheck_path wiring (M85-S2) ──────────────────────────────────


class TestHealthcheckPathWiring:
    def test_healthcheck_path_unset_defaults_to_root(self):
        """A service without `healthcheck_path` collects path `/` and
        check_domain is invoked with `/`, producing probed_url `https://example.com/`."""
        services = {"svc": {"access": "public", "domains": ["example.com"]}}
        targets = _collect_targets(services, include_vpn=False)
        assert targets == [("svc", "example.com", "/", False, None)]

        captured: dict[str, str] = {}

        def fake_check(name, domain, path, **_kw):
            captured["path"] = path
            return CheckResult(
                service=name, domain=domain, status=200, ok=True,
                probed_url=f"https://{domain}{path}",
            )

        with patch("bay_cli.healthcheck.check_domain", side_effect=fake_check):
            results = run_healthcheck(services)
        assert captured["path"] == "/"
        assert results[0].probed_url == "https://example.com/"

    def test_healthcheck_path_set_to_dynamic_route(self):
        """A service with `healthcheck_path: /healthcheck` is probed at
        `https://example.com/healthcheck`."""
        services = {
            "svc": {
                "access": "public",
                "domains": ["example.com"],
                "healthcheck_path": "/healthcheck",
            }
        }
        targets = _collect_targets(services, include_vpn=False)
        assert targets == [("svc", "example.com", "/healthcheck", False, None)]

        def fake_check(name, domain, path, **_kw):
            return CheckResult(
                service=name, domain=domain, status=200, ok=True,
                probed_url=f"https://{domain}{path}",
            )

        with patch("bay_cli.healthcheck.check_domain", side_effect=fake_check):
            results = run_healthcheck(services)
        assert results[0].probed_url == "https://example.com/healthcheck"

    def test_healthcheck_path_404_marks_failure(self):
        """A service whose `healthcheck_path` points at a non-existent
        route returns ok=False with the exact probed URL surfaced."""
        services = {
            "svc": {
                "access": "public",
                "domains": ["example.com"],
                "healthcheck_path": "/does-not-exist",
            }
        }

        def fake_check(name, domain, path, **_kw):
            return CheckResult(
                service=name, domain=domain, status=404, ok=False,
                probed_url=f"https://{domain}{path}",
            )

        with patch("bay_cli.healthcheck.check_domain", side_effect=fake_check):
            results = run_healthcheck(services)
        assert results[0].ok is False
        assert results[0].probed_url == "https://example.com/does-not-exist"

    def test_probed_url_in_to_dict_round_trip(self):
        """`probed_url` is serialised by to_dict() so JSON output preserves it."""
        r = CheckResult(
            service="svc", domain="example.com", status=200, ok=True,
            probed_url="https://example.com/healthcheck",
        )
        d = r.to_dict()
        assert d["probed_url"] == "https://example.com/healthcheck"

    def test_probed_url_none_for_skipped_services(self):
        """Skipped services don't probe and their CheckResult has probed_url=None."""
        services = {
            "vpn-svc": {"access": "vpn", "domains": ["v.example.com"]},
        }
        results = run_healthcheck(services)
        assert results[0].skipped is True
        assert results[0].probed_url is None


# ── output rendering for probed_url (M85-S3) ──────────────────────────


class TestOutputProbedUrl:
    """Verify the rich console + JSON output surfaces the probed URL when
    `healthcheck_path` is set, and falls back to the bare domain when not."""

    @staticmethod
    def _run_cli(results: list[CheckResult], json_mode: bool = False):
        import typer.testing
        from bay_cli.cli import app

        services = {"svc": {"access": "public", "domains": ["example.com"]}}
        with patch("bay_cli.commands.healthcheck.paths.find_bay_dir", return_value="/tmp/bay"), \
             patch("bay_cli.commands.healthcheck.paths.consumer_root", return_value="/tmp/consumer"), \
             patch("bay_cli.commands.healthcheck.StackConfig") as sc_cls, \
             patch("bay_cli.commands.healthcheck.run_healthcheck") as run:
            sc_cls.return_value.get_services.return_value = services
            run.return_value = results
            args = ["healthcheck", "testing"]
            if json_mode:
                args = ["--json", *args]
            return typer.testing.CliRunner().invoke(app, args)

    def test_console_output_probed_url_when_path_non_root(self):
        """A non-root path renders the full URL in the console line."""
        result = CheckResult(
            service="svc", domain="example.com", status=200, ok=True,
            probed_url="https://example.com/healthcheck",
        )
        cli_result = self._run_cli([result])
        assert cli_result.exit_code == 0
        assert "https://example.com/healthcheck" in cli_result.stdout

    def test_console_output_domain_only_when_path_root(self):
        """A root-path probe renders the bare domain (no scheme/path) — no
        regression vs. pre-M85 layout."""
        result = CheckResult(
            service="svc", domain="example.com", status=200, ok=True,
            probed_url="https://example.com/",
        )
        cli_result = self._run_cli([result])
        assert cli_result.exit_code == 0
        assert "https://example.com/" not in cli_result.stdout
        assert "example.com" in cli_result.stdout

    def test_console_output_skipped_no_url(self):
        """Skipped-service line shows the bare domain and the skip reason —
        no probed URL since no probe was made."""
        result = CheckResult(
            service="vpn-svc", domain="v.example.com", status=None, ok=True,
            skipped=True, skip_reason="VPN-only", probed_url=None,
        )
        cli_result = self._run_cli([result])
        assert cli_result.exit_code == 0
        assert "v.example.com" in cli_result.stdout
        assert "VPN-only" in cli_result.stdout
        assert "https://" not in cli_result.stdout

    def test_console_failure_block_shows_probed_url(self):
        """The post-summary failure block surfaces the probed URL so an
        operator triaging the alert sees exactly which path was checked."""
        result = CheckResult(
            service="svc", domain="example.com", status=502, ok=False,
            probed_url="https://example.com/healthcheck",
        )
        cli_result = self._run_cli([result])
        assert cli_result.exit_code == 1
        # Failure summary block at the bottom contains the URL, not just domain
        # (matched at least twice — once in the per-line list, once in the
        # failure block).
        assert cli_result.stdout.count("https://example.com/healthcheck") >= 2

    def test_json_output_includes_probed_url(self):
        """JSON output emits `probed_url` as a top-level field on each result."""
        import json
        result = CheckResult(
            service="svc", domain="example.com", status=200, ok=True,
            probed_url="https://example.com/healthcheck",
        )
        cli_result = self._run_cli([result], json_mode=True)
        assert cli_result.exit_code == 0
        envelope = json.loads(cli_result.stdout)
        results = envelope["data"]["results"]
        assert results[0]["probed_url"] == "https://example.com/healthcheck"

    def test_json_probed_url_root_present(self):
        """`probed_url` is always present in JSON output, even for root probes."""
        import json
        result = CheckResult(
            service="svc", domain="example.com", status=200, ok=True,
            probed_url="https://example.com/",
        )
        cli_result = self._run_cli([result], json_mode=True)
        envelope = json.loads(cli_result.stdout)
        results = envelope["data"]["results"]
        assert "probed_url" in results[0]
        assert results[0]["probed_url"] == "https://example.com/"

    def test_probed_url_type_is_string_or_none(self):
        """The serialised `probed_url` is `str` for probed services and
        `None` for skipped services — never any other type."""
        probed = CheckResult(
            service="a", domain="a.com", status=200, ok=True,
            probed_url="https://a.com/",
        )
        skipped = CheckResult(
            service="b", domain="b.com", status=None, ok=True,
            skipped=True, skip_reason="VPN-only",
        )
        assert isinstance(probed.to_dict()["probed_url"], str)
        assert skipped.to_dict()["probed_url"] is None


# ── domain_base expansion ─────────────────────────────────────────────
#
# services.yml documents `{{ domain_base }}` as the multi-region idiom, but
# healthcheck parses services.yml as plain YAML and renders no Jinja. The
# template used to reach check_domain verbatim, so `bin/bay healthcheck
# production --include-vpn` probed `https://status.{{ domain_base }}/` and
# reported a false RED (exit 1) for a hostname that never existed.

# Region -> its group_vars scalars. `domain_base_next` is present on purpose:
# a consumer mid-domain-migration defines a second per-region domain variable,
# and those endpoints must be probed, not skipped.
_RVARS = {
    "eu": {
        "domain_base": "eu.argo.example.com",  # legacy-argo: DNS zone example, non-goal per rename-map
        "domain_base_next": "eu.bay.example.com",
    },
    "na": {
        "domain_base": "na.argo.example.com",  # legacy-argo: DNS zone example, non-goal per rename-map
        "domain_base_next": "na.bay.example.com",
    },
    "infra": {
        "domain_base": "infra.argo.example.com",  # legacy-argo: DNS zone example, non-goal per rename-map
        "domain_base_next": "infra.bay.example.com",
    },
}


class TestExpandDomain:
    def test_untemplated_domain_unchanged(self) -> None:
        assert expand_domain("campfire.example.com", None, _RVARS) == [
            "campfire.example.com"
        ]

    def test_untemplated_domain_not_duplicated_per_region(self) -> None:
        """A plain domain must not fan out just because the service spans regions."""
        assert expand_domain("campfire.example.com", ["eu", "na"], _RVARS) == [
            "campfire.example.com"
        ]

    def test_expands_for_the_services_single_region(self) -> None:
        """The real gatus case: regions: [infra] -> infra's base, not eu's."""
        assert expand_domain("status.{{ domain_base }}", ["infra"], _RVARS) == [
            "status.infra.argo.example.com"  # legacy-argo: DNS zone example, non-goal per rename-map
        ]

    def test_any_per_region_variable_resolves_not_just_domain_base(self) -> None:
        """The bug: only domain_base was expanded. A consumer mid-domain-move
        defined domain_base_next, so `status.{{ domain_base_next }}` — a real,
        live, public endpoint — was reported "unresolved template" on every
        deploy instead of being probed. A skipped check reads like a passing
        one at a glance."""
        assert expand_domain("status.{{ domain_base_next }}", ["infra"], _RVARS) == [
            "status.infra.bay.example.com"
        ]

    def test_multiple_variables_in_one_domain(self) -> None:
        assert expand_domain(
            "{{ domain_base_next }}.via.{{ domain_base }}", ["eu"], _RVARS
        ) == ["eu.bay.example.com.via.eu.argo.example.com"]  # legacy-argo: DNS zone example, non-goal per rename-map

    def test_region_missing_one_referenced_variable_is_skipped_not_half_rendered(
        self,
    ) -> None:
        """Half-substituting would probe a hostname that never existed."""
        rvars = {
            "eu": {"domain_base": "eu.example.com", "domain_base_next": "eu.bay.example.com"},
            "na": {"domain_base": "na.example.com"},
        }
        assert expand_domain("s.{{ domain_base_next }}", None, rvars) == [
            "s.eu.bay.example.com"
        ]

    def test_unknown_variable_still_falls_through_to_the_backstop(self) -> None:
        out = expand_domain("s.{{ nope }}", ["eu"], _RVARS)
        assert out == ["s.{{ nope }}"]

    def test_expands_across_regions_when_service_spans_them(self) -> None:
        assert expand_domain("api.{{ domain_base }}", ["eu", "na"], _RVARS) == [
            "api.eu.argo.example.com",  # legacy-argo: DNS zone example, non-goal per rename-map
            "api.na.argo.example.com",  # legacy-argo: DNS zone example, non-goal per rename-map
        ]

    def test_no_regions_key_expands_everywhere(self) -> None:
        """No `regions:` means deploy everywhere (docs/services.md)."""
        assert expand_domain("api.{{ domain_base }}", None, _RVARS) == [
            "api.eu.argo.example.com",  # legacy-argo: DNS zone example, non-goal per rename-map
            "api.na.argo.example.com",  # legacy-argo: DNS zone example, non-goal per rename-map
            "api.infra.argo.example.com",  # legacy-argo: DNS zone example, non-goal per rename-map
        ]

    def test_whitespace_variants(self) -> None:
        for tpl in ("x.{{domain_base}}", "x.{{  domain_base  }}"):
            assert expand_domain(tpl, ["eu"], _RVARS) == ["x.eu.argo.example.com"]  # legacy-argo: DNS zone example, non-goal per rename-map

    def test_region_without_a_base_yields_no_expansion(self) -> None:
        """Left templated on purpose — the backstop skips it."""
        assert expand_domain("x.{{ domain_base }}", ["nowhere"], _RVARS) == [
            "x.{{ domain_base }}"
        ]

    def test_single_region_consumer_maps_under_env(self) -> None:
        assert expand_domain("x.{{ domain_base }}", None, {"production": {"domain_base": "argo.example.de"}}) == [  # legacy-argo: DNS zone example, non-goal per rename-map
            "x.argo.example.de"  # legacy-argo: DNS zone example, non-goal per rename-map
        ]


class TestCollectTargetsDomainBase:
    def test_gatus_resolves_instead_of_being_probed_raw(self) -> None:
        services = {
            "gatus": {
                "access": "vpn",
                "regions": ["infra"],
                "domains": ["status.{{ domain_base }}"],
            }
        }
        targets = _collect_targets(services, include_vpn=True, region_vars=_RVARS)
        assert [t[1] for t in targets] == ["status.infra.argo.example.com"]  # legacy-argo: DNS zone example, non-goal per rename-map
        # include_vpn=True -> actually probed, so it MUST be resolved.
        assert targets[0][3] is False

    def test_public_templated_domain_is_never_probed_raw(self) -> None:
        """The false-RED case: a public service whose template can't resolve."""
        services = {"svc": {"domains": ["x.{{ some_other_var }}"]}}
        targets = _collect_targets(services, include_vpn=False, region_vars=_RVARS)
        assert len(targets) == 1
        assert targets[0][3] is True, "unresolved template must be skipped, not probed"
        assert targets[0][4] == "unresolved template in domain"

    def test_no_domain_bases_skips_rather_than_probes(self) -> None:
        services = {"svc": {"domains": ["x.{{ domain_base }}"]}}
        targets = _collect_targets(services, include_vpn=False, region_vars=None)
        assert targets[0][3] is True
        assert targets[0][4] == "unresolved template in domain"

    def test_untemplated_services_unaffected(self) -> None:
        services = {"svc": {"domains": ["a.example.com", "b.example.com"]}}
        targets = _collect_targets(services, include_vpn=False, region_vars=_RVARS)
        assert [t[1] for t in targets] == ["a.example.com", "b.example.com"]
        assert all(t[3] is False for t in targets)


# ── readiness window / cold-start retry ───────────────────────────────
#
# Incident 2026-07-27: a Node service (`pilotco`) was recreated during
# a deploy and the post-deploy healthcheck reported
#     ✗ https://beta.pilotco.example.com/health  502  [FAIL]
# The service was fine — a minute later it served 200 on 6/6 probes with
# RestartCount=0, and the next region's deploy passed the identical URL. The
# old window was 3 attempts × 5s ≈ 10s, far short of the container's cold
# start. A healthcheck that cries wolf trains the operator to ignore it, so
# the window is now bounded by elapsed wall-clock and sized by *how* the probe
# failed.
#
# These tests drive a virtual clock: `sleep` advances it instead of blocking,
# so a 90s window is exercised in microseconds and the suite never sleeps.


class _FakeClock:
    """Stand-in for the `time` module inside bay_cli.healthcheck.

    `sleep` advances a virtual clock that `monotonic` reads back, so the
    budget arithmetic under test is real while the test is instant."""

    def __init__(self, *, sleep_advances: bool = True) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self._sleep_advances = sleep_advances

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self._sleep_advances:
            self.now += seconds

    def monotonic(self) -> float:
        return self.now


class _ScriptedProbe:
    """Replaces `_check_once` with a scripted list of `(status, error)`
    outcomes. The final outcome repeats forever, so `[(502, None)]` models a
    service that never comes up. Each attempt costs `cost_s` of virtual
    wall-clock, because a real request is not free either."""

    def __init__(self, clock: _FakeClock, outcomes, cost_s: float = 0.05) -> None:
        self.clock = clock
        self.outcomes = list(outcomes)
        self.cost_s = cost_s
        self.calls = 0

    def __call__(self, url, *, session):
        self.calls += 1
        status, error = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        self.clock.now += self.cost_s
        return status, [], error


def _probe(outcomes, *, budget=None, cost_s=0.05, sleep_advances=True):
    """Run `check_domain` against scripted outcomes. Returns (result, clock, probe)."""
    clock = _FakeClock(sleep_advances=sleep_advances)
    scripted = _ScriptedProbe(clock, outcomes, cost_s=cost_s)
    kwargs = {} if budget is None else {"startup_budget_s": budget}
    with patch("bay_cli.healthcheck.time", clock), \
         patch("bay_cli.healthcheck._check_once", scripted):
        result = check_domain("svc", "example.com", "/health", **kwargs)
    return result, clock, scripted


_REFUSED = (None, "connection refused: HTTPSConnectionPool(host='x') ... Connection refused")
_TIMEOUT = (None, "timeout")
_DNS = (None, "DNS failure: ... [Errno -2] Name or service not known")
_TLS = (None, "TLS error: certificate verify failed")


class TestColdStartRetry:
    def test_healthy_service_costs_nothing_extra(self):
        """The whole point of the wide window is that a green deploy never
        pays for it: 2xx on attempt 1, no sleep, no elapsed time."""
        result, clock, probe = _probe([(200, None)])
        assert result.ok is True
        assert result.attempts == 1
        assert probe.calls == 1
        assert clock.slept == []

    def test_redirect_also_passes_on_first_attempt(self):
        result, clock, _ = _probe([(301, None)])
        assert result.ok is True
        assert clock.slept == []

    def test_slow_starter_502_then_200_passes(self):
        """THE regression: the pilotco shape. Traefik returns 502
        while the recreated container is still booting, then the app serves
        200. Must PASS — under the old 3-attempt/5s window this was a FAIL."""
        result, clock, probe = _probe([(502, None)] * 6 + [(200, None)])
        assert result.ok is True, "a container that comes up in ~45s is not a failure"
        assert result.status == 200
        assert result.attempts == 7
        assert probe.calls == 7
        # Well inside the budget, and it needed far more than the old ~10s.
        assert 10.0 < clock.now < STARTUP_BUDGET_S

    def test_slow_starter_connection_refused_then_200_passes(self):
        """Same story one layer lower: nothing is listening on the port yet,
        so the connection is refused outright rather than proxied to a 502."""
        result, clock, _ = _probe([_REFUSED] * 6 + [(200, None)])
        assert result.ok is True
        assert result.attempts == 7
        assert 10.0 < clock.now < STARTUP_BUDGET_S

    def test_slow_starter_timeout_then_200_passes(self):
        """A read timeout during boot (app accepted the socket but hasn't
        started serving) is also a starting signal, not a failure."""
        result, _clock, _ = _probe([_TIMEOUT] * 4 + [(200, None)], cost_s=8.0)
        assert result.ok is True
        assert result.attempts == 5

    def test_never_ready_still_fails_and_is_bounded(self):
        """The other half of the tradeoff: a genuinely dead service must
        still go RED, inside the advertised window."""
        result, clock, probe = _probe([(502, None)])
        assert result.ok is False
        assert result.status == 502
        # Bounded by wall-clock, not by attempt count.
        assert clock.now < STARTUP_BUDGET_S
        assert result.attempts == probe.calls
        assert result.attempts < _attempt_cap(STARTUP_BUDGET_S)

    def test_never_ready_connection_refused_bounded(self):
        result, clock, _ = _probe([_REFUSED])
        assert result.ok is False
        assert clock.now < STARTUP_BUDGET_S

    def test_definitive_404_fails_fast(self):
        """A 404 means Traefik routed and the app answered — the app is up
        and the path is wrong. Keep the short legacy window (a recreate does
        briefly drop the router) but do not spend the cold-start budget."""
        result, clock, _ = _probe([(404, None)])
        assert result.ok is False
        assert result.status == 404
        assert clock.now < DEFINITIVE_BUDGET_S
        assert result.attempts == 3, "roughly the legacy 3 attempts, unchanged"

    def test_definitive_500_is_not_a_starting_signal(self):
        """500 is the app erroring, not the proxy failing to reach it."""
        result, clock, _ = _probe([(500, None)])
        assert result.ok is False
        assert clock.now < DEFINITIVE_BUDGET_S

    def test_tls_error_uses_short_window(self):
        result, clock, _ = _probe([_TLS])
        assert result.ok is False
        assert clock.now < DEFINITIVE_BUDGET_S

    def test_dns_failure_is_fatal_and_never_retried(self):
        """No amount of waiting invents a DNS record. Retrying a typo'd
        domain 11 times just burns 90s per bad domain."""
        result, clock, probe = _probe([_DNS])
        assert result.ok is False
        assert result.attempts == 1
        assert probe.calls == 1
        assert clock.slept == []

    def test_transient_404_recovers_within_short_window(self):
        """Traefik drops the router for a beat during a recreate. One 404
        followed by a 200 must still pass."""
        result, _clock, _ = _probe([(404, None), (200, None)])
        assert result.ok is True
        assert result.attempts == 2

    def test_budget_reclassifies_when_the_app_starts_answering(self):
        """Starts as 'still booting' (refused), then the app answers 404.
        Once there is a definitive answer the short window applies
        retroactively — no reason to keep waiting out the cold-start budget."""
        result, clock, _ = _probe([_REFUSED, _REFUSED, (404, None)])
        assert result.ok is False
        assert result.status == 404
        assert result.attempts == 3
        assert clock.now < STARTUP_BUDGET_S / 2

    def test_max_attempts_terminates_when_the_clock_never_moves(self):
        """Belt-and-braces: if every attempt returned instantly and sleep
        cost nothing, the elapsed-time bound could never trip. The loop must
        still terminate."""
        result, _clock, probe = _probe(
            [(502, None)], cost_s=0.0, sleep_advances=False
        )
        assert result.ok is False
        assert result.attempts == _attempt_cap(STARTUP_BUDGET_S)
        assert probe.calls == result.attempts

    def test_attempt_cap_never_truncates_a_widened_budget(self):
        """The cap is a termination guard, not a second bound. A service
        that declares health_check_timeout: 240 must spend its 240s, not stop
        early because a flat attempt ceiling ran out first."""
        result, clock, _ = _probe([(502, None)], budget=240.0)
        assert result.ok is False
        assert result.attempts < _attempt_cap(240.0), "time budget must bind first"
        assert 230.0 < clock.now < 240.0

    def test_backoff_is_capped_and_monotonic(self):
        _result, clock, _ = _probe([(502, None)])
        assert clock.slept[0] == 2.0
        assert clock.slept[:3] == [2.0, 4.0, 8.0]
        assert max(clock.slept) == 10.0
        assert clock.slept == sorted(clock.slept)


class TestClassifyFailure:
    def test_gateway_statuses_are_starting(self):
        for status in (502, 503, 504):
            assert classify_failure(status, None) == "starting"

    def test_app_statuses_are_definitive(self):
        for status in (400, 401, 403, 404, 429, 500):
            assert classify_failure(status, None) == "definitive"

    def test_connection_errors_are_starting(self):
        assert classify_failure(None, _REFUSED[1]) == "starting"
        assert classify_failure(None, "timeout") == "starting"

    def test_dns_is_fatal(self):
        assert classify_failure(None, _DNS[1]) == "fatal"

    def test_tls_and_unknown_are_definitive(self):
        assert classify_failure(None, _TLS[1]) == "definitive"
        assert classify_failure(None, "request error: something new") == "definitive"
        assert classify_failure(None, None) == "definitive"


class TestPerServiceStartupBudget:
    """`health_check_timeout` already means 'how long may this service take to
    become healthy after a restart' for rebuild.sh (docs/build-pipeline.md).
    The URL probe asks the same question, so it reuses the same key rather
    than adding a near-homonym operators would mistype."""

    def test_default_when_unset(self):
        assert startup_budget_for({}) == STARTUP_BUDGET_S

    def test_larger_value_widens_the_window(self):
        assert startup_budget_for({"health_check_timeout": 180}) == 180.0

    def test_smaller_value_is_ignored(self):
        """Override may only widen. A consumer that tuned rebuild.sh's
        rollback poll down must not make the probe stricter than baseline —
        that would reintroduce the false positive."""
        assert startup_budget_for({"health_check_timeout": 30}) == STARTUP_BUDGET_S

    def test_bool_is_not_a_number(self):
        assert startup_budget_for({"health_check_timeout": True}) == STARTUP_BUDGET_S

    def test_garbage_falls_back_to_default(self):
        assert startup_budget_for({"health_check_timeout": "180"}) == STARTUP_BUDGET_S

    def test_run_healthcheck_passes_override_to_probe(self):
        services = {
            "slow": {
                "access": "public",
                "domains": ["slow.example.com"],
                "health_check_timeout": 240,
            },
            "normal": {"access": "public", "domains": ["normal.example.com"]},
        }
        seen: dict[str, float] = {}

        def fake_check(name, domain, path, *, startup_budget_s):
            seen[name] = startup_budget_s
            return CheckResult(service=name, domain=domain, status=200, ok=True)

        with patch("bay_cli.healthcheck.check_domain", side_effect=fake_check):
            run_healthcheck(services)
        assert seen == {"slow": 240.0, "normal": STARTUP_BUDGET_S}

    def test_override_actually_extends_the_probe(self):
        """End-to-end: a service that needs ~105s would be RED on the default
        90s budget but passes when it declares health_check_timeout: 180."""
        outcomes = [(502, None)] * 12 + [(200, None)]
        default_run, _clock, _ = _probe(outcomes)
        assert default_run.ok is False, "sanity: 12 failures exceed the 90s default"

        widened, clock, _ = _probe(outcomes, budget=180.0)
        assert widened.ok is True
        assert widened.attempts == 13
        assert STARTUP_BUDGET_S < clock.now < 180.0


class TestReadinessNoteRendering:
    """A 90s window is wide enough to absorb a cold start silently. The
    console prints what it absorbed so a service creeping toward the ceiling
    stays visible instead of reading as instantly-green."""

    def test_note_omitted_for_first_attempt_pass(self):
        r = CheckResult(
            service="svc", domain="x.com", status=200, ok=True,
            attempts=1, elapsed_ms=120,
        )
        assert readiness_note(r) == ""

    def test_note_reports_attempts_and_seconds(self):
        r = CheckResult(
            service="svc", domain="x.com", status=200, ok=True,
            attempts=7, elapsed_ms=44_350,
        )
        note = readiness_note(r)
        assert "44s" in note
        assert "7 attempts" in note

    def test_slow_pass_surfaced_in_cli_output(self):
        import typer.testing
        from bay_cli.cli import app

        services = {"svc": {"access": "public", "domains": ["example.com"]}}
        slow = CheckResult(
            service="svc", domain="example.com", status=200, ok=True,
            attempts=7, elapsed_ms=44_350, probed_url="https://example.com/",
        )
        with patch("bay_cli.commands.healthcheck.paths.find_bay_dir", return_value="/tmp/bay"), \
             patch("bay_cli.commands.healthcheck.paths.consumer_root", return_value="/tmp/consumer"), \
             patch("bay_cli.commands.healthcheck.StackConfig") as sc_cls, \
             patch("bay_cli.commands.healthcheck.run_healthcheck") as run:
            sc_cls.return_value.get_services.return_value = services
            run.return_value = [slow]
            cli_result = typer.testing.CliRunner().invoke(app, ["healthcheck", "testing"])
        assert cli_result.exit_code == 0
        assert "7 attempts" in cli_result.stdout
