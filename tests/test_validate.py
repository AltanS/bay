"""Tests for bay validate command."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from bay_cli.commands.validate import (
    ValidationResult,
    _check_depends_on,
    _check_domain_uniqueness,
    _check_vault_keys,
    _is_docker_hub_image,
    _load_schema,
    _regions_overlap,
    _to_plain,
    _validate_cross_references,
)


# ── ValidationResult tests ───────────────────────────────────────────────


def test_result_tracks_pass_fail_warn(capsys):
    """ValidationResult correctly tracks outcomes."""
    from bay_cli.console.output import set_json_mode

    set_json_mode(True)  # suppress Rich output
    try:
        r = ValidationResult()
        r.ok("check 1")
        r.fail("check 2")
        r.warn("check 3")

        assert len(r.passed) == 1
        assert len(r.failed) == 1
        assert len(r.warnings) == 1
        assert r.total_issues == 1

        d = r.to_dict()
        assert d["total_passed"] == 1
        assert d["total_failed"] == 1
        assert d["total_warnings"] == 1
    finally:
        set_json_mode(False)


# ── Schema loading ───────────────────────────────────────────────────────


def test_schema_loads():
    """Schema file loads and has expected structure."""
    schema = _load_schema()
    assert "$defs" in schema
    assert "service" in schema["$defs"]
    assert "accessory" in schema["$defs"]
    assert "build_block" in schema["$defs"]
    assert "env_block" in schema["$defs"]


# ── Schema validation tests ─────────────────────────────────────────────


def test_schema_valid_service():
    """A well-formed service passes schema validation."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = {
        "services": {
            "myapp": {
                "image": "nginx:latest",
                "access": "public",
                "domains": ["app.example.com"],
                "ports": {"internal": 8080},
            }
        },
        "accessories": {},
    }
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert errors == [], f"Unexpected errors: {errors}"


def test_schema_valid_build_service():
    """A service with build block (no image) passes."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = {
        "services": {
            "myapp": {
                "build": {
                    "repo": "https://github.com/test/repo",
                    "branch": "main",
                    "dockerfile": "Dockerfile",
                },
                "access": "vpn",
                "domains": ["app.{{ domain_base }}"],
                "ports": {"internal": 3000},
            }
        },
    }
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert errors == [], f"Unexpected errors: {errors}"


def test_schema_rejects_missing_required():
    """Missing required fields are flagged."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = {
        "services": {
            "myapp": {
                "image": "nginx:latest",
                # missing access, domains, ports
            }
        },
    }
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    messages = [e.message for e in errors]
    assert any("'access' is a required property" in m for m in messages)
    assert any("'domains' is a required property" in m for m in messages)
    assert any("'ports' is a required property" in m for m in messages)


def test_schema_allows_image_and_build_together():
    """image + build is schema-valid (for strategy: registry). Business logic enforced by cross-ref validation."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = {
        "services": {
            "myapp": {
                "image": "nginx:latest",
                "build": {"repo": "https://github.com/test/repo", "strategy": "registry"},
                "access": "public",
                "domains": ["app.example.com"],
                "ports": {"internal": 8080},
            }
        },
    }
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert errors == [], f"Unexpected errors: {errors}"


def test_schema_rejects_invalid_access():
    """Invalid access mode is flagged."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = {
        "services": {
            "myapp": {
                "image": "nginx:latest",
                "access": "private",
                "domains": ["app.example.com"],
                "ports": {"internal": 8080},
            }
        },
    }
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert len(errors) > 0


def test_schema_rejects_empty_domains():
    """Empty domains list is flagged."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = {
        "services": {
            "myapp": {
                "image": "nginx:latest",
                "access": "public",
                "domains": [],
                "ports": {"internal": 8080},
            }
        },
    }
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert len(errors) > 0


def test_schema_rejects_invalid_port():
    """Non-positive port is rejected."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = {
        "services": {
            "myapp": {
                "image": "nginx:latest",
                "access": "public",
                "domains": ["app.example.com"],
                "ports": {"internal": 0},
            }
        },
    }
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert len(errors) > 0


