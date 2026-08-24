"""Tests for build strategy model changes.

Covers:
- Schema acceptance of build.strategy: local / remote / push / registry
- Schema rejection of invalid strategy values
- Schema acceptance of mem_limit and replicas fields
- Cross-reference validation: registry strategy requires image
- Cross-reference validation: remote/push strategy requires image
- Cross-reference validation: image+build without registry/remote strategy fails
- Deprecation warning for legacy app_build_strategy variable
"""

from __future__ import annotations

import pytest

from bay_cli.commands.validate import (
    ValidationResult,
    _load_schema,
    _validate_cross_references,
    _validate_deprecations,
)
from bay_cli.console.output import set_json_mode
from jsonschema import Draft202012Validator


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_build_service(extra_svc: dict | None = None, build_extra: dict | None = None) -> dict:
    """Return a minimal services.yml data dict with a build-only service."""
    svc = {
        "build": {
            "repo": "https://github.com/example/app",
            "branch": "main",
            **(build_extra or {}),
        },
        "access": "public",
        "domains": ["app.example.com"],
        "ports": {"internal": 3000},
        **(extra_svc or {}),
    }
    return {"services": {"myapp": svc}, "accessories": {}}


def _make_image_service(extra_svc: dict | None = None) -> dict:
    """Return a minimal services.yml data dict with an image-only service."""
    svc = {
        "image": "nginx:latest",
        "access": "public",
        "domains": ["app.example.com"],
        "ports": {"internal": 8080},
        **(extra_svc or {}),
    }
    return {"services": {"myapp": svc}, "accessories": {}}


# ── Schema tests ─────────────────────────────────────────────────────────


class TestSchemaBuildStrategy:
    """Schema-level validation for build.strategy and related service fields."""

    def setup_method(self):
        set_json_mode(True)

    def teardown_method(self):
        set_json_mode(False)

    def test_schema_build_service_with_strategy_local(self):
        """build block with strategy: local passes schema validation."""
        schema = _load_schema()
        data = _make_build_service(build_extra={"strategy": "local"})
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected errors: {errors}"

    def test_schema_build_service_with_strategy_registry_and_image(self):
        """build + image + strategy: registry passes schema validation."""
        schema = _load_schema()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/myapp:latest"},
            build_extra={"strategy": "registry"},
        )
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected errors: {errors}"

    def test_schema_build_service_with_strategy_push(self):
        """build block with strategy: push passes schema validation (deprecated alias)."""
        schema = _load_schema()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/stack/myapp:latest"},
            build_extra={"strategy": "push"},
        )
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected errors: {errors}"

    def test_schema_build_service_with_strategy_remote(self):
        """build block with strategy: remote passes schema validation."""
        schema = _load_schema()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/stack/myapp:latest"},
            build_extra={"strategy": "remote"},
        )
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected errors: {errors}"

    def test_schema_rejects_invalid_strategy(self):
        """strategy: 'cloud' and strategy: 'controller' are rejected by the enum."""
        schema = _load_schema()
        validator = Draft202012Validator(schema)

        for bad_strategy in ("cloud", "controller"):
            data = _make_build_service(build_extra={"strategy": bad_strategy})
            errors = list(validator.iter_errors(data))
            assert len(errors) > 0, (
                f"Expected schema error for strategy: {bad_strategy!r}"
            )
            validators_used = [e.validator for e in errors]
            assert "enum" in validators_used, (
                f"Expected 'enum' validator error for strategy: {bad_strategy!r}, "
                f"got validators: {validators_used}"
            )

    def test_schema_accepts_mem_limit_on_service(self):
        """service with mem_limit: '512m' passes schema validation."""
        schema = _load_schema()
        data = _make_image_service(extra_svc={"mem_limit": "512m"})
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected errors: {errors}"

    def test_schema_accepts_replicas_on_service(self):
        """service with replicas: 2 passes schema validation."""
        schema = _load_schema()
        data = _make_image_service(extra_svc={"replicas": 2})
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected errors: {errors}"

    def test_schema_accepts_mem_limit_on_accessory(self):
        """accessory with mem_limit: '1g' passes schema validation."""
        schema = _load_schema()
        data = {
            "services": {},
            "accessories": {
                "postgres": {
                    "image": "postgres:16",
                    "mem_limit": "1g",
                }
            },
        }
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected errors: {errors}"

    def test_schema_build_with_args(self):
        """build block with args dict passes schema validation."""
        schema = _load_schema()
        data = _make_build_service(
            build_extra={
                "args": {
                    "NODE_ENV": "production",
                    "APP_VERSION": "1.2.3",
                }
            }
        )
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected errors: {errors}"

    def test_schema_build_with_secrets(self):
        """build block with secrets dict passes schema validation."""
        schema = _load_schema()
        data = _make_build_service(
            build_extra={
                "secrets": {
                    "NPM_TOKEN": "vault_npm_token",
                    "GITHUB_TOKEN": "vault_github_token",
                }
            }
        )
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected errors: {errors}"


