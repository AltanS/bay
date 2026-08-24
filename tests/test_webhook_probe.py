"""Tests for the GitHub webhook health probe and prune-webhooks command.

Covers:
- parse_repo_owner_name: URL format variants
- probe_service_webhook: match logic, last_response codes, null/missing cases
- find_orphan_hooks: domain scoping, service name matching
- _probe_webhook_health integration into ValidationResult (vault key missing,
  webhook_domain missing, URL pattern mismatch, 404 last_response, orphan hooks)
- service prune-webhooks: dry-run lists without deleting
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bay_cli.github_webhook import (
    ProbeResult,
    find_orphan_hooks,
    parse_repo_owner_name,
    probe_service_webhook,
)


# ── parse_repo_owner_name ─────────────────────────────────────────────────


def test_parse_https_with_git_suffix():
    assert parse_repo_owner_name("https://github.com/MyOrg/myapp.git") == ("MyOrg", "myapp")


def test_parse_https_without_git_suffix():
    assert parse_repo_owner_name("https://github.com/MyOrg/myapp") == ("MyOrg", "myapp")


def test_parse_https_trailing_slash():
    assert parse_repo_owner_name("https://github.com/MyOrg/myapp/") == ("MyOrg", "myapp")


def test_parse_ssh_with_git_suffix():
    assert parse_repo_owner_name("git@github.com:MyOrg/myapp.git") == ("MyOrg", "myapp")


def test_parse_ssh_without_git_suffix():
    assert parse_repo_owner_name("git@github.com:MyOrg/myapp") == ("MyOrg", "myapp")


def test_parse_non_github_returns_none():
    assert parse_repo_owner_name("https://gitlab.com/Org/repo.git") is None


def test_parse_empty_returns_none():
    assert parse_repo_owner_name("") is None


def test_parse_bitbucket_returns_none():
    assert parse_repo_owner_name("https://bitbucket.org/Org/repo") is None


# ── probe_service_webhook ─────────────────────────────────────────────────


def _make_hook(url: str, code: int | None = 200, has_last_response: bool = True) -> dict:
    """Build a minimal GitHub hook dict for testing."""
    hook: dict = {"id": 1, "config": {"url": url}}
    if has_last_response:
        hook["last_response"] = {"code": code, "status": "OK", "message": ""}
    return hook


def test_probe_no_matching_hook_returns_fail():
    hooks = [_make_hook("https://other.example.com/webhook/api", code=200)]
    result = probe_service_webhook(
        "myapp", "https://webhook.example.com/webhook/myapp", hooks
    )
    assert result.status == "fail"
    assert "no webhook found" in result.message


def test_probe_matching_hook_200_returns_ok():
    hooks = [_make_hook("https://webhook.example.com/webhook/myapp", code=200)]
    result = probe_service_webhook(
        "myapp", "https://webhook.example.com/webhook/myapp", hooks
    )
    assert result.status == "ok"
    assert "healthy" in result.message


def test_probe_matching_hook_404_returns_fail():
    hooks = [_make_hook("https://webhook.example.com/webhook/myapp", code=404)]
    result = probe_service_webhook(
        "myapp", "https://webhook.example.com/webhook/myapp", hooks
    )
    assert result.status == "fail"
    assert "404" in result.message


def test_probe_last_response_null_code_returns_note():
    hook = {"id": 1, "config": {"url": "https://webhook.example.com/webhook/myapp"}, "last_response": {"code": None}}
    result = probe_service_webhook(
        "myapp", "https://webhook.example.com/webhook/myapp", [hook]
    )
    assert result.status == "note"
    assert "never received a delivery" in result.message


def test_probe_no_last_response_key_returns_note():
    hook = {"id": 1, "config": {"url": "https://webhook.example.com/webhook/myapp"}}
    result = probe_service_webhook(
        "myapp", "https://webhook.example.com/webhook/myapp", [hook]
    )
    assert result.status == "note"
    assert "never received a delivery" in result.message


def test_probe_last_response_none_returns_note():
    hook = {"id": 1, "config": {"url": "https://webhook.example.com/webhook/myapp"}, "last_response": None}
    result = probe_service_webhook(
        "myapp", "https://webhook.example.com/webhook/myapp", [hook]
    )
    assert result.status == "note"


def test_probe_ignores_non_bay_hooks():
    """Non-bay hooks (Slack, CI, etc.) on the same repo must not cause failures."""
    hooks = [
        _make_hook("https://hooks.slack.com/services/T00/B00/abc123", code=200),
        _make_hook("https://ci.example.com/github/webhook", code=200),
        _make_hook("https://webhook.example.com/webhook/myapp", code=200),
    ]
    result = probe_service_webhook(
        "myapp", "https://webhook.example.com/webhook/myapp", hooks
    )
    assert result.status == "ok"


def test_probe_url_case_insensitive():
    hooks = [_make_hook("HTTPS://Webhook.EXAMPLE.COM/Webhook/MyApp", code=200)]
    result = probe_service_webhook(
        "myapp", "https://webhook.example.com/webhook/myapp", hooks
    )
    assert result.status == "ok"


def test_probe_trailing_slash_normalised():
    hooks = [_make_hook("https://webhook.example.com/webhook/myapp/", code=200)]
    result = probe_service_webhook(
        "myapp", "https://webhook.example.com/webhook/myapp", hooks
    )
    assert result.status == "ok"


# ── find_orphan_hooks ─────────────────────────────────────────────────────


def test_find_orphans_known_service_not_orphan():
    hooks = [{"id": 1, "config": {"url": "https://webhook.example.com/webhook/myapp"}}]
    orphans = find_orphan_hooks(hooks, "webhook.example.com", {"myapp"})
    assert orphans == []


def test_find_orphans_unknown_service_is_orphan():
    hooks = [{"id": 2, "config": {"url": "https://webhook.example.com/webhook/old-service"}}]
    orphans = find_orphan_hooks(hooks, "webhook.example.com", {"myapp"})
    assert len(orphans) == 1
    assert orphans[0]["id"] == 2


def test_find_orphans_different_consumer_domain_not_orphan():
    """A Example Co hook on the same repo is NOT an orphan from demo's perspective."""
    hooks = [
        {"id": 1, "config": {"url": "https://webhook.example.com/webhook/myapp"}},
        {"id": 2, "config": {"url": "https://webhook.other.example.com/webhook/api"}},
    ]
    # demo perspective: only check webhook.example.com domain
    orphans = find_orphan_hooks(hooks, "webhook.example.com", {"myapp"})
    assert orphans == []