def test_schema_valid_accessory():
    """A well-formed accessory passes."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = {
        "accessories": {
            "postgres": {
                "image": "postgres:16",
                "port": "127.0.0.1:5432:5432",
                "env": {
                    "clear": {"POSTGRES_USER": "app"},
                    "secret": ["POSTGRES_PASSWORD"],
                },
                "backup": {"method": "pg_dump"},
            }
        },
    }
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert errors == [], f"Unexpected errors: {errors}"


def test_schema_validates_build_block():
    """Build block requires repo, validates optional fields."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    # Build without repo should fail
    data = {
        "services": {
            "myapp": {
                "build": {"branch": "main"},
                "access": "public",
                "domains": ["app.example.com"],
                "ports": {"internal": 8080},
            }
        },
    }
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert len(errors) > 0, "Build without repo should fail"


def test_schema_validates_env_block():
    """Env block: clear is dict, secret is list or dict."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()

    # Secret as list (valid)
    data = {
        "services": {
            "myapp": {
                "image": "nginx:latest",
                "access": "public",
                "domains": ["app.example.com"],
                "ports": {"internal": 8080},
                "env": {
                    "clear": {"KEY": "val"},
                    "secret": ["SECRET_KEY"],
                },
            }
        },
    }
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(data)) == []

    # Secret as dict (valid)
    data["services"]["myapp"]["env"]["secret"] = {"SECRET_KEY": "VAULT_KEY"}
    assert list(validator.iter_errors(data)) == []


def test_schema_validates_database_block():
    """Database block requires accessory field."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = {
        "services": {
            "myapp": {
                "image": "nginx:latest",
                "access": "public",
                "domains": ["app.example.com"],
                "ports": {"internal": 8080},
                "database": {"accessory": "postgres"},
            }
        },
    }
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(data)) == []

    # Missing accessory should fail
    data["services"]["myapp"]["database"] = {"name": "mydb"}
    errors = list(validator.iter_errors(data))
    assert len(errors) > 0


# ── Cross-reference validation ───────────────────────────────────────────


def test_cross_ref_missing_accessory(capsys):
    """Database referencing non-existent accessory is flagged."""
    from bay_cli.console.output import set_json_mode

    set_json_mode(True)
    try:
        result = ValidationResult()
        data = {
            "services": {
                "myapp": {
                    "database": {"accessory": "nonexistent"},
                }
            },
            "accessories": {},
        }
        _validate_cross_references(data, "group_vars/all/services.yml", result)
        assert len(result.failed) == 1
        assert "nonexistent" in result.failed[0]
    finally:
        set_json_mode(False)


def test_cross_ref_valid_accessory(capsys):
    """Database referencing existing accessory passes."""
    from bay_cli.console.output import set_json_mode

    set_json_mode(True)
    try:
        result = ValidationResult()
        data = {
            "services": {
                "myapp": {
                    "database": {"accessory": "postgres"},
                }
            },
            "accessories": {
                "postgres": {"image": "postgres:16"},
            },
        }
        _validate_cross_references(data, "group_vars/all/services.yml", result)
        assert len(result.failed) == 0
    finally:
        set_json_mode(False)


# ── to_plain conversion ─────────────────────────────────────────────────


def test_to_plain_converts_nested():
    """_to_plain converts ruamel types to plain Python."""
    from ruamel.yaml.comments import CommentedMap

    cm = CommentedMap()
    cm["key"] = "value"
    cm["nested"] = CommentedMap()
    cm["nested"]["inner"] = 42
    cm["list"] = [1, "two", True, None]

    result = _to_plain(cm)
    assert isinstance(result, dict)
    assert not hasattr(result, "ca")  # no CommentedMap attributes
    assert result == {"key": "value", "nested": {"inner": 42}, "list": [1, "two", True, None]}


# ── S3: Connectivity and access pre-checks ───────────────────────────────


class _JsonMode:
    """Context manager to enable JSON mode (suppresses Rich console output)."""

    def __enter__(self):
        from bay_cli.console.output import set_json_mode
        set_json_mode(True)
        return self

    def __exit__(self, *args):
        from bay_cli.console.output import set_json_mode
        set_json_mode(False)


# ── Domain uniqueness ────────────────────────────────────────────────────


