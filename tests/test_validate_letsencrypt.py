"""`bin/bay validate` must hard-fail on a missing Let's Encrypt email.

Traefik uses `letsencrypt_email` unconditionally for the ACME resolver -- the
framework has no `acme_enabled` opt-out -- so every routed service asks Let's
Encrypt for a certificate on the first deploy. An empty value is always a
broken SSL config. `example/group_vars/production/domains.yml` shipped it
empty, and nothing checked it.
"""

from __future__ import annotations


import pytest
import yaml

from bay_cli.catalog import _package_framework_root
from bay_cli.commands.validate import (
    ValidationResult,
    _validate_letsencrypt_email,
)

ROOT = _package_framework_root()


def _check(value: object | None, *, present: bool = True) -> ValidationResult:
    result = ValidationResult()
    data = {"letsencrypt_email": value} if present else {"domain_base": "example.com"}
    _validate_letsencrypt_email({"group_vars/production/domains.yml": data}, result)
    return result


def test_a_real_address_passes() -> None:
    assert _check("ops@example.com").failed == []


@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_is_a_hard_failure(value: object | None) -> None:
    assert _check(value).failed


def test_missing_key_is_a_hard_failure() -> None:
    assert _check(None, present=False).failed


def test_placeholder_is_a_hard_failure() -> None:
    failed = _check("CHANGE-ME@example.com").failed
    assert failed
    assert "placeholder" in failed[0]


def test_a_non_address_is_a_hard_failure() -> None:
    assert _check("not-an-email").failed


def test_the_env_file_wins_over_group_vars_all() -> None:
    result = ValidationResult()
    _validate_letsencrypt_email(
        {
            "group_vars/all/main.yml": {"letsencrypt_email": "ops@example.com"},
            "group_vars/production/domains.yml": {"letsencrypt_email": ""},
        },
        result,
    )
    assert result.failed, "env-level group_vars outrank group_vars/all"


# ── The shipped example tree ─────────────────────────────────────────────


def test_the_example_ships_a_placeholder_validate_rejects() -> None:
    domains = ROOT / "example" / "group_vars" / "production" / "domains.yml"
    value = yaml.safe_load(domains.read_text())["letsencrypt_email"]
    assert value, "an empty value reads as 'nothing to change here'"
    assert _check(value).failed, "the example placeholder must fail validate"