def test_find_orphans_multiple_consumers_same_repo():
    """Multi-consumer repo: each consumer sees only its own domain's hooks as potential orphans."""
    hooks = [
        {"id": 1, "config": {"url": "https://webhook.example.com/webhook/myapp"}},
        {"id": 2, "config": {"url": "https://webhook.example.com/webhook/deleted-service"}},
        {"id": 3, "config": {"url": "https://webhook.other.example.com/webhook/api"}},
    ]
    # demo perspective with "myapp" active: hook 2 is orphan, hook 3 is not ours
    orphans = find_orphan_hooks(hooks, "webhook.example.com", {"myapp"})
    assert len(orphans) == 1
    assert orphans[0]["id"] == 2


def test_find_orphans_non_bay_hook_ignored():
    """Hooks on other domains entirely (Slack, CI) are invisible to find_orphan_hooks."""
    hooks = [
        {"id": 1, "config": {"url": "https://hooks.slack.com/services/abc"}},
        {"id": 2, "config": {"url": "https://webhook.example.com/webhook/myapp"}},
    ]
    orphans = find_orphan_hooks(hooks, "webhook.example.com", {"myapp"})
    assert orphans == []


def test_find_orphans_empty_hooks():
    orphans = find_orphan_hooks([], "webhook.example.com", {"myapp"})
    assert orphans == []


# ── _probe_webhook_health ValidationResult integration ───────────────────


def _make_consumer_root(tmp_path: Path, webhook_domain: str | None, github_token: str | None) -> Path:
    """Set up a minimal consumer directory for probe tests."""
    gv_all = tmp_path / "group_vars" / "all"
    gv_all.mkdir(parents=True)

    main_data = "---\nstack_name: teststack\n"
    if webhook_domain:
        main_data += f"webhook_domain: {webhook_domain}\n"
    (gv_all / "main.yml").write_text(main_data)

    if github_token is not None:
        # Write a plain (non-encrypted) secrets.yml for test convenience
        (gv_all / "secrets.yml").write_text(
            f"---\ngithub_admin_token: {github_token}\n" if github_token else "---\n"
        )

    return tmp_path


def _make_services_data(repo_url: str, service_name: str = "myapp") -> dict:
    return {
        "services": {
            service_name: {
                "image": "registry.example.com/stack/myapp:latest",
                "build": {"repo": repo_url, "strategy": "remote"},
                "access": "vpn",
            }
        },
        "accessories": {},
    }