def test_domain_uniqueness_no_duplicates():
    """Unique domains across all services passes."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app1": {"domains": ["app1.example.com"], "regions": []},
            "app2": {"domains": ["app2.example.com"], "regions": []},
        }
        _check_domain_uniqueness(services, result)
        assert len(result.failed) == 0
        assert len(result.passed) == 1
        assert "no duplicates" in result.passed[0]


def test_domain_uniqueness_duplicate_same_region():
    """Two services claiming the same domain in overlapping regions is flagged."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app1": {"domains": ["app.example.com"], "regions": []},
            "app2": {"domains": ["app.example.com"], "regions": ["eu"]},
        }
        _check_domain_uniqueness(services, result)
        assert len(result.failed) == 1
        assert "app1" in result.failed[0]
        assert "app2" in result.failed[0]


def test_domain_uniqueness_duplicate_different_regions():
    """Same domain in non-overlapping regions is OK (no conflict)."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app1": {"domains": ["app.{{ domain_base }}"], "regions": ["eu"]},
            "app2": {"domains": ["app.{{ domain_base }}"], "regions": ["na"]},
        }
        _check_domain_uniqueness(services, result)
        assert len(result.failed) == 0


# ── depends_on references ────────────────────────────────────────────────


def test_depends_on_valid():
    """Valid depends_on references pass."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app": {"depends_on": ["postgres"]},
        }
        accessories = {
            "postgres": {"image": "postgres:16"},
        }
        _check_depends_on(services, accessories, result)
        assert len(result.failed) == 0
        assert len(result.passed) == 1


def test_depends_on_missing_ref():
    """depends_on referencing a non-existent entry is flagged."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app": {"depends_on": ["nonexistent"]},
        }
        _check_depends_on(services, {}, result)
        assert len(result.failed) == 1
        assert "nonexistent" in result.failed[0]


def test_depends_on_empty():
    """No depends_on entries means nothing to check (pass)."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app": {"image": "nginx"},
        }
        _check_depends_on(services, {}, result)
        assert len(result.failed) == 0
        assert len(result.passed) == 1


# ── Vault key validation ────────────────────────────────────────────────


def test_vault_keys_all_present():
    """All referenced secret keys present in vault passes."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app": {"env": {"secret": ["API_KEY"]}},
        }
        parsed_files = {
            "group_vars/production/secrets.yml": {"APP_API_KEY": "secret-value"},
        }
        _check_vault_keys(services, {}, parsed_files, result)
        assert len(result.failed) == 0
        assert any("1 referenced" in p for p in result.passed)


def test_vault_keys_missing_key():
    """Missing vault key is flagged as error."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app": {"env": {"secret": ["API_KEY", "DB_PASS"]}},
        }
        parsed_files = {
            "group_vars/production/secrets.yml": {"APP_API_KEY": "secret-value"},
        }
        _check_vault_keys(services, {}, parsed_files, result)
        assert len(result.failed) == 1
        assert "APP_DB_PASS" in result.failed[0]


def test_vault_keys_dict_form():
    """Secret as dict (env_var -> vault_key) uses vault_key for lookup."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app": {"env": {"secret": {"DATABASE_URL": "DB_URL_SECRET"}}},
        }
        parsed_files = {
            "group_vars/production/secrets.yml": {"DB_URL_SECRET": "postgres://..."},
        }
        _check_vault_keys(services, {}, parsed_files, result)
        assert len(result.failed) == 0


def test_vault_keys_no_vault():
    """Missing vault file produces a warning, not an error."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app": {"env": {"secret": ["API_KEY"]}},
        }
        parsed_files = {}  # no secrets.yml parsed
        _check_vault_keys(services, {}, parsed_files, result)
        assert len(result.failed) == 0
        assert len(result.warnings) == 1
        assert "not decryptable" in result.warnings[0]


