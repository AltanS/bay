"""Unit tests for bay_spec_hash filter (hash-based infra recreation).

These tests run without Ansible — pure Python, no infra required.
The filter is the source of truth for what constitutes a "config change"
that should trigger container recreation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Use the global filter_plugins (not the role-local copy)
sys.path.insert(0, str(Path(__file__).parent.parent / "filter_plugins"))

from bay_filters import bay_spec_hash  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────

_WEBHOOK_SPEC = {
    "name": "bay-webhook",
    "type": "infra",
    "image": "bay-webhook:latest",
    "build": True,
    "restart_policy": "unless-stopped",
    "networks": ["services"],
    "env": {
        "WEBHOOK_SECRET": "super-secret",
        "TRIGGER_DIR": "/triggers",
        "HOSTNAME": "server.example.com",
    },
    "volumes": [
        "/opt/myapp/triggers:/triggers",
        "/opt/myapp/webhook/config.json:/config/services.json:ro",
    ],
    "labels": {
        "traefik.enable": "true",
        "traefik.http.routers.bay-webhook.rule": "Host(`deploy.example.com`)",
        "traefik.http.services.bay-webhook.loadbalancer.server.port": "9000",
        "com.centurylinklabs.watchtower.enable": "false",
    },
}

_TRAEFIK_SPEC = {
    "name": "traefik",
    "type": "infra",
    "image": "traefik:v3.6",
    "restart_policy": "unless-stopped",
    "network_mode": "host",
    "volumes": [
        "/var/run/docker.sock:/var/run/docker.sock:ro",
        "/opt/myapp/traefik.yml:/etc/traefik/traefik.yml:ro",
    ],
    "labels": {
        "traefik.enable": "true",
        "com.centurylinklabs.watchtower.enable": "false",
    },
}


# ── Basic contract ────────────────────────────────────────────────────────


class TestSpecHashBasicContract:
    def test_returns_64_char_hex_string(self) -> None:
        h = bay_spec_hash(_WEBHOOK_SPEC)
        assert isinstance(h, str)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic_same_input(self) -> None:
        h1 = bay_spec_hash(_WEBHOOK_SPEC)
        h2 = bay_spec_hash(_WEBHOOK_SPEC)
        assert h1 == h2

    def test_different_containers_different_hashes(self) -> None:
        assert bay_spec_hash(_WEBHOOK_SPEC) != bay_spec_hash(_TRAEFIK_SPEC)


# ── Meta-field exclusions (should NOT change the hash) ─────────────────────


class TestSpecHashExcludedFields:
    def test_type_field_excluded(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "type": "infra"}
        spec_b = {**_WEBHOOK_SPEC, "type": "service"}
        assert bay_spec_hash(spec_a) == bay_spec_hash(spec_b)

    def test_build_field_excluded(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "build": True}
        spec_b = dict(_WEBHOOK_SPEC)
        spec_b.pop("build", None)
        assert bay_spec_hash(spec_a) == bay_spec_hash(spec_b)

    def test_zero_downtime_excluded(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "zero_downtime": True}
        spec_b = {**_WEBHOOK_SPEC, "zero_downtime": False}
        assert bay_spec_hash(spec_a) == bay_spec_hash(spec_b)

    def test_health_check_timeout_excluded(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "health_check_timeout": 120}
        spec_b = {**_WEBHOOK_SPEC, "health_check_timeout": 60}
        assert bay_spec_hash(spec_a) == bay_spec_hash(spec_b)

    def test_env_file_excluded(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "env_file": "/opt/myapp/env/webhook.env"}
        spec_b = dict(_WEBHOOK_SPEC)
        spec_b.pop("env_file", None)
        assert bay_spec_hash(spec_a) == bay_spec_hash(spec_b)

    def test_config_hash_label_excluded(self) -> None:
        """The hash label itself must not affect the hash (no circular dependency)."""
        spec_base = dict(_WEBHOOK_SPEC)
        h_base = bay_spec_hash(spec_base)

        spec_with_label = dict(_WEBHOOK_SPEC)
        spec_with_label["labels"] = dict(_WEBHOOK_SPEC["labels"])
        spec_with_label["labels"]["com.argo.config-hash"] = h_base  # legacy-argo: exercises dual-read hash-label exclusion

        assert bay_spec_hash(spec_with_label) == h_base


# ── Fields that MUST change the hash ──────────────────────────────────────


class TestSpecHashSensitiveFields:
    def test_image_change_detected(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "image": "bay-webhook:latest"}
        spec_b = {**_WEBHOOK_SPEC, "image": "bay-webhook:v2"}
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_new_volume_detected(self) -> None:
        spec_a = dict(_WEBHOOK_SPEC)
        spec_b = dict(_WEBHOOK_SPEC)
        spec_b["volumes"] = spec_a["volumes"] + ["/opt/myapp/state:/state"]
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_removed_volume_detected(self) -> None:
        spec_a = dict(_WEBHOOK_SPEC)
        spec_b = dict(_WEBHOOK_SPEC)
        spec_b["volumes"] = spec_a["volumes"][:-1]  # drop last volume
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_env_change_detected(self) -> None:
        spec_a = dict(_WEBHOOK_SPEC)
        spec_b = dict(_WEBHOOK_SPEC)
        spec_b["env"] = {**spec_a["env"], "NEW_VAR": "new_value"}
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_label_change_detected(self) -> None:
        spec_a = dict(_WEBHOOK_SPEC)
        spec_b = dict(_WEBHOOK_SPEC)
        spec_b["labels"] = {**spec_a["labels"], "custom.label": "value"}
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_network_change_detected(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "networks": ["services"]}
        spec_b = {**_WEBHOOK_SPEC, "networks": ["services", "internal"]}
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_network_mode_change_detected(self) -> None:
        spec_a = dict(_TRAEFIK_SPEC)
        spec_b = {**_TRAEFIK_SPEC, "network_mode": "bridge"}
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_port_change_detected(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "ports": ["9000:9000"]}
        spec_b = {**_WEBHOOK_SPEC, "ports": ["9001:9000"]}
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_command_change_detected(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "command": "serve"}
        spec_b = {**_WEBHOOK_SPEC, "command": "serve --verbose"}
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_restart_policy_change_detected(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "restart_policy": "unless-stopped"}
        spec_b = {**_WEBHOOK_SPEC, "restart_policy": "always"}
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_mem_limit_change_detected(self) -> None:
        spec_a = {**_WEBHOOK_SPEC, "mem_limit": "256m"}
        spec_b = {**_WEBHOOK_SPEC, "mem_limit": "512m"}
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)


# ── Stability / ordering invariance ───────────────────────────────────────


class TestSpecHashStability:
    def test_dict_key_order_invariant(self) -> None:
        """Hash must be the same regardless of key insertion order."""
        spec_a = {
            "name": "bay-webhook",
            "image": "bay-webhook:latest",
            "networks": ["services"],
        }
        spec_b = {
            "networks": ["services"],
            "name": "bay-webhook",
            "image": "bay-webhook:latest",
        }
        assert bay_spec_hash(spec_a) == bay_spec_hash(spec_b)

    def test_list_order_matters(self) -> None:
        """Volume list order SHOULD matter — different order = different container."""
        spec_a = {**_WEBHOOK_SPEC, "volumes": ["/a:/a", "/b:/b"]}
        spec_b = {**_WEBHOOK_SPEC, "volumes": ["/b:/b", "/a:/a"]}
        # Lists are ordered — order change should produce different hash
        assert bay_spec_hash(spec_a) != bay_spec_hash(spec_b)

    def test_no_labels_field_handled(self) -> None:
        """Specs without a labels key must not raise."""
        spec = {
            "name": "watchtower",
            "image": "nickfedor/watchtower:latest",
            "networks": ["services"],
        }
        h = bay_spec_hash(spec)
        assert len(h) == 64


# ── Env/secrets digest (service/accessory env lives in env_file) ───


class TestSpecHashEnvDigest:
    def test_env_digest_changes_hash(self) -> None:
        """Providing an env digest must change the hash vs. omitting it."""
        assert bay_spec_hash(_WEBHOOK_SPEC, env_digest="abc123") != bay_spec_hash(
            _WEBHOOK_SPEC
        )

    def test_env_digest_deterministic(self) -> None:
        assert bay_spec_hash(_WEBHOOK_SPEC, env_digest="d") == bay_spec_hash(
            _WEBHOOK_SPEC, env_digest="d"
        )

    def test_different_env_digest_different_hash(self) -> None:
        """Secret rotation (different rendered env) busts the hash."""
        assert bay_spec_hash(_WEBHOOK_SPEC, env_digest="rev1") != bay_spec_hash(
            _WEBHOOK_SPEC, env_digest="rev2"
        )

    def test_empty_env_digest_equals_omitted(self) -> None:
        """Falsy digest is a no-op — preserves the rig/infra hash contract."""
        base = bay_spec_hash(_WEBHOOK_SPEC)
        assert bay_spec_hash(_WEBHOOK_SPEC, env_digest="") == base
        assert bay_spec_hash(_WEBHOOK_SPEC, env_digest=None) == base

    def test_env_digest_not_reversible_in_output(self) -> None:
        """The label value is a one-way sha256 — never contains plaintext."""
        h = bay_spec_hash(_WEBHOOK_SPEC, env_digest="PLAINTEXT_SECRET_VALUE")
        assert "PLAINTEXT_SECRET_VALUE" not in h
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)