def test_probe_webhook_health_missing_webhook_domain(tmp_path, capsys):
    """vault key missing → probe skips with clear warning."""
    from bay_cli.commands.validate import ValidationResult, _probe_webhook_health
    from bay_cli.console.output import set_json_mode

    set_json_mode(True)
    try:
        root = _make_consumer_root(tmp_path, webhook_domain=None, github_token="tok")
        gv_all = root / "group_vars" / "all"
        parsed = {"group_vars/all/main.yml": {"stack_name": "teststack"}}
        services_data = _make_services_data("https://github.com/Org/repo.git")

        result = ValidationResult()
        _probe_webhook_health(root, "production", services_data, parsed, result)

        assert len(result.warnings) >= 1
        assert any("webhook_domain" in w for w in result.warnings)
        assert result.total_issues == 0  # warnings don't count as failures
    finally:
        set_json_mode(False)


def test_probe_webhook_health_missing_github_token(tmp_path, capsys):
    """github_admin_token missing from vault → probe skips with vault path hint."""
    from bay_cli.commands.validate import ValidationResult, _probe_webhook_health
    from bay_cli.console.output import set_json_mode

    set_json_mode(True)
    try:
        root = _make_consumer_root(tmp_path, webhook_domain="webhook.example.com", github_token=None)
        parsed = {
            "group_vars/all/main.yml": {
                "stack_name": "teststack",
                "webhook_domain": "webhook.example.com",
            }
        }
        services_data = _make_services_data("https://github.com/Org/repo.git")

        result = ValidationResult()
        _probe_webhook_health(root, "production", services_data, parsed, result)

        assert any("github_admin_token" in w for w in result.warnings)
        assert result.total_issues == 0
    finally:
        set_json_mode(False)


