"""Scaffolded secrets are generated, and validate rejects empty ones.

The wizard used to write `KEY: ""` for every secret a selected service
needs, and `bay validate` only checked that the key was *present*. So a
fresh project reported "all 4 referenced secret(s) present" while all four
were the empty string — which reaches the container as an empty password
and fails hours later as "auth is broken", not "you never filled this in".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bay_cli.commands.validate import ValidationResult, _check_vault_keys
from bay_cli.utils.secret_gen import generate_password
from bay_cli.wizard.models import WizardResult
from bay_cli.wizard.scaffold import generated_secrets_for, scaffold

ALL_SERVICES = ["gatus", "postgres", "mariadb", "redis", "vaultwarden", "n8n", "plausible", "umami"]


def _scaffold(tmp_path: Path, services: list[str]) -> dict:
    scaffold(
        WizardResult(
            project_name="testapp",
            multi_region=False,
            server_ip="203.0.113.10",
            domain_base="example.com",
            letsencrypt_email="ops@example.com",
            ssh_keys=[],
            access_gateway="none",
            selected_services=services,
        ),
        tmp_path,
    )
    return yaml.safe_load((tmp_path / "group_vars/production/secrets.yml").read_text())


# ── The generator ────────────────────────────────────────────────────────


def test_generator_is_the_one_bin_bay_secret_uses() -> None:
    """`bin/bay secret` and the wizard must not drift apart."""
    from bay_cli.commands import secret as secret_cmd

    assert secret_cmd.generate_password is generate_password
    assert len(generate_password()) == 32
    assert len(generate_password(64)) == 64
    assert generate_password() != generate_password()


def test_generated_secrets_cover_only_selected_services() -> None:
    assert generated_secrets_for(["gatus", "redis"]) == {}
    assert set(generated_secrets_for(["postgres"])) == {"POSTGRES_PASSWORD"}
    assert set(generated_secrets_for(["mariadb"])) == {
        "MARIADB_ROOT_PASSWORD",
        "MARIADB_PASSWORD",
    }


# ── The scaffolded file ──────────────────────────────────────────────────


def test_no_scaffolded_secret_is_empty(tmp_path: Path) -> None:
    data = _scaffold(tmp_path, ALL_SERVICES)
    empty = [k for k, v in (data["secrets"] or {}).items() if not str(v or "").strip()]
    assert empty == []


def test_scaffolded_secrets_are_all_distinct(tmp_path: Path) -> None:
    values = list((_scaffold(tmp_path, ALL_SERVICES)["secrets"] or {}).values())
    assert len(values) == len(set(values))


def test_validate_accepts_the_scaffolded_pair(tmp_path: Path) -> None:
    secrets = _scaffold(tmp_path, ALL_SERVICES)
    services = yaml.safe_load((tmp_path / "group_vars/all/services.yml").read_text())
    result = ValidationResult()
    _check_vault_keys(
        services.get("services") or {},
        services.get("accessories") or {},
        {"group_vars/production/secrets.yml": secrets},
        result,
    )
    assert result.failed == []


# ── validate ─────────────────────────────────────────────────────────────


def _services_with_secret() -> dict:
    return {
        "myapp": {
            "image": "x:1",
            "access": "public",
            "domains": ["a.example.com"],
            "ports": {"internal": 3000},
            "env": {"secret": ["SESSION_SECRET"]},
        }
    }


@pytest.mark.parametrize("value", ["", "   ", None])
def test_validate_fails_on_an_empty_secret(value) -> None:
    result = ValidationResult()
    _check_vault_keys(
        _services_with_secret(),
        {},
        {"group_vars/production/secrets.yml": {"secrets": {"MYAPP_SESSION_SECRET": value}}},
        result,
    )
    assert result.failed
    assert "empty" in " ".join(result.failed)
    assert "bin/bay secret" in " ".join(result.failed)


def test_validate_still_fails_on_a_missing_secret() -> None:
    result = ValidationResult()
    _check_vault_keys(
        _services_with_secret(), {}, {"group_vars/production/secrets.yml": {"secrets": {}}}, result
    )
    assert "missing" in " ".join(result.failed)


def test_validate_passes_on_a_filled_secret() -> None:
    result = ValidationResult()
    _check_vault_keys(
        _services_with_secret(),
        {},
        {"group_vars/production/secrets.yml": {"secrets": {"MYAPP_SESSION_SECRET": "s3cret"}}},
        result,
    )
    assert result.failed == []
