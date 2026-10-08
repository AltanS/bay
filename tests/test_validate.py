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
    _validate_config_files,
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


def test_domain_uniqueness_trailing_slash_is_the_same_path():
    """/api and /api/ route the same requests, so they collide."""
    with _JsonMode():
        result = ValidationResult()
        services = {
            "a": {"domains": ["app.example.com"], "path": "/api", "regions": []},
            "b": {"domains": ["app.example.com"], "path": "/api/", "regions": []},
        }
        _check_domain_uniqueness(services, result)
        assert any("with path '/api'" in f for f in result.failed), result.failed
        result = ValidationResult()
        services["b"]["path"] = "/other/"
        _check_domain_uniqueness(services, result)
        assert result.failed == []


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
    (gv / "main.yml").write_text(
            "---\ndomain_base: example.com\nletsencrypt_email: ops@example.com\n"
        )
    hosts_dir = tmp_path / "hosts"
    hosts_dir.mkdir()
    (hosts_dir / "production").write_text("[production]\n10.0.0.1\n")

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


# ── config_files: where a mounted file may live ─────────────────────────


def _config_files_services(*entries: str) -> dict:
    return {"services": {"gatus": {"image": "example/gatus:1", "config_files": list(entries)}}}


def _check_config_files(root: Path, entries: list[str], files_root: Path | None = None):
    from bay_cli.console.output import set_json_mode

    result = ValidationResult()
    set_json_mode(True)  # suppress Rich output
    try:
        _validate_config_files(root, _config_files_services(*entries), result, files_root)
    finally:
        set_json_mode(False)
    return result