# ── Cross-reference validation tests ─────────────────────────────────────


class TestCrossReferenceBuildStrategy:
    """Cross-reference validation for build.strategy business rules."""

    def setup_method(self):
        set_json_mode(True)

    def teardown_method(self):
        set_json_mode(False)

    def test_validate_registry_strategy_requires_image(self):
        """build.strategy: registry without image: produces a failure."""
        result = ValidationResult()
        data = _make_build_service(build_extra={"strategy": "registry"})
        _validate_cross_references(data, "services.yml", result)
        assert len(result.failed) > 0, (
            "Expected a failure for registry strategy without image"
        )
        assert any("registry" in msg for msg in result.failed), (
            f"Expected failure mentioning 'registry', got: {result.failed}"
        )

    def test_validate_image_plus_build_without_registry_strategy_fails(self):
        """image + build without strategy: registry produces a failure."""
        result = ValidationResult()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/myapp:latest"},
            build_extra={"strategy": "local"},
        )
        _validate_cross_references(data, "services.yml", result)
        assert len(result.failed) > 0, (
            "Expected a failure for image+build without strategy:registry"
        )
        assert any("image" in msg and "build" in msg for msg in result.failed), (
            f"Expected failure mentioning both 'image' and 'build', got: {result.failed}"
        )

    def test_validate_image_plus_build_with_registry_strategy_passes(self):
        """image + build + strategy: registry does NOT produce a failure."""
        result = ValidationResult()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/myapp:latest"},
            build_extra={"strategy": "registry"},
        )
        _validate_cross_references(data, "services.yml", result)
        assert len(result.failed) == 0, (
            f"Expected no failures for image+build+strategy:registry, got: {result.failed}"
        )

    def test_validate_build_only_no_strategy_passes(self):
        """build-only service (no image) with no explicit strategy passes."""
        result = ValidationResult()
        data = _make_build_service()
        _validate_cross_references(data, "services.yml", result)
        assert len(result.failed) == 0, (
            f"Expected no failures for build-only service, got: {result.failed}"
        )

    def test_validate_remote_strategy_requires_image(self):
        """build.strategy: remote without image: produces a failure."""
        result = ValidationResult()
        data = _make_build_service(build_extra={"strategy": "remote"})
        _validate_cross_references(data, "services.yml", result)
        assert len(result.failed) > 0, (
            "Expected a failure for remote strategy without image"
        )
        assert any("remote" in msg for msg in result.failed), (
            f"Expected failure mentioning 'remote', got: {result.failed}"
        )

    def test_validate_remote_strategy_with_image_passes(self):
        """build.strategy: remote + image: does NOT produce a failure."""
        result = ValidationResult()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/stack/myapp:latest"},
            build_extra={"strategy": "remote"},
        )
        _validate_cross_references(data, "services.yml", result)
        assert len(result.failed) == 0, (
            f"Expected no failures for remote+image, got: {result.failed}"
        )

    def test_validate_image_plus_build_with_remote_strategy_passes(self):
        """image + build + strategy: remote does NOT trigger the image+build error."""
        result = ValidationResult()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/stack/myapp:latest"},
            build_extra={"strategy": "remote"},
        )
        _validate_cross_references(data, "services.yml", result)
        # Should not have the "set build.strategy: registry" error
        assert not any("has both 'image' and 'build'" in msg for msg in result.failed), (
            f"Remote strategy should not trigger image+build conflict, got: {result.failed}"
        )

    def test_validate_push_strategy_requires_image(self):
        """build.strategy: push (deprecated) without image: produces a failure."""
        result = ValidationResult()
        data = _make_build_service(build_extra={"strategy": "push"})
        _validate_cross_references(data, "services.yml", result)
        assert len(result.failed) > 0, (
            "Expected a failure for push strategy without image"
        )
        assert any("push" in msg for msg in result.failed), (
            f"Expected failure mentioning 'push', got: {result.failed}"
        )

    def test_validate_push_strategy_with_image_passes(self):
        """build.strategy: push (deprecated) + image: does NOT produce a failure."""
        result = ValidationResult()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/stack/myapp:latest"},
            build_extra={"strategy": "push"},
        )
        _validate_cross_references(data, "services.yml", result)
        assert len(result.failed) == 0, (
            f"Expected no failures for push+image, got: {result.failed}"
        )

    def test_validate_image_plus_build_with_push_strategy_passes(self):
        """image + build + strategy: push does NOT trigger the image+build error."""
        result = ValidationResult()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/stack/myapp:latest"},
            build_extra={"strategy": "push"},
        )
        _validate_cross_references(data, "services.yml", result)
        # Should not have the "set build.strategy: registry" error
        assert not any("has both 'image' and 'build'" in msg for msg in result.failed), (
            f"Push strategy should not trigger image+build conflict, got: {result.failed}"
        )

    def test_validate_webhook_with_remote_strategy_no_warn(self):
        """webhook + remote service produces no warning (auto-builds supported)."""
        result = ValidationResult()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/stack/myapp:latest"},
            build_extra={"strategy": "remote"},
        )
        data["webhook"] = {"domain": "deploy.example.com", "secret": "vault_webhook_secret"}
        _validate_cross_references(data, "services.yml", result)
        # Remote strategy now supports auto-builds via build server webhook
        assert not any("remote" in msg and "webhook" in msg for msg in result.warnings), (
            f"Expected no webhook warning for remote strategy, got: {result.warnings}"
        )

    def test_validate_webhook_with_push_strategy_no_warn(self):
        """webhook + push (deprecated) service produces no warning (auto-builds supported)."""
        result = ValidationResult()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/stack/myapp:latest"},
            build_extra={"strategy": "push"},
        )
        data["webhook"] = {"domain": "deploy.example.com", "secret": "vault_webhook_secret"}
        _validate_cross_references(data, "services.yml", result)
        # Push (deprecated alias for remote) now supports auto-builds
        assert not any("push" in msg and "webhook" in msg for msg in result.warnings), (
            f"Expected no webhook warning for push strategy, got: {result.warnings}"
        )

    def test_validate_webhook_with_registry_strategy_warns(self):
        """webhook + registry service produces a warning."""
        result = ValidationResult()
        data = _make_build_service(
            extra_svc={"image": "registry.example.com/stack/myapp:latest"},
            build_extra={"strategy": "registry"},
        )
        data["webhook"] = {"domain": "deploy.example.com", "secret": "vault_webhook_secret"}
        _validate_cross_references(data, "services.yml", result)
        assert any("registry" in msg and "webhook" in msg for msg in result.warnings), (
            f"Expected warning about registry+webhook, got warnings: {result.warnings}"
        )

    def test_validate_webhook_all_registry_warns(self):
        """webhook + all registry services produces global warning."""
        result = ValidationResult()
        data = {
            "services": {
                "app1": {
                    "build": {"repo": "https://github.com/example/app1", "strategy": "registry"},
                    "image": "registry.example.com/stack/app1:latest",
                    "access": "public",
                    "domains": ["app1.example.com"],
                    "ports": {"internal": 3000},
                },
                "app2": {
                    "build": {"repo": "https://github.com/example/app2", "strategy": "registry"},
                    "image": "registry.example.com/stack/app2:latest",
                    "access": "public",
                    "domains": ["app2.example.com"],
                    "ports": {"internal": 3001},
                },
            },
            "accessories": {},
            "webhook": {"domain": "deploy.example.com", "secret": "vault_webhook_secret"},
        }
        _validate_cross_references(data, "services.yml", result)
        assert any("all build services use registry" in msg for msg in result.warnings), (
            f"Expected global registry warning, got warnings: {result.warnings}"
        )

    def test_validate_webhook_mixed_remote_registry_no_global_warn(self):
        """webhook + mix of remote+registry does NOT produce global warning."""
        result = ValidationResult()
        data = {
            "services": {
                "app1": {
                    "build": {"repo": "https://github.com/example/app1", "strategy": "remote"},
                    "image": "registry.example.com/stack/app1:latest",
                    "access": "public",
                    "domains": ["app1.example.com"],
                    "ports": {"internal": 3000},
                },
                "app2": {
                    "build": {"repo": "https://github.com/example/app2", "strategy": "registry"},
                    "image": "registry.example.com/stack/app2:latest",
                    "access": "public",
                    "domains": ["app2.example.com"],
                    "ports": {"internal": 3001},
                },
            },
            "accessories": {},
            "webhook": {"domain": "deploy.example.com", "secret": "vault_webhook_secret"},
        }
        _validate_cross_references(data, "services.yml", result)
        # Only one registry service, not all — no global warning
        assert not any("all build services use registry" in msg for msg in result.warnings), (
            f"Expected no global registry warning for mixed strategies, got: {result.warnings}"
        )

    def test_validate_webhook_with_local_strategy_no_warn(self):
        """webhook + local strategy service produces no strategy warning."""
        result = ValidationResult()
        data = _make_build_service(build_extra={"strategy": "local"})
        data["webhook"] = {"domain": "deploy.example.com", "secret": "vault_webhook_secret"}
        _validate_cross_references(data, "services.yml", result)
        # Should not have any webhook+strategy warnings
        assert not any("webhook" in msg for msg in result.warnings), (
            f"Expected no webhook warnings for local strategy, got: {result.warnings}"
        )


