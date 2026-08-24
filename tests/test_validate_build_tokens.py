"""Tests for _validate_build_tokens and _probe_token_scope.

All network calls are mocked — no real GitHub API calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# Suppress Rich console output during tests
from bay_cli.console.output import set_json_mode

from bay_cli.commands.validate import (
    ValidationResult,
    _probe_token_scope,
    _validate_build_tokens,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


def _result() -> ValidationResult:
    """Return a fresh ValidationResult with JSON mode active (suppresses Rich)."""
    set_json_mode(True)
    return ValidationResult()


def _svc(token: str = '{{ secrets.GOOD_KEY }}', strategy: str = "local") -> dict:
    """Return a minimal service dict with build.token set."""
    return {
        "build": {
            "repo": "git@github.com:TestOrg/myapp.git",
            "branch": "main",
            "token": token,
            "strategy": strategy,
        },
        "domains": ["myapp.example.com"],
        "ports": {"internal": 3000},
    }


def _remote_svc(
    token: str = '{{ secrets.GOOD_KEY }}',
    repo: str = "git@github.com:TestOrg/myapp.git",
) -> dict:
    """Return a remote-strategy service dict."""
    return {
        "build": {
            "repo": repo,
            "branch": "main",
            "token": token,
            "strategy": "remote",
        },
        "image": "registry.example.com/test:latest",
        "domains": ["myapp.example.com"],
        "ports": {"internal": 3000},
    }


# ── _validate_build_tokens: S1 unit tests ────────────────────────────────


class TestValidateBuildTokens:
    """Unit tests for _validate_build_tokens vault presence check."""

    def test_missing_vault_key_emits_error(self):
        """Service with build.token referencing absent vault key → not found error."""
        r = _result()
        services = {"myapp": _svc(token='{{ secrets.MISSING_KEY }}')}
        vault = {"OTHER_KEY": "some-value"}
        _validate_build_tokens(services, {}, vault, "group_vars/production/secrets.yml", r)
        assert r.failed, "Expected at least one failure"
        assert any("not found in vault" in msg for msg in r.failed)

    def test_empty_vault_value_emits_error(self):
        """Service with build.token referencing empty vault value → value is empty error."""
        r = _result()
        services = {"myapp": _svc(token='{{ secrets.EMPTY_KEY }}')}
        vault = {"EMPTY_KEY": ""}
        _validate_build_tokens(services, {}, vault, "group_vars/production/secrets.yml", r)
        assert r.failed, "Expected at least one failure"
        assert any("value is empty" in msg for msg in r.failed)

    def test_none_vault_value_emits_error(self):
        """Service with build.token referencing None vault value → value is empty error."""
        r = _result()
        services = {"myapp": _svc(token='{{ secrets.NULL_KEY }}')}
        vault = {"NULL_KEY": None}
        _validate_build_tokens(services, {}, vault, "group_vars/production/secrets.yml", r)
        assert r.failed, "Expected at least one failure"
        assert any("value is empty" in msg for msg in r.failed)

    def test_unrecognised_token_format_emits_error(self):
        """Service with build.token in non-Jinja form → unrecognised format error."""
        r = _result()
        services = {"myapp": _svc(token="plaintext-token-value")}
        vault = {"SOME_KEY": "fake-vault-val"}
        _validate_build_tokens(services, {}, vault, "group_vars/production/secrets.yml", r)
        assert r.failed, "Expected at least one failure"
        assert any("unrecognised format" in msg for msg in r.failed)

    def test_happy_path_no_errors(self):
        """Service with valid build.token referencing existing non-empty vault key → no errors."""
        r = _result()
        services = {"myapp": _svc(token='{{ secrets.GOOD_KEY }}')}
        vault = {"GOOD_KEY": "fake-vault-value-abc123"}
        _validate_build_tokens(services, {}, vault, "group_vars/production/secrets.yml", r)
        assert not r.failed, f"Unexpected failures: {r.failed}"

    def test_no_build_token_no_errors(self):
        """Service with no build.token field → no errors (no false positive)."""
        r = _result()
        services = {
            "myapp": {
                "build": {"repo": "git@github.com:Org/repo.git"},
                "domains": ["myapp.example.com"],
            }
        }
        vault = {}
        _validate_build_tokens(services, {}, vault, "secrets.yml", r)
        assert not r.failed, f"Unexpected failures: {r.failed}"

    def test_no_services_no_errors(self):
        """Empty services and accessories → ok check, no errors."""
        r = _result()
        _validate_build_tokens({}, {}, {}, "secrets.yml", r)
        assert not r.failed

    def test_multiple_services_mixed_one_error(self):
        """One missing key, one valid key → exactly one error."""
        r = _result()
        services = {
            "good": _svc(token='{{ secrets.GOOD_KEY }}'),
            "bad": _svc(token='{{ secrets.MISSING_KEY }}'),
        }
        vault = {"GOOD_KEY": "fake-vault-good-value"}
        _validate_build_tokens(services, {}, vault, "secrets.yml", r)
        assert len(r.failed) == 1
        assert "MISSING_KEY" in r.failed[0]

    def test_accessories_are_iterated(self):
        """Accessory with build.token referencing absent vault key → error emitted."""
        r = _result()
        accessories = {"my-accessory": _svc(token='{{ secrets.MISSING_KEY }}')}
        vault = {}
        _validate_build_tokens({}, accessories, vault, "secrets.yml", r)
        assert r.failed, "Expected failure for accessory with missing vault key"
        assert any("not found in vault" in msg for msg in r.failed)

    def test_vault_none_emits_warn_not_error(self):
        """vault_data=None → warn containing 'vault not decryptable', no errors."""
        r = _result()
        services = {"myapp": _svc(token='{{ secrets.SOME_KEY }}')}
        _validate_build_tokens(services, {}, None, "secrets.yml", r)
        assert not r.failed, f"Unexpected errors on vault_data=None: {r.failed}"
        assert r.warnings, "Expected a warning"
        assert any("vault not decryptable" in w for w in r.warnings)

    def test_vault_none_does_not_raise(self):
        """vault_data=None path must not raise any exception."""
        r = _result()
        # Should not raise
        _validate_build_tokens(
            {"svc": _svc()}, {}, None, "secrets.yml", r
        )

    def test_error_references_vault_key_name_not_value(self):
        """Error message references the vault key name, not the token value."""
        r = _result()
        services = {"myapp": _svc(token='{{ secrets.MY_SECRET_KEY }}')}
        vault = {}  # key absent
        _validate_build_tokens(services, {}, vault, "secrets.yml", r)
        assert r.failed
        # Key name must appear
        assert any("MY_SECRET_KEY" in msg for msg in r.failed)


# ── _probe_token_scope: S2 unit tests ────────────────────────────────────


class TestProbeTokenScope:
    """Unit tests for _probe_token_scope GitHub API probe (all mocked)."""

    def _vault(self) -> dict:
        return {"TESTORG_GIT_TOKEN": "fake-resolved-token-value"}

    def _services(self, strategy: str = "remote", token: str = '{{ secrets.TESTORG_GIT_TOKEN }}', repo: str = "git@github.com:TestOrg/myapp.git") -> dict:
        return {
            "myapp": _remote_svc(token=token, repo=repo) if strategy == "remote" else _svc(token=token, strategy=strategy),
        }

    def test_http_200_no_errors(self):
        """HTTP 200 from GitHub API → no errors emitted."""
        r = _result()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("bay_cli.commands.validate.requests.get", return_value=mock_resp):
            _probe_token_scope(self._services(), self._vault(), r)
        assert not r.failed, f"Unexpected errors: {r.failed}"

    def test_http_403_emits_lacks_read_access_error(self):
        """HTTP 403 → error contains 'lacks read access' and 'Contents: Read'."""
        r = _result()
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        with patch("bay_cli.commands.validate.requests.get", return_value=mock_resp):
            _probe_token_scope(self._services(), self._vault(), r)
        assert r.failed, "Expected failure on HTTP 403"
        msg = r.failed[0]
        assert "lacks read access" in msg
        assert "Contents: Read" in msg

    def test_http_403_does_not_include_token_value(self):
        """HTTP 403 error message must NOT contain the resolved token value."""
        r = _result()
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        with patch("bay_cli.commands.validate.requests.get", return_value=mock_resp):
            _probe_token_scope(self._services(), self._vault(), r)
        assert r.failed
        # Vault KEY name should appear, not the resolved ghp_ value
        assert "TESTORG_GIT_TOKEN" in r.failed[0]
        assert "fake-resolved-token-value" not in r.failed[0]

    def test_http_404_emits_cannot_see_error(self):
        """HTTP 404 → error contains 'cannot see' and vault key name."""
        r = _result()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("bay_cli.commands.validate.requests.get", return_value=mock_resp):
            _probe_token_scope(self._services(), self._vault(), r)
        assert r.failed, "Expected failure on HTTP 404"
        msg = r.failed[0]
        assert "cannot see" in msg
        assert "TESTORG_GIT_TOKEN" in msg

    def test_http_other_emits_generic_error(self):
        """HTTP 500 → error contains the HTTP status code."""
        r = _result()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("bay_cli.commands.validate.requests.get", return_value=mock_resp):
            _probe_token_scope(self._services(), self._vault(), r)
        assert r.failed
        assert "500" in r.failed[0]

    def test_network_error_emits_warn_not_error(self):
        """requests.RequestException → warn containing 'unreachable', no raised exception."""
        import requests as req_mod
        r = _result()
        with patch(
            "bay_cli.commands.validate.requests.get",
            side_effect=req_mod.exceptions.ConnectionError("Connection refused"),
        ):
            _probe_token_scope(self._services(), self._vault(), r)
        assert not r.failed, f"Unexpected errors on network failure: {r.failed}"
        assert r.warnings
        assert any("unreachable" in w for w in r.warnings)

    def test_local_strategy_service_is_not_probed(self):
        """Service with strategy=local is skipped, even if build.token is set."""
        r = _result()
        services = {
            "localapp": _svc(token='{{ secrets.TESTORG_GIT_TOKEN }}', strategy="local"),
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        with patch("bay_cli.commands.validate.requests.get", return_value=mock_resp) as mock_get:
            _probe_token_scope(services, self._vault(), r)
        mock_get.assert_not_called()
        assert not r.failed, "Local-strategy service should not be probed"

    def test_no_remote_services_emits_ok(self):
        """No remote services with build.token → ok check, no errors."""
        r = _result()
        services = {
            "webapp": {
                "image": "registry.example.com/webapp:latest",
                "domains": ["webapp.example.com"],
            }
        }
        _probe_token_scope(services, self._vault(), r)
        assert not r.failed

    def test_vault_none_emits_warn_and_returns(self):
        """vault_data=None → warn, no probe, no errors."""
        r = _result()
        with patch("bay_cli.commands.validate.requests.get") as mock_get:
            _probe_token_scope(self._services(), None, r)
        mock_get.assert_not_called()
        assert not r.failed
        assert r.warnings

    def test_https_github_url_parsed_correctly(self):
        """HTTPS github.com URL is parsed to owner/repo correctly."""
        r = _result()
        services = {
            "myapp": _remote_svc(
                token='{{ secrets.TESTORG_GIT_TOKEN }}',
                repo="https://github.com/TestOrg/myapp.git",
            )
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("bay_cli.commands.validate.requests.get", return_value=mock_resp) as mock_get:
            _probe_token_scope(services, self._vault(), r)
        assert mock_get.called
        called_url = mock_get.call_args[0][0]
        assert "TestOrg/myapp" in called_url

    def test_git_at_github_url_parsed_correctly(self):
        """git@ URL is parsed to owner/repo correctly."""
        r = _result()
        services = {
            "myapp": _remote_svc(
                token='{{ secrets.TESTORG_GIT_TOKEN }}',
                repo="git@github.com:TestOrg/myapp.git",
            )
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("bay_cli.commands.validate.requests.get", return_value=mock_resp) as mock_get:
            _probe_token_scope(services, self._vault(), r)
        assert mock_get.called
        called_url = mock_get.call_args[0][0]
        assert "TestOrg/myapp" in called_url