def _write(path: Path, text: str = "x: 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_validate_accepts_a_mount_beside_the_toml(tmp_path: Path):
    """A file moved beside the project's bay.toml (Bay 2.1) is found without files/."""
    _write(tmp_path / "projects" / "gatus" / "bay.toml", 'name = "gatus"\n')
    _write(tmp_path / "projects" / "gatus" / "config.yaml")
    assert not (tmp_path / "files").exists()

    result = _check_config_files(tmp_path, ["gatus/config.yaml"])

    assert result.total_issues == 0, result.failed


def test_validate_still_accepts_the_deprecated_files_place(tmp_path: Path):
    _write(tmp_path / "files" / "gatus" / "config.yaml")

    result = _check_config_files(tmp_path, ["gatus/config.yaml"])

    assert result.total_issues == 0, result.failed


def test_up_validation_reads_the_scratch_files_root(tmp_path: Path):
    """With the compile's files root, a file only there counts, and run_validation passes it on."""
    fleet = tmp_path / "fleet"
    scratch = tmp_path / "scratch" / "files"
    fleet.mkdir()
    _write(scratch / "gatus" / "config.yaml")

    assert _check_config_files(fleet, ["gatus/config.yaml"], scratch).total_issues == 0
    # Without the scratch root the same entry is missing, so the root decides.
    assert _check_config_files(fleet, ["gatus/config.yaml"]).total_issues == 1


def test_up_passes_its_files_root_into_the_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """default_deploy hands config_files_root to run_validation (the call bay up makes)."""
    from types import SimpleNamespace

    from bay_cli import apply as applymod
    from bay_cli.commands import ops, validate

    seen: dict = {}

    def fake_validation(root, env, **kw):
        seen.update(kw)
        return SimpleNamespace(total_issues=0)

    monkeypatch.setattr(validate, "run_validation", fake_validation)
    monkeypatch.setattr(ops, "_run_playbook", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_invalidate_rig_cache", lambda *_: None)
    monkeypatch.setattr(ops, "_run_post_deploy_healthcheck", lambda *a, **k: None)
    cx = SimpleNamespace(
        fleet_root=tmp_path, framework_root=tmp_path, cache_dir=tmp_path / "cache"
    )
    monkeypatch.setattr("bay_cli.receipts.deploy_extra_vars", lambda _cx: [])

    applymod.default_deploy(cx, "production", config_files_root=tmp_path / "scratch" / "files")

    assert seen["config_files_root"] == tmp_path / "scratch" / "files"


def test_validate_names_every_place_when_the_file_is_in_none(tmp_path: Path):
    fleet = tmp_path / "fleet"
    scratch = tmp_path / "scratch" / "files"
    fleet.mkdir()
    scratch.mkdir(parents=True)

    result = _check_config_files(fleet, ["gatus/config.yaml"], scratch)

    assert result.total_issues == 1
    message = result.failed[0]
    assert "services.gatus.config_files: 'gatus/config.yaml' has no file at" in message
    assert "the compile's files/gatus/config.yaml" in message
    assert "projects/gatus/config.yaml" in message
    assert "files/gatus/config.yaml" in message
    assert "create it, or drop the entry" in message

    standalone = _check_config_files(fleet, ["gatus/config.yaml"])
    assert standalone.total_issues == 1
    assert "compile" not in standalone.failed[0]


# ── M118/05: ingress box, box names and the hosts pattern ───────────────


def _box_fleet(tmp_path: Path, fleet_toml: str, hosts: dict[str, str]) -> Path:
    """A fleet dir with bay.fleet.toml, the hosts files and an empty group_vars/all."""
    (tmp_path / "group_vars" / "all").mkdir(parents=True)
    (tmp_path / "hosts").mkdir()
    (tmp_path / "bay.fleet.toml").write_text(fleet_toml)
    for env, text in hosts.items():
        (tmp_path / "hosts" / env).write_text(text)
    return tmp_path


def _gv(root: Path, group: str, text: str, name: str = "main.yml") -> None:
    (root / "group_vars" / group).mkdir(parents=True, exist_ok=True)
    (root / "group_vars" / group / name).write_text(text)


def _parsed(root: Path) -> dict:
    import yaml as pyyaml

    out = {}
    for path in sorted((root / "group_vars").glob("*/*.yml")):
        out[str(path.relative_to(root))] = pyyaml.safe_load(path.read_text())
    return out


def _ingress(root: Path, bay_dir: Path | None = None) -> ValidationResult:
    from bay_cli.commands.validate import _validate_ingress_box

    result = ValidationResult()
    with _JsonMode():
        _validate_ingress_box(root, _parsed(root), bay_dir, result)
    return result


_THREE_BOXES = """\
name = "acme"
[boxes.eu]
env = "prod"
group = "eu"
[boxes.gw]
env = "prod"
group = "gw"
[boxes.na]
env = "prod"
group = "na"
[tailnet]
ingress_box = "gw"
"""

# The live shape: IP host lines, one group per box, the box env as a parent group.
_THREE_HOSTS = """\
[eu]
192.0.2.10
[na]
192.0.2.11
[gw]
192.0.2.12
[prod:children]
eu
na
gw
"""


def test_validate_ingress_box_is_headscale_host(tmp_path: Path) -> None:
    root = _box_fleet(tmp_path, _THREE_BOXES, {"prod": _THREE_HOSTS})

    # The gateway is not headscale (the role default, read from the role file).
    fake_framework = tmp_path / "framework"
    _gv_dir = fake_framework / "roles" / "access_gateway" / "defaults"
    _gv_dir.mkdir(parents=True)
    (_gv_dir / "main.yml").write_text("---\naccess_gateway: wireguard\n")
    result = _ingress(root, fake_framework)
    assert result.failed == ["Ingress box         ingress_box gw is not the Headscale host "
                             "(access_gateway = wireguard)"]

    # headscale, control region set and different.
    _gv(root, "all", "---\naccess_gateway: headscale\nheadscale_control_region: eu\n",
        "access_gateway.yml")
    _gv(root, "gw", "---\nregion: gw\n")
    result = _ingress(root)
    assert len(result.failed) == 1
    assert "ingress_box gw is not the Headscale host (headscale_control_region = eu)" in (
        result.failed[0]
    )

    # Control region set and equal: ok. The region comes from group_vars/<group>/.
    _gv(root, "all", "---\naccess_gateway: headscale\nheadscale_control_region: gw\n",
        "access_gateway.yml")
    result = _ingress(root)
    assert result.failed == [] and result.warnings == []
    assert result.passed and "gw runs Headscale" in result.passed[0]

    # A region var wins over the group name.
    _gv(root, "gw", "---\nregion: infra-zone\n")
    assert len(_ingress(root).failed) == 1
    _gv(root, "all", "---\naccess_gateway: headscale\nheadscale_control_region: infra-zone\n",
        "access_gateway.yml")
    assert _ingress(root).failed == []
    # No region var: the box group is the region.
    (root / "group_vars" / "gw" / "main.yml").unlink()
    assert len(_ingress(root).failed) == 1
    _gv(root, "all", "---\naccess_gateway: headscale\nheadscale_control_region: gw\n",
        "access_gateway.yml")
    assert _ingress(root).failed == []

    # No control region: every host runs Headscale, so any ingress box is fine.
    _gv(root, "all", "---\naccess_gateway: headscale\n", "access_gateway.yml")
    result = _ingress(root)
    assert result.failed == [] and result.warnings == [] and result.passed

    # No ingress_box: nothing to check.
    (root / "bay.fleet.toml").write_text(_THREE_BOXES.replace('ingress_box = "gw"\n', ""))
    result = _ingress(root)
    assert result.failed == [] and result.passed == []


def _boxes(root: Path) -> ValidationResult:
    from bay_cli.commands.validate import _validate_box_inventory

    result = ValidationResult()
    with _JsonMode():
        _validate_box_inventory(root, result)
    return result


def test_validate_box_names_match_inventory(tmp_path: Path) -> None:
    # The live shape passes with no warning.
    root = _box_fleet(tmp_path / "a", _THREE_BOXES, {"prod": _THREE_HOSTS})
    result = _boxes(root)
    assert result.failed == [] and result.warnings == []
    assert result.passed == ["Boxes               3 box(es) match the inventory"]

    # One box per env, box name = group name, no group key.
    single = _box_fleet(
        tmp_path / "b",
        'name = "acme"\n[boxes.prod]\nenv = "prod"\n',
        {"prod": "[prod]\n192.0.2.20\n"},
    )
    result = _boxes(single)
    assert result.failed == [] and result.warnings == []

    # Two box envs, each its own file; an extra group in one file.
    two = _box_fleet(
        tmp_path / "c",
        'name = "acme"\n[boxes.test]\nenv = "test"\n',
        {"test": "[test]\n192.0.2.30\n[gw]\n192.0.2.31\n"},
    )
    result = _boxes(two)
    assert result.failed == [] and result.warnings == []

    # A box group that is not in the hosts file: error.
    text = _THREE_HOSTS.replace("[na]\n192.0.2.11\n", "").replace("na\n", "")
    (root / "hosts" / "prod").write_text(text)
    result = _boxes(root)
    assert result.failed == ["Boxes               box na: group na is not in hosts/prod"]
    # ...and the box name is then neither a host nor a group: a warning.
    assert len(result.warnings) == 1
    assert "box na is neither a host nor a group in hosts/prod" in result.warnings[0]
    assert "receipt `box` field" in result.warnings[0]

    # A box name that is a host line is fine.
    hosted = _box_fleet(
        tmp_path / "d",
        'name = "acme"\n[boxes.app-1]\nenv = "prod"\n',
        {"prod": "[prod]\napp-1 ansible_host=192.0.2.40\n"},
    )
    assert _boxes(hosted).warnings == []
    # A box whose name the inventory does not know: warning only.
    other = _box_fleet(
        tmp_path / "e",
        'name = "acme"\n[boxes.web]\nenv = "prod"\n',
        {"prod": "[prod]\n192.0.2.50\n"},
    )
    result = _boxes(other)
    assert result.failed == [] and len(result.warnings) == 1
    assert "box web is neither a host nor a group in hosts/prod" in result.warnings[0]


def test_validate_hosts_pattern_matches_box_env(tmp_path: Path) -> None:
    # Only the region groups: the box env matches no host.
    text = "[eu]\n192.0.2.10\n[na]\n192.0.2.11\n[gw]\n192.0.2.12\n"
    root = _box_fleet(tmp_path, _THREE_BOXES, {"prod": text})
    result = _boxes(root)
    assert result.failed == [
        "Boxes               hosts/prod has no host or group named prod; Ansible gets that "
        "name as its host pattern. Add [prod:children] and list the groups under it"
    ]
    # The fix the hint names.
    (root / "hosts" / "prod").write_text(text + "[prod:children]\neu\nna\ngw\n")
    assert _boxes(root).failed == []

    # Through run_validation: the check is part of bay validate.
    from bay_cli.commands.validate import run_validation

    (root / "hosts" / "prod").write_text(text)
    with _JsonMode():
        full = run_validation(root, "prod", show_banner=False)
    assert any("has no host or group named prod" in f for f in full.failed)