# ── Deprecation validation tests ─────────────────────────────────────────


class TestDeprecationBuildStrategy:
    """Deprecation warnings for legacy app_build_strategy variable."""

    def setup_method(self):
        set_json_mode(True)

    def teardown_method(self):
        set_json_mode(False)

    def test_deprecation_warns_on_app_build_strategy(self):
        """parsed file containing app_build_strategy produces a warning."""
        result = ValidationResult()
        parsed_files = {
            "group_vars/production/main.yml": {
                "app_build_strategy": "local",
                "domain_base": "example.com",
            }
        }
        _validate_deprecations(parsed_files, result)
        assert len(result.warnings) > 0, (
            "Expected a deprecation warning for app_build_strategy"
        )
        assert any("app_build_strategy" in msg for msg in result.warnings), (
            f"Expected warning mentioning 'app_build_strategy', got: {result.warnings}"
        )

    def test_deprecation_clean_when_no_deprecated_vars(self):
        """No deprecated vars in parsed files produces an ok (no warnings)."""
        result = ValidationResult()
        parsed_files = {
            "group_vars/production/main.yml": {
                "domain_base": "example.com",
                "git_deploy_build_strategy": "local",
            }
        }
        _validate_deprecations(parsed_files, result)
        assert len(result.warnings) == 0, (
            f"Expected no warnings for clean config, got: {result.warnings}"
        )
        assert len(result.passed) > 0, (
            "Expected at least one ok entry for clean deprecation check"
        )