def test_probe_webhook_health_url_mismatch_fails(tmp_path):
    """URL pattern mismatch → probe fails."""
    from bay_cli.commands.validate import ValidationResult, _probe_webhook_health
    from bay_cli.console.output import set_json_mode

    set_json_mode(True)
    try:
        root = _make_consumer_root(tmp_path, webhook_domain="webhook.example.com", github_token="tok")
        parsed = {
            "group_vars/all/secrets.yml": {"github_admin_token": "tok"},
            "group_vars/all/main.yml": {
                "stack_name": "teststack",
                "webhook_domain": "webhook.example.com",
            },
        }
        services_data = _make_services_data("https://github.com/Org/repo.git")

        # Hooks have the wrong domain — URL won't match
        hooks = [{"id": 1, "config": {"url": "https://wrong-domain.com/webhook/myapp"}, "last_response": {"code": 200}}]

        with patch("bay_cli.github_webhook.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = hooks
            mock_resp.raise_for_status = MagicMock()
            mock_req.get.return_value = mock_resp

            result = ValidationResult()
            _probe_webhook_health(root, "production", services_data, parsed, result)

        assert any("no webhook found" in f for f in result.failed)
    finally:
        set_json_mode(False)


def test_probe_webhook_health_404_last_response_fails(tmp_path):
    """last_response.code = 404 → probe fails."""
    from bay_cli.commands.validate import ValidationResult, _probe_webhook_health
    from bay_cli.console.output import set_json_mode

    set_json_mode(True)
    try:
        root = _make_consumer_root(tmp_path, webhook_domain="webhook.example.com", github_token="tok")
        parsed = {
            "group_vars/all/secrets.yml": {"github_admin_token": "tok"},
            "group_vars/all/main.yml": {
                "stack_name": "teststack",
                "webhook_domain": "webhook.example.com",
            },
        }
        services_data = _make_services_data("https://github.com/Org/repo.git")

        hooks = [{"id": 1, "config": {"url": "https://webhook.example.com/webhook/myapp"}, "last_response": {"code": 404}}]

        with patch("bay_cli.github_webhook.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = hooks
            mock_resp.raise_for_status = MagicMock()
            mock_req.get.return_value = mock_resp

            result = ValidationResult()
            _probe_webhook_health(root, "production", services_data, parsed, result)

        assert any("404" in f for f in result.failed)
    finally:
        set_json_mode(False)


def test_probe_webhook_health_orphan_emits_warning(tmp_path):
    """Orphan hook → non-blocking warning."""
    from bay_cli.commands.validate import ValidationResult, _probe_webhook_health
    from bay_cli.console.output import set_json_mode

    set_json_mode(True)
    try:
        root = _make_consumer_root(tmp_path, webhook_domain="webhook.example.com", github_token="tok")
        parsed = {
            "group_vars/all/secrets.yml": {"github_admin_token": "tok"},
            "group_vars/all/main.yml": {
                "stack_name": "teststack",
                "webhook_domain": "webhook.example.com",
            },
        }
        # Service "myapp" exists; hook for "old-deleted" is an orphan
        services_data = _make_services_data("https://github.com/Org/repo.git", "myapp")

        hooks = [
            {"id": 1, "config": {"url": "https://webhook.example.com/webhook/myapp"}, "last_response": {"code": 200}},
            {"id": 2, "config": {"url": "https://webhook.example.com/webhook/old-deleted"}, "last_response": {"code": 200}},
        ]

        with patch("bay_cli.github_webhook.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = hooks
            mock_resp.raise_for_status = MagicMock()
            mock_req.get.return_value = mock_resp

            result = ValidationResult()
            _probe_webhook_health(root, "production", services_data, parsed, result)

        assert result.total_issues == 0  # orphan is non-blocking
        assert any("orphan" in w for w in result.warnings)
    finally:
        set_json_mode(False)


def test_probe_webhook_health_null_last_response_is_note(tmp_path):
    """last_response = null → non-blocking note (warn, not fail)."""
    from bay_cli.commands.validate import ValidationResult, _probe_webhook_health
    from bay_cli.console.output import set_json_mode

    set_json_mode(True)
    try:
        root = _make_consumer_root(tmp_path, webhook_domain="webhook.example.com", github_token="tok")
        parsed = {
            "group_vars/all/secrets.yml": {"github_admin_token": "tok"},
            "group_vars/all/main.yml": {
                "stack_name": "teststack",
                "webhook_domain": "webhook.example.com",
            },
        }
        services_data = _make_services_data("https://github.com/Org/repo.git")

        hooks = [{"id": 1, "config": {"url": "https://webhook.example.com/webhook/myapp"}, "last_response": None}]

        with patch("bay_cli.github_webhook.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = hooks
            mock_resp.raise_for_status = MagicMock()
            mock_req.get.return_value = mock_resp

            result = ValidationResult()
            _probe_webhook_health(root, "production", services_data, parsed, result)

        assert result.total_issues == 0  # note is non-blocking
        assert any("never received a delivery" in w for w in result.warnings)
    finally:
        set_json_mode(False)


# ── prune-webhooks dry-run ────────────────────────────────────────────────


def _make_consumer_with_service(tmp_path: Path, webhook_domain: str) -> Path:
    """Write minimal consumer dirs for CLI tests."""
    gv_all = tmp_path / "group_vars" / "all"
    gv_all.mkdir(parents=True)
    (gv_all / "main.yml").write_text(
        f"---\nstack_name: teststack\nwebhook_domain: {webhook_domain}\ndomain_base: example.com\n"
    )
    (gv_all / "services.yml").write_text(
        "---\nservices:\n  myapp:\n    image: registry.example.com/stack/myapp:latest\n    access: vpn\n"
    )
    # Plain (non-encrypted) secrets for test
    (gv_all / "secrets.yml").write_text(
        "---\ngithub_admin_token: test-token-not-real\n"
    )
    inv = tmp_path / "hosts" / "production"
    inv.parent.mkdir(parents=True)
    inv.write_text("[production]\n1.2.3.4\n")
    return tmp_path


def test_prune_webhooks_dry_run_lists_without_deleting(tmp_path, monkeypatch):
    """prune-webhooks --dry-run lists orphans without making DELETE calls."""
    from typer.testing import CliRunner

    from bay_cli.cli import app as main_app

    # Patch the consumer root discovery so CLI finds our tmp_path
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _make_consumer_with_service(tmp_path, webhook_domain="webhook.example.com")

    orphan_hooks = [
        {"id": 99, "config": {"url": "https://webhook.example.com/webhook/old-service"}, "last_response": {"code": 200}, "created_at": "2025-01-01T00:00:00Z"},
    ]

    deleted_calls = []

    def mock_get(url, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = orphan_hooks
        resp.raise_for_status = MagicMock()
        return resp

    def mock_delete(url, headers=None, timeout=None):
        deleted_calls.append(url)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    # Patch find_bay_dir so CLI doesn't fail on missing .bay/
    import bay_cli.paths as bay_paths
    import bay_cli.github_webhook as gw_mod
    import requests as req_mod

    monkeypatch.setattr(bay_paths, "find_bay_dir", lambda: root / ".bay")
    monkeypatch.setattr(bay_paths, "consumer_root", lambda d: root)
    monkeypatch.setattr(req_mod, "get", mock_get)
    monkeypatch.setattr(req_mod, "delete", mock_delete)

    # Also patch _read_vault_token to return our fake token
    import bay_cli.commands.service as svc_mod
    monkeypatch.setattr(svc_mod, "_read_vault_token", lambda root, env, key: "test-token-not-real" if key == "github_admin_token" else None)

    runner = CliRunner()
    result = runner.invoke(main_app, ["service", "prune-webhooks", "Org/repo", "--dry-run"])

    # Should NOT have called DELETE
    assert deleted_calls == [], f"DELETE was called during dry-run: {deleted_calls}"
    # Should have exited cleanly
    assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
