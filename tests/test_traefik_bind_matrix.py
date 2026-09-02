"""Traefik static config: entrypoint bind matrix, metrics bind, TLS options.

Three findings converge on `traefik.yml.j2`:

  - S11: with `traefik_split_entrypoints: true` and a blank
    `traefik_public_bind_ip` (it defaults to `netplan_address`, often
    unset) the template renders `:80`/`:443` wildcard binds — the
    opposite of the requested split — and `websecure_tailnet` then
    collides with `websecure` on 443. The template still renders that
    way; the gate is an Ansible `fail` in
    `roles/deploy_stack/tasks/validate.yml`, asserted here by reading the
    task file, because the blank value is a host fact.
  - S15: metrics bound on all interfaces; now `traefik_metrics_bind_ip`
    (127.0.0.1 by default).
  - S15: no `tls.options` block at all; now `tls.options.default` with
    `minVersion` always on and `sniStrict` opt-in. `tls.options` is
    DYNAMIC configuration — Traefik parses and then ignores a `tls:` key
    in the static file — so it is rendered to
    `dynamic/tls-options.yml` and served by the file provider, which is
    therefore always enabled.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from helpers import make_ansible_env

ROOT = Path(__file__).parent.parent
TEMPLATE_DIR = ROOT / "roles" / "traefik" / "templates"
DEPLOY_VALIDATE = ROOT / "roles" / "deploy_stack" / "tasks" / "validate.yml"


def _base_context(**overrides) -> dict:
    base = {
        "ansible_managed": "test",
        "traefik_docker_network": "services",
        "letsencrypt_email": "ops@example.com",
        "traefik_metrics_enabled": True,
        "traefik_metrics_port": 8082,
        "traefik_metrics_bind_ip": "127.0.0.1",
        "traefik_access_log_fields_headers": {},
        "traefik_log_level": "ERROR",
        "traefik_split_entrypoints": False,
        "traefik_public_bind_ip": "",
        "gateway_bind_ip": "",
        "traefik_tls_min_version": "VersionTLS12",
        "traefik_tls_sni_strict": False,
        "traefik_dns_challenge_enabled": False,
        "traefik_dns_resolver_name": "letsencrypt_dns",
        "traefik_dns_provider": "cloudflare",
        "traefik_acme_dns_email": "ops@example.com",
    }
    base.update(overrides)
    return base


def _render(**overrides) -> dict:
    env = make_ansible_env(TEMPLATE_DIR)
    rendered = env.get_template("traefik.yml.j2").render(**_base_context(**overrides))
    return yaml.safe_load(rendered)


def _render_tls_options(**overrides) -> dict:
    env = make_ansible_env(TEMPLATE_DIR)
    rendered = env.get_template("dynamic/tls-options.yml.j2").render(
        **_base_context(**overrides)
    )
    return yaml.safe_load(rendered)


# ── entrypoint bind matrix ─────────────────────────────────────────────


class TestSplitOff:
    def test_wildcard_binds_are_correct_when_split_is_off(self):
        eps = _render(traefik_split_entrypoints=False)["entryPoints"]
        assert eps["web"]["address"] == ":80"
        assert eps["websecure"]["address"] == ":443"

    def test_no_tailnet_entrypoint_when_split_is_off(self):
        eps = _render(
            traefik_split_entrypoints=False, gateway_bind_ip="100.64.0.5"
        )["entryPoints"]
        assert "websecure_tailnet" not in eps


class TestSplitOnWithIp:
    def test_public_entrypoints_bind_the_public_ip(self):
        eps = _render(
            traefik_split_entrypoints=True,
            traefik_public_bind_ip="203.0.113.10",
            gateway_bind_ip="100.64.0.5",
        )["entryPoints"]
        assert eps["web"]["address"] == "203.0.113.10:80"
        assert eps["websecure"]["address"] == "203.0.113.10:443"

    def test_tailnet_entrypoint_does_not_collide(self):
        eps = _render(
            traefik_split_entrypoints=True,
            traefik_public_bind_ip="203.0.113.10",
            gateway_bind_ip="100.64.0.5",
        )["entryPoints"]
        assert eps["websecure_tailnet"]["address"] == "100.64.0.5:443"
        assert eps["websecure"]["address"] != eps["websecure_tailnet"]["address"]


class TestSplitOnWithBlankIp:
    """The combination the deploy must refuse."""

    def test_deploy_validate_fails_the_combination(self):
        """The gate is Ansible-side — `netplan_address` is a host fact."""
        text = DEPLOY_VALIDATE.read_text()
        assert "traefik_split_entrypoints" in text
        # The fail task must key off both the split flag and a blank bind IP.
        idx = text.index("- name: Validate split entrypoints")
        task = text[idx : idx + 1400]
        assert "ansible.builtin.fail" in task
        assert "traefik_split_entrypoints | default(false)" in task
        assert "not (traefik_public_bind_ip | default('', true) | trim)" in task

    def test_unguarded_render_would_wildcard_and_collide(self):
        """Documents exactly what the Ansible gate prevents."""
        eps = _render(
            traefik_split_entrypoints=True,
            traefik_public_bind_ip="",
            gateway_bind_ip="100.64.0.5",
        )["entryPoints"]
        assert eps["websecure"]["address"] == ":443"
        assert eps["websecure_tailnet"]["address"] == "100.64.0.5:443"


# ── metrics bind ───────────────────────────────────────────────────────


class TestMetricsBind:
    def test_metrics_bind_loopback_by_default(self):
        eps = _render()["entryPoints"]
        assert eps["metrics"]["address"] == "127.0.0.1:8082"

    def test_metrics_bind_ip_is_overridable(self):
        eps = _render(traefik_metrics_bind_ip="100.64.0.5")["entryPoints"]
        assert eps["metrics"]["address"] == "100.64.0.5:8082"

    def test_metrics_absent_when_disabled(self):
        doc = _render(traefik_metrics_enabled=False)
        assert "metrics" not in doc["entryPoints"]
        assert "metrics" not in doc


# ── tls.options.default (DYNAMIC config) ───────────────────────────────


class TestTlsOptions:
    def test_min_version_is_tls12_by_default(self):
        opts = _render_tls_options()["tls"]["options"]["default"]
        assert opts["minVersion"] == "VersionTLS12"

    def test_min_version_is_overridable(self):
        opts = _render_tls_options(traefik_tls_min_version="VersionTLS13")["tls"][
            "options"
        ]["default"]
        assert opts["minVersion"] == "VersionTLS13"

    def test_sni_strict_off_by_default(self):
        """sniStrict refuses SNI-less requests, breaking IP-based probes."""
        opts = _render_tls_options()["tls"]["options"]["default"]
        assert opts["sniStrict"] is False

    def test_sni_strict_opt_in(self):
        opts = _render_tls_options(traefik_tls_sni_strict=True)["tls"]["options"][
            "default"
        ]
        assert opts["sniStrict"] is True

    def test_static_config_has_no_tls_key(self):
        """The regression this replaces.

        Traefik reads `tls:` out of the static file and ignores it, so a
        TLS floor declared there is a silent no-op. If this assertion
        ever fails again, the floor is not being enforced.
        """
        for kwargs in (
            {"traefik_split_entrypoints": False},
            {"traefik_split_entrypoints": True, "traefik_public_bind_ip": "203.0.113.10"},
            {"traefik_dns_challenge_enabled": True},
        ):
            assert "tls" not in _render(**kwargs)

    def test_file_provider_is_always_enabled(self):
        """tls-options.yml is only read if the file provider is on."""
        for dns in (False, True):
            providers = _render(traefik_dns_challenge_enabled=dns)["providers"]
            assert providers["file"]["directory"] == "/etc/traefik/dynamic"
            assert providers["file"]["watch"] is True