def test_vault_keys_no_secrets():
    """No secret references means nothing to check."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "app": {"env": {"clear": {"KEY": "val"}}},
        }
        _check_vault_keys(services, {}, {}, result)
        assert len(result.failed) == 0
        assert any("no secret references" in p for p in result.passed)


def test_vault_keys_accessory_secrets():
    """Accessory secret references are also checked."""
    with _JsonMode():
        result = ValidationResult()
        accessories = {
            "beszel-agent": {
                "env": {"secret": {"TOKEN": "BESZEL_TOKEN", "KEY": "BESZEL_KEY"}},
            },
        }
        parsed_files = {
            "group_vars/production/secrets.yml": {
                "BESZEL_TOKEN": "tok",
                "BESZEL_KEY": "key",
            },
        }
        _check_vault_keys({}, accessories, parsed_files, result)
        assert len(result.failed) == 0
        assert any("2 referenced" in p for p in result.passed)


# ── Region overlap helper ────────────────────────────────────────────────


def test_regions_overlap_both_empty():
    """Both empty = all regions = overlap."""
    assert _regions_overlap([], []) is True


def test_regions_overlap_one_empty():
    """One empty = all regions = overlap with anything."""
    assert _regions_overlap([], ["eu"]) is True
    assert _regions_overlap(["na"], []) is True


def test_regions_overlap_matching():
    """Overlapping region lists detected."""
    assert _regions_overlap(["eu", "na"], ["na"]) is True


def test_regions_overlap_disjoint():
    """Non-overlapping region lists."""
    assert _regions_overlap(["eu"], ["na"]) is False


# ── Docker Hub image detection ───────────────────────────────────────────


def test_is_docker_hub_simple():
    """Simple image names (no slash) are Docker Hub."""
    assert _is_docker_hub_image("nginx") is True


def test_is_docker_hub_org_repo():
    """org/repo format is Docker Hub."""
    assert _is_docker_hub_image("vaultwarden/server") is True


def test_is_docker_hub_ghcr():
    """ghcr.io images are NOT Docker Hub."""
    assert _is_docker_hub_image("ghcr.io/user/repo") is False


def test_is_docker_hub_custom_registry():
    """Custom registry with dots is NOT Docker Hub."""
    assert _is_docker_hub_image("docker.stirlingpdf.com/stirlingtools/stirling-pdf") is False
    assert _is_docker_hub_image("registry.example.com:5000/myimage") is False


# ── S4: Deploy flow integration ──────────────────────────────────────────


def test_run_validation_returns_result(tmp_path):
    """run_validation returns a ValidationResult with checks applied."""
    from bay_cli.commands.validate import run_validation

    # Create minimal consumer structure
    gv = tmp_path / "group_vars" / "all"
    gv.mkdir(parents=True)
    (gv / "services.yml").write_text(
        "---\nservices:\n  app:\n    image: nginx:latest\n"
        "    access: public\n    domains:\n      - app.example.com\n"
        "    ports:\n      internal: 8080\naccessories: {}\n"
    )
    (gv / "main.yml").write_text("---\ndomain_base: example.com\n")
    hosts_dir = tmp_path / "hosts"
    hosts_dir.mkdir()
    (hosts_dir / "production").write_text("[production]\n10.0.0.1\n")

    # Need .bay dir for find_bay_dir — just mock the paths
    with _JsonMode():
        result = run_validation(tmp_path, "production", show_banner=False)

    assert isinstance(result, ValidationResult)
    # Should have passed YAML checks and schema checks
    assert result.total_issues == 0
    assert len(result.passed) > 0


def test_run_validation_catches_schema_errors(tmp_path):
    """run_validation flags schema errors and returns non-zero total_issues."""
    from bay_cli.commands.validate import run_validation

    gv = tmp_path / "group_vars" / "all"
    gv.mkdir(parents=True)
    # Service missing required fields (access, domains, ports)
    (gv / "services.yml").write_text(
        "---\nservices:\n  bad:\n    image: nginx:latest\naccessories: {}\n"
    )
    hosts_dir = tmp_path / "hosts"
    hosts_dir.mkdir()
    (hosts_dir / "production").write_text("[production]\n10.0.0.1\n")

    with _JsonMode():
        result = run_validation(tmp_path, "production", show_banner=False)

    assert result.total_issues > 0
    # Should flag missing required fields
    assert any("required" in f for f in result.failed)


def test_deploy_skip_validate_flag():
    """--skip-validate flag is accepted by the deploy command."""
    from bay_cli.cli import app
    from typer.testing import CliRunner

    runner = CliRunner()
    # Invoke with --skip-validate — will fail because no .bay dir,
    # but we check that the flag is accepted without crashing on validation
    result = runner.invoke(app, ["deploy", "production", "--skip-validate"])
    # It should fail due to missing .bay dir, NOT due to unknown option
    assert "--skip-validate" not in result.output or "Error" not in result.output
    # The key test: it should NOT say "No such option"
    assert "No such option: --skip-validate" not in result.output
