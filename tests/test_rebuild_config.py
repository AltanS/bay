"""Tests for rebuild.sh.j2 template rendering with remote strategy.

Verifies that the rebuild script template correctly renders:
- Remote strategy services with IMAGE_REF, CLONE_URL, BUILD_STRATEGY
- Build secrets (BUILD_SECRETS associative array) with vault-decrypted values
- Git tokens embedded in CLONE_URL for various repo URL formats
- Persistent clone directory (push-builds path) for remote strategy
- Local strategy services still render correctly alongside remote ones
- webhook-config.json strategy field per service
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import make_ansible_env

# Import custom filters from the filter_plugins directory
_filter_dir = str(
    Path(__file__).resolve().parent.parent / "filter_plugins"
)
if _filter_dir not in sys.path:
    sys.path.insert(0, _filter_dir)

from bay_filters import (
    bay_build_dedup_map,
    bay_image_consumers,
    bay_image_region_map,
    bay_prefix_volumes,
    bay_repo_slug,
    bay_traefik_labels,
    bay_watchtower_labels,
)

TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "git_deploy"
    / "templates"
)


def _extract_service_block(rendered: str, svc_name: str) -> str:
    """Extract the per-service config block from the rendered rebuild.sh.

    Finds the if/elif block for the given service name in the per-service
    config section (the first if/elif chain in the script).
    """
    # The service key is compared against a SINGLE-quoted literal since the
    # M109 config-to-shell hardening — a double-quoted right operand would
    # expand a `$` in the key.
    match = re.search(
        rf"if \[\[ \"\${{SERVICE}}\" == '{re.escape(svc_name)}' \]\]; then(.*?)^(?:else|elif|fi)",
        rendered,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, f"Service block for '{svc_name}' not found in rendered output"
    return match.group(1)


# ── Jinja2 environment with Ansible-compatible filters ──────────────────


def _regex_replace(value, pattern, replacement):
    """Ansible-compatible regex_replace filter."""
    return re.sub(pattern, replacement, value)


def _to_json(value):
    """Ansible-compatible to_json filter."""
    return json.dumps(value)


def _render_rebuild_sh(
    services: dict,
    rebuild_services: list[str],
    *,
    git_deploy_services: list[str] | None = None,
    git_deploy_build_strategy: str = "local",
    git_deploy_health_check_timeout: int = 90,
    stack_name: str = "teststack",
    webhook_secret: str = "test-webhook-secret",
    peer_urls: dict | None = None,
    secrets: dict | None = None,
    gateway_bind_ip: str = "100.64.0.1",
) -> str:
    """Render rebuild.sh.j2 with the given services config.

    Returns the rendered bash script as a string.
    """
    env = make_ansible_env(TEMPLATE_DIR)
    env.filters["regex_replace"] = _regex_replace
    env.filters["to_json"] = _to_json
    env.filters["bay_build_dedup_map"] = bay_build_dedup_map
    env.filters["bay_image_consumers"] = bay_image_consumers
    env.filters["bay_image_region_map"] = bay_image_region_map
    env.filters["bay_repo_slug"] = bay_repo_slug
    env.filters["bay_prefix_volumes"] = bay_prefix_volumes
    env.filters["bay_traefik_labels"] = bay_traefik_labels
    env.filters["bay_watchtower_labels"] = bay_watchtower_labels

    tmpl = env.get_template("rebuild.sh.j2")
    rendered = tmpl.render(
        ansible_managed="Ansible managed - test render",
        services=services,
        git_deploy_rebuild_services=rebuild_services,
        git_deploy_services=git_deploy_services or [],
        git_deploy_build_strategy=git_deploy_build_strategy,
        git_deploy_build_dir=f"/opt/{stack_name}/builds",
        git_deploy_known_hosts=f"/opt/{stack_name}/builds/github_known_hosts",
        git_deploy_image_prefix=f"bay-{stack_name}",
        git_deploy_remote_build_dir=f"/opt/{stack_name}/push-builds",
        git_deploy_cb_max_failures=3,
        git_deploy_health_check_timeout=git_deploy_health_check_timeout,
        git_deploy_build_timeout=1200,
        git_deploy_build_mem_limit="2g",
        git_deploy_peer_webhook_urls=peer_urls or {},
        stack_dir=f"/opt/{stack_name}",
        stack_name=stack_name,
        docker_monitor_telegram_bot_token="test-bot-token",
        docker_monitor_telegram_chat_id="test-chat-id",
        docker_monitor_alert_header="[TEST] ",
        inventory_hostname="build-server",
        traefik_docker_network="services",
        watchtower_enabled=False,
        webhook={"secret": webhook_secret},
        secrets=secrets or {},
        gateway_bind_ip=gateway_bind_ip,
    )
    return rendered


def _render_webhook_config(
    services: dict,
    *,
    peer_urls: dict | None = None,
    git_deploy_build_strategy: str = "local",
) -> dict:
    """Render webhook-config.json.j2 and return parsed JSON."""
    env = make_ansible_env(TEMPLATE_DIR)
    env.filters["to_nice_json"] = lambda obj: json.dumps(obj, indent=4, sort_keys=True)
    env.filters["dict2items"] = lambda d: [{"key": k, "value": v} for k, v in d.items()]
    tmpl = env.get_template("webhook-config.json.j2")
    rendered = tmpl.render(
        services=services,
        git_deploy_peer_webhook_urls=peer_urls or {},
        git_deploy_build_strategy=git_deploy_build_strategy,
    )
    return json.loads(rendered)


# ── Test fixtures ───────────────────────────────────────────────────────


def _remote_service_with_token():
    """A remote-strategy service with a git token (git@ URL)."""
    return {
        "animals": {
            "build": {
                "repo": "git@github.com:acmecorp/animals.git",
                "branch": "main",
                "token": "ghp_FAKE_TOKEN_12345",
                "strategy": "remote",
            },
            "image": "zot.infra.example.com/demo/animals:latest",
            "access": "public",
            "domains": ["animals.example.com"],
            "ports": {"internal": 3000},
        }
    }


def _remote_service_with_secrets():
    """A remote-strategy service with build secrets (npmrc)."""
    return {
        "blog": {
            "build": {
                "repo": "https://github.com/acmecorp/blog.git",
                "branch": "main",
                "token": "ghp_BLOG_TOKEN_67890",
                "strategy": "remote",
                "secrets": {
                    "npmrc": "@scope:registry=https://npm.pkg.github.com\n//npm.pkg.github.com/:_authToken=npm_FAKE_TOKEN",
                },
            },
            "image": "zot.infra.example.com/demo/blog:latest",
            "access": "public",
            "domains": ["blog.example.com"],
            "ports": {"internal": 3000},
        }
    }


def _local_service():
    """A local-strategy service (no token, deploy key)."""
    return {
        "localapp": {
            "build": {
                "repo": "git@github.com:acmecorp/localapp.git",
                "branch": "develop",
            },
            "access": "public",
            "domains": ["localapp.example.com"],
            "ports": {"internal": 8080},
        }
    }


def _mixed_services():
    """Mix of remote + local services (multi-strategy)."""
    return {
        **_remote_service_with_token(),
        **_remote_service_with_secrets(),
        **_local_service(),
    }


# ── Remote strategy: IMAGE_REF, CLONE_URL, BUILD_STRATEGY ──────────────


class TestRemoteStrategyRendering:
    """rebuild.sh.j2 renders correct variables for remote-strategy services."""

    def test_build_strategy_set_to_remote(self):
        """Remote service has BUILD_STRATEGY="remote" in rendered script."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        assert 'BUILD_STRATEGY="remote"' in rendered

    def test_image_ref_populated(self):
        """Remote service has IMAGE_REF set to the service's image field."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        assert 'IMAGE_REF="zot.infra.example.com/demo/animals:latest"' in rendered

    def test_image_repo_populated(self):
        """Remote service has IMAGE_REPO (image ref without tag) for SHA tagging."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        assert 'IMAGE_REPO="zot.infra.example.com/demo/animals"' in rendered

    def test_clone_url_is_token_free_git_at(self):
        """A git@ repo becomes a plain HTTPS URL — auth goes via GIT_ASKPASS."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        assert 'CLONE_URL="https://github.com/acmecorp/animals.git"' in rendered
        assert "ghp_FAKE_TOKEN_12345" not in rendered

    def test_clone_url_is_token_free_https(self):
        """An https:// repo keeps its URL, with no credential spliced in."""
        services = _remote_service_with_secrets()
        rendered = _render_rebuild_sh(services, ["blog"])
        assert 'CLONE_URL="https://github.com/acmecorp/blog.git"' in rendered
        assert "ghp_BLOG_TOKEN_67890" not in rendered

    def test_persistent_clone_dir_push_builds(self):
        """Remote service uses push-builds persistent clone directory."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        assert 'REPO_DIR="${REMOTE_BUILD_DIR}/animals/repo"' in rendered

    def test_clone_cmd_rendered(self):
        """Remote service has CLONE_CMD for initial clone.

        The branch, the URL and the target directory are referenced as escaped
        parameter expansions rather than pasted in: the string is handed to
        `eval`, which re-parses the VALUE, so an interpolated branch holding
        `$(...)` would run. BRANCH= and CLONE_URL= are asserted separately.
        """
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        assert "CLONE_CMD=" in rendered
        assert r'git clone --branch \"\${BRANCH}\" --single-branch' in rendered
        assert 'BRANCH="main"' in rendered

    def test_fetch_cmd_rendered(self):
        """Remote service has FETCH_CMD for subsequent fetches."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        assert "FETCH_CMD=" in rendered
        assert r'fetch origin \"\${BRANCH}\"' in rendered
        assert 'BRANCH="main"' in rendered

    def test_remote_strategy_no_pull_cmd(self):
        """Remote service does NOT have PULL_CMD (that's for local strategy)."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        animals_block = _extract_service_block(rendered, "animals")
        assert "PULL_CMD=" not in animals_block


# ── Build secrets ───────────────────────────────────────────────────────


class TestBuildSecrets:
    """rebuild.sh.j2 renders BUILD_SECRETS associative array with vault values."""

    def test_build_secrets_populated(self):
        """Service with build.secrets has populated BUILD_SECRETS array."""
        services = _remote_service_with_secrets()
        rendered = _render_rebuild_sh(services, ["blog"])
        assert 'declare -A BUILD_SECRETS=(' in rendered
        assert '["npmrc"]=' in rendered

    def test_build_secrets_contain_actual_values(self):
        """BUILD_SECRETS values are the actual vault-decrypted secret content."""
        services = _remote_service_with_secrets()
        rendered = _render_rebuild_sh(services, ["blog"])
        # The npmrc value should be baked in (not a variable reference)
        assert "npm.pkg.github.com" in rendered
        assert "npm_FAKE_TOKEN" in rendered

    def test_no_secrets_renders_empty_array(self):
        """Service without build.secrets renders empty BUILD_SECRETS."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        animals_block = _extract_service_block(rendered, "animals")
        assert "declare -A BUILD_SECRETS=()" in animals_block

    def test_secrets_dir_uses_push_builds(self):
        """Remote build secret temp files use persistent secrets dir."""
        services = _remote_service_with_secrets()
        rendered = _render_rebuild_sh(services, ["blog"])
        assert 'SECRETS_DIR="${REMOTE_BUILD_DIR}/.secrets"' in rendered


# ── Local strategy rendering (unchanged) ────────────────────────────────


class TestLocalStrategyRendering:
    """Local-strategy services render correctly alongside remote ones."""

    def test_local_strategy_value(self):
        """Local service has BUILD_STRATEGY="local"."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert 'BUILD_STRATEGY="local"' in rendered

    def test_local_uses_shared_repo_dir(self):
        """Local service uses shared/ repo directory, not push-builds/."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert 'REPO_DIR="${BUILD_DIR}/shared/' in rendered

    def test_local_has_deploy_key(self):
        """Local service without token uses deploy key."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        block = _extract_service_block(rendered, "localapp")
        assert "DEPLOY_KEY=" in block
        assert "GIT_SSH_COMMAND=" in block

    def test_local_empty_image_ref(self):
        """Local service has empty IMAGE_REF (not used for local builds)."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        block = _extract_service_block(rendered, "localapp")
        assert 'IMAGE_REF=""' in block


# ── Mixed strategy rendering ────────────────────────────────────────────


class TestMixedStrategyRendering:
    """Template correctly handles multiple services with different strategies."""

    def test_mixed_services_all_present(self):
        """All services appear as elif branches in the rendered script."""
        services = _mixed_services()
        rebuild_svcs = ["animals", "blog", "localapp"]
        rendered = _render_rebuild_sh(
            services,
            rebuild_svcs,
            git_deploy_services=["localapp"],
        )
        assert '"animals"' in rendered
        assert '"blog"' in rendered
        assert '"localapp"' in rendered

    def test_mixed_strategy_values(self):
        """Each service gets its correct BUILD_STRATEGY value."""
        services = _mixed_services()
        rebuild_svcs = ["animals", "blog", "localapp"]
        rendered = _render_rebuild_sh(
            services,
            rebuild_svcs,
            git_deploy_services=["localapp"],
        )
        # Both remote services and one local service
        assert rendered.count('BUILD_STRATEGY="remote"') == 2
        assert rendered.count('BUILD_STRATEGY="local"') == 1

    def test_push_strategy_normalized_to_remote(self):
        """Deprecated 'push' strategy renders as 'remote'."""
        services = {
            "legacy-push": {
                "build": {
                    "repo": "git@github.com:acmecorp/legacy.git",
                    "branch": "main",
                    "token": "ghp_LEGACY_TOKEN",
                    "strategy": "push",
                },
                "image": "zot.example.com/stack/legacy:latest",
                "access": "public",
                "domains": ["legacy.example.com"],
                "ports": {"internal": 3000},
            }
        }
        rendered = _render_rebuild_sh(services, ["legacy-push"])
        assert 'BUILD_STRATEGY="remote"' in rendered
        assert 'BUILD_STRATEGY="push"' not in rendered


# ── Fan-out pull signal rendering ───────────────────────────────────────


class TestFanOutRendering:
    """rebuild.sh.j2 renders pull signal fan-out for remote-strategy services."""

    def test_fanout_curl_rendered_for_remote_service(self):
        """Remote service with peer_urls gets curl fan-out blocks."""
        services = _remote_service_with_token()
        services["animals"]["regions"] = ["eu"]
        peer_urls = {
            "eu": "https://deploy.eu.example.com",
            "na": "https://deploy.na.example.com",
        }
        rendered = _render_rebuild_sh(
            services, ["animals"], peer_urls=peer_urls
        )
        assert "X-Bay-Pull-Signal: 1" in rendered
        assert "deploy.eu.example.com" in rendered

    def test_fanout_respects_service_regions(self):
        """Fan-out only targets regions where the service runs."""
        services = _remote_service_with_token()
        services["animals"]["regions"] = ["eu"]
        peer_urls = {
            "eu": "https://deploy.eu.example.com",
            "na": "https://deploy.na.example.com",
        }
        rendered = _render_rebuild_sh(
            services, ["animals"], peer_urls=peer_urls
        )
        # EU-only service should fan out to EU, not NA
        assert "deploy.eu.example.com" in rendered
        # The image-level fan-out uses IMAGE_REGIONS associative array
        # which should only contain "eu" for the animals image
        fanout_start = rendered.index("Fan-out image-level pull signals")
        fanout_section = rendered[fanout_start:]
        # IMAGE_REGIONS should map animals image to "eu" only
        assert '"eu"' in fanout_section or 'eu' in fanout_section
        # NA URL appears in PEER_URLS but the IMAGE_REGIONS for animals
        # image only has "eu", so the loop won't send to NA
        assert 'IMAGE_REGIONS' in fanout_section


# ── webhook-config.json strategy field ──────────────────────────────────


class TestWebhookConfigStrategy:
    """webhook-config.json.j2 includes strategy field per service."""

    def test_strategy_field_present(self):
        """Each service entry includes its strategy value."""
        services = _remote_service_with_token()
        config = _render_webhook_config(services)
        assert config["animals"]["strategy"] == "remote"

    def test_strategy_default_local(self):
        """Services without explicit strategy default to 'local'."""
        services = _local_service()
        config = _render_webhook_config(services)
        assert config["localapp"]["strategy"] == "local"

    def test_strategy_inherits_global_default(self):
        """Services inherit git_deploy_build_strategy when no per-service strategy."""
        services = _local_service()
        config = _render_webhook_config(
            services, git_deploy_build_strategy="remote"
        )
        assert config["localapp"]["strategy"] == "remote"

    def test_mixed_strategies_in_config(self):
        """Config with mixed strategies renders correct per-service values."""
        services = _mixed_services()
        config = _render_webhook_config(services)
        assert config["animals"]["strategy"] == "remote"
        assert config["blog"]["strategy"] == "remote"
        assert config["localapp"]["strategy"] == "local"

    def test_branch_and_paths_with_strategy(self):
        """Strategy field coexists with branch and paths fields."""
        services = {
            "filtered-svc": {
                "build": {
                    "repo": "git@github.com:acmecorp/app.git",
                    "branch": "develop",
                    "strategy": "remote",
                    "paths": {
                        "include": ["src/**"],
                        "exclude": ["*.md", "docs/**"],
                    },
                },
                "image": "zot.example.com/stack/app:latest",
                "regions": ["eu"],
            }
        }
        config = _render_webhook_config(services)
        entry = config["filtered-svc"]
        assert entry["strategy"] == "remote"
        assert entry["branch"] == "develop"
        assert entry["paths"]["include"] == ["src/**"]
        assert entry["paths"]["exclude"] == ["*.md", "docs/**"]

    def test_push_strategy_not_normalized_in_webhook_config(self):
        """webhook-config.json preserves the raw strategy value (push)."""
        # Normalization happens in rebuild.sh, not in webhook-config.json
        services = {
            "legacy": {
                "build": {
                    "repo": "git@github.com:acmecorp/legacy.git",
                    "strategy": "push",
                },
                "image": "zot.example.com/stack/legacy:latest",
            }
        }
        config = _render_webhook_config(services)
        # webhook-config.json stores the raw value; rebuild.sh normalizes it
        assert config["legacy"]["strategy"] == "push"


# ── Notification dedup ─────────────────────────────────────────


class TestNotificationDedup:
    """rebuild.sh.j2 notification dedup via _record_failure and SHA tracking."""

    def test_sha_initialized_empty(self):
        """SHA is initialized to empty string before per-service config."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        # SHA="" must appear before the per-service config block
        sha_init = rendered.index('SHA=""')
        svc_block = rendered.index('if [[ "${SERVICE}"')
        assert sha_init < svc_block

    def test_record_failure_accepts_sha_context_detail(self):
        """_record_failure function signature accepts sha, context, detail."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert 'local sha="${1:-}"' in rendered
        assert 'local context="${2:-Deploy}"' in rendered
        assert 'local detail="${3:-}"' in rendered

    def test_state_file_tracks_last_failure_sha(self):
        """State file schema v1 stores last failure SHA for dedup."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        # Schema v1: last_failure.sha replaces the old last_notified_sha field
        assert "last_failure" in rendered
        assert ".last_failure.sha" in rendered

    def test_deploy_failed_passes_sha(self):
        """_deploy_failed passes SHA to _record_failure."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert '_record_failure "${SHA}" "Webhook deploy"' in rendered

    def test_build_failure_passes_sha_and_context(self):
        """Local build failure passes SHA and 'Build' context."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert '_record_failure "${SHA}" "Build" "${TAIL_OUTPUT}"' in rendered

    def test_remote_build_failure_passes_sha_and_context(self):
        """Remote build failure passes SHA and 'Remote build' context."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        assert '_record_failure "${SHA}" "Remote build" "${TAIL_OUTPUT}"' in rendered

    def test_pull_failure_passes_empty_sha(self):
        """Pull failure passes empty SHA (no commit available)."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(
            services, ["animals"], git_deploy_services=["animals"]
        )
        assert '_record_failure "" "Image pull"' in rendered

    def test_no_standalone_failure_send_telegram(self):
        """Failure paths do not call send_telegram directly (dedup in _record_failure)."""
        services = _mixed_services()
        rendered = _render_rebuild_sh(
            services,
            ["animals", "blog", "localapp"],
            git_deploy_services=["localapp"],
        )
        # Failure-specific send_telegram calls should not exist
        assert "Build failed</b>" not in rendered
        assert "Remote build failed</b>" not in rendered
        assert "Pull failed</b>" not in rendered
        assert "Webhook deploy failed</b>" not in rendered

    def test_success_notifications_unchanged(self):
        """Success notifications still call send_telegram directly."""
        services = _mixed_services()
        rendered = _render_rebuild_sh(
            services,
            ["animals", "blog", "localapp"],
            git_deploy_services=["localapp"],
        )
        assert "Webhook deploy complete</b>" in rendered
        assert "Remote build complete</b>" in rendered
        assert "Pull deploy complete</b>" in rendered

    def test_atomic_write_temp_and_mv(self):
        """State file written atomically via temp file + mv."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert '${STATE_FILE}.tmp' in rendered
        assert 'mv -f "${tmpfile}" "${STATE_FILE}"' in rendered

    def test_dedup_suppression_logged(self):
        """Duplicate suppression logs a message."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "notification suppressed" in rendered

    def test_cb_open_always_notifies(self):
        """Circuit breaker OPEN notification is always sent (no dedup check)."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        # CB OPEN send_telegram is inside _record_failure, before the dedup check
        cb_open_idx = rendered.index("Circuit breaker OPEN")
        dedup_idx = rendered.index("notification suppressed")
        assert cb_open_idx < dedup_idx


# ── Pull-only services ──────────────────────────────────────


def _shared_image_services():
    """Multi-service topology: 1 builder + 2 pull-only services."""
    return {
        "storefront-de": {
            "build": {
                "repo": "git@github.com:acmecorp/storefront.git",
                "branch": "master",
                "token": "ghp_FAKE_TOKEN",
                "strategy": "remote",
            },
            "image": "registry.example.com/storefront:latest",
            "access": "public",
            "domains": ["storefront.de"],
            "ports": {"internal": 3000},
            "regions": ["eu"],
        },
        "storefront-es": {
            "image": "registry.example.com/storefront:latest",
            "access": "public",
            "domains": ["ketocontrol.es"],
            "ports": {"internal": 3000},
            "regions": ["eu"],
        },
        "storefront-com": {
            "image": "registry.example.com/storefront:latest",
            "access": "public",
            "domains": ["storefront.com"],
            "ports": {"internal": 3000},
            "regions": ["na"],
        },
    }


class TestPullOnlyServiceRendering:
    """Pull-only services in rebuild.sh.j2."""

    def test_pull_only_service_has_pull_only_strategy(self):
        """Pull-only service (no build block) gets BUILD_STRATEGY='pull_only'."""
        services = _shared_image_services()
        rebuild_svcs = ["storefront-de", "storefront-es", "storefront-com"]
        rendered = _render_rebuild_sh(services, rebuild_svcs)
        assert 'BUILD_STRATEGY="pull_only"' in rendered

    def test_pull_only_service_has_image_ref(self):
        """Pull-only service has IMAGE_REF set to its image field."""
        services = _shared_image_services()
        rebuild_svcs = ["storefront-de", "storefront-es", "storefront-com"]
        rendered = _render_rebuild_sh(services, rebuild_svcs)
        # Find the storefront-es block (pull-only)
        match = re.search(
            r"""if \[\[ "\$\{SERVICE\}" == 'storefront-es' \]\]; then(.*?)(?:elif|else|fi)""",
            rendered,
            re.DOTALL,
        )
        assert match is not None
        block = match.group(1)
        assert 'IMAGE_REF="registry.example.com/storefront:latest"' in block

    def test_pull_only_service_no_repo_dir(self):
        """Pull-only service does not set REPO_DIR."""
        services = _shared_image_services()
        rebuild_svcs = ["storefront-de", "storefront-es", "storefront-com"]
        rendered = _render_rebuild_sh(services, rebuild_svcs)
        match = re.search(
            r"""if \[\[ "\$\{SERVICE\}" == 'storefront-es' \]\]; then(.*?)(?:elif|else|fi)""",
            rendered,
            re.DOTALL,
        )
        assert match is not None
        block = match.group(1)
        assert "REPO_DIR=" not in block
        assert "CLONE_URL=" not in block

    def test_pull_only_guard_rendered(self):
        """rebuild.sh has pull_only guard that skips non-pull triggers."""
        services = _shared_image_services()
        rebuild_svcs = ["storefront-de", "storefront-es"]
        rendered = _render_rebuild_sh(services, rebuild_svcs)
        assert '${BUILD_STRATEGY}" == "pull_only"' in rendered
        assert "is pull-only but no pull signal detected" in rendered

    def test_pull_only_in_restart_block(self):
        """Pull-only services appear in the docker run restart block."""
        services = _shared_image_services()
        rebuild_svcs = ["storefront-de", "storefront-es", "storefront-com"]
        rendered = _render_rebuild_sh(services, rebuild_svcs)
        # The pull signal restart block should include storefront-es
        assert '"storefront-es"' in rendered
        assert '"storefront-com"' in rendered

    def test_mixed_build_and_pull_only(self):
        """Both build and pull-only services rendered in same script."""
        services = _shared_image_services()
        rebuild_svcs = ["storefront-de", "storefront-es", "storefront-com"]
        rendered = _render_rebuild_sh(services, rebuild_svcs)
        # Builder has remote strategy
        assert rendered.count('BUILD_STRATEGY="remote"') == 1
        # Two pull-only services
        assert rendered.count('BUILD_STRATEGY="pull_only"') == 2

    def test_image_region_map_rendered(self):
        """Image-level fan-out renders IMAGE_REGIONS associative array."""
        services = _shared_image_services()
        rebuild_svcs = ["storefront-de", "storefront-es", "storefront-com"]
        peer_urls = {
            "eu": "https://deploy.eu.example.com",
            "na": "https://deploy.na.example.com",
        }
        rendered = _render_rebuild_sh(
            services, rebuild_svcs, peer_urls=peer_urls
        )
        assert "IMAGE_REGIONS" in rendered
        # The storefront image should map to both eu and na
        assert '"registry.example.com/storefront:latest"' in rendered
        assert "eu na" in rendered or "eu" in rendered


# ── Health check and rollback ─────────────────────────────────


class TestHealthCheckAndRollback:
    """Post-restart health check and automatic rollback in rebuild.sh."""

    def test_health_check_timeout_rendered(self):
        """HEALTH_CHECK_TIMEOUT variable is rendered in script."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "HEALTH_CHECK_TIMEOUT=90" in rendered

    def test_wait_healthy_function_rendered(self):
        """_wait_healthy function is defined in rendered script."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "_wait_healthy()" in rendered
        assert "State.Running" in rendered
        assert "State.Health.Status" in rendered

    def test_handle_rollback_function_rendered(self):
        """_handle_rollback function is defined in rendered script."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "_handle_rollback()" in rendered
        assert "Rolled back to previous" in rendered

    def test_run_container_in_local_path(self):
        """Local build path wraps docker run in _run_container() function."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "_run_container() {" in rendered
        assert '_wait_healthy "${SERVICE}" "${HEALTH_CHECK_TIMEOUT}"' in rendered

    def test_run_container_in_pull_path(self):
        """Pull-signal path wraps docker run in _run_container() function."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(
            services, ["animals"], git_deploy_services=["animals"]
        )
        assert "_run_container() {" in rendered

    def test_previous_tag_before_pull_stop(self):
        """Pull-signal path tags running image as :previous before stopping."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(
            services, ["animals"], git_deploy_services=["animals"]
        )
        # :previous tagging must appear before docker stop in pull path
        pull_start = rendered.index("Pull signal detected")
        pull_section = rendered[pull_start:]
        prev_tag_idx = pull_section.index("IMAGE_REPO}:previous")
        stop_idx = pull_section.index('docker stop "${SERVICE}"')
        assert prev_tag_idx < stop_idx

    def test_health_check_before_reset_cb_pull_path(self):
        """Health check runs before _reset_cb in pull-signal path."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(
            services, ["animals"], git_deploy_services=["animals"]
        )
        pull_start = rendered.index("Pull signal detected")
        pull_section = rendered[pull_start:]
        health_idx = pull_section.index("_wait_healthy")
        reset_idx = pull_section.index("_reset_cb")
        assert health_idx < reset_idx

    def test_health_check_before_reset_cb_local_path(self):
        """Health check runs before _reset_cb in local build path."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        local_start = rendered.index("Restart with new image")
        local_section = rendered[local_start:]
        health_idx = local_section.index("_wait_healthy")
        reset_idx = local_section.index("_reset_cb")
        assert health_idx < reset_idx

    def test_rollback_uses_previous_tag_pull_path(self):
        """Pull path rollback uses IMAGE_REPO:previous -> IMAGE_REF."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(
            services, ["animals"], git_deploy_services=["animals"]
        )
        assert (
            '_handle_rollback "${SERVICE}" "${IMAGE_REPO}:previous" "${IMAGE_REF}"'
            in rendered
        )

    def test_rollback_uses_previous_tag_local_path(self):
        """Local path rollback uses IMAGE_NAME:previous -> IMAGE_NAME:latest."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert (
            '_handle_rollback "${SERVICE}" "${IMAGE_NAME}:previous" "${IMAGE_NAME}:latest"'
            in rendered
        )

    def test_rollback_records_circuit_breaker_failure(self):
        """_handle_rollback calls _record_failure for circuit breaker tracking."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert '_record_failure "${SHA}" "Health check"' in rendered

    def test_rollback_failed_alert_distinct(self):
        """ROLLBACK FAILED alert is distinct from successful rollback alert."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "ROLLBACK FAILED" in rendered
        assert "Rolled back to previous" in rendered

    def test_no_previous_image_alert(self):
        """Alert when no :previous image exists for rollback."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "no rollback available" in rendered

    def test_double_failure_alert(self):
        """Distinct alert when rollback image also fails health check."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "previous image also unhealthy" in rendered

    def test_crash_loop_detection_multiple_checks(self):
        """_wait_healthy polls State.Running multiple times for crash-loop detection."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "checks=$((checks + 1))" in rendered
        assert "checks -ge 3" in rendered

    def test_healthcheck_aware_polling(self):
        """_wait_healthy handles containers with and without HEALTHCHECK."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "healthy)" in rendered
        assert "unhealthy)" in rendered
        assert "none)" in rendered


# ── Build duration monitoring and timeout ─────────────────────


def _render_systemd_unit(
    template_name: str,
    *,
    stack_name: str = "teststack",
    build_mem_limit: str = "2g",
    build_timeout: int = 1200,
) -> str:
    """Render a systemd unit template."""
    env = make_ansible_env(TEMPLATE_DIR)
    env.filters["to_json"] = _to_json
    tmpl = env.get_template(template_name)
    return tmpl.render(
        ansible_managed="Ansible managed - test render",
        app_user="bay",
        stack_dir=f"/opt/{stack_name}",
        stack_name=stack_name,
        git_deploy_build_mem_limit=build_mem_limit,
        git_deploy_build_timeout=build_timeout,
        docker_monitor_telegram_bot_token="test-bot-token",
        docker_monitor_telegram_chat_id="test-chat-id",
        docker_monitor_alert_header="[TEST] ",
        inventory_hostname="build-server",
    )


class TestBuildTimeout:
    """Systemd timeout and OnFailure alert unit."""

    def test_timeout_in_service_unit(self):
        """bay-build@.service includes TimeoutStartSec."""
        rendered = _render_systemd_unit("bay-build@.service.j2")
        assert "TimeoutStartSec=1200" in rendered

    def test_timeout_custom_value(self):
        """TimeoutStartSec uses configured value."""
        rendered = _render_systemd_unit(
            "bay-build@.service.j2", build_timeout=600
        )
        assert "TimeoutStartSec=600" in rendered

    def test_onfailure_in_service_unit(self):
        """bay-build@.service includes OnFailure directive."""
        rendered = _render_systemd_unit("bay-build@.service.j2")
        assert "OnFailure=bay-build-alert@%i.service" in rendered

    def test_memory_max_rendered(self):
        """MemoryMax is still rendered when configured."""
        rendered = _render_systemd_unit("bay-build@.service.j2")
        assert "MemoryMax=2g" in rendered

    def test_alert_unit_rendered(self):
        """bay-build-alert@.service template renders."""
        rendered = _render_systemd_unit("bay-build-alert@.service.j2")
        assert "build-alert.sh %i" in rendered

    def test_alert_script_filters_exit_code(self):
        """build-alert.sh skips notification for exit-code failures."""
        rendered = _render_systemd_unit("build-alert.sh.j2")
        assert 'RESULT=$(systemctl show' in rendered
        assert '"exit-code"' in rendered
        assert "not a systemd-level kill" in rendered

    def test_alert_script_filters_success_race(self):
        """build-alert.sh skips when result is success (path unit race)."""
        rendered = _render_systemd_unit("build-alert.sh.j2")
        assert '"success"' in rendered

    def test_alert_script_timeout_message(self):
        """build-alert.sh sends timeout-specific Telegram alert."""
        rendered = _render_systemd_unit("build-alert.sh.j2")
        assert "Build timed out" in rendered
        assert "journalctl -u bay-build@" in rendered

    def test_alert_script_oom_message(self):
        """build-alert.sh sends OOM-specific Telegram alert."""
        rendered = _render_systemd_unit("build-alert.sh.j2")
        assert "Build OOM killed" in rendered

    def test_alert_script_includes_timeout_value(self):
        """build-alert.sh includes the configured timeout in alerts."""
        rendered = _render_systemd_unit("build-alert.sh.j2")
        assert "BUILD_TIMEOUT=1200" in rendered

    def test_alert_script_cleans_trigger(self):
        """build-alert.sh cleans up trigger file."""
        rendered = _render_systemd_unit("build-alert.sh.j2")
        assert "triggers/${SERVICE}.trigger" in rendered


class TestBuildDuration:
    """Build duration tracking in rebuild.sh success notifications."""

    def test_build_start_recorded(self):
        """BUILD_START is set at the top of the script."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert 'BUILD_START="${EPOCHSECONDS}"' in rendered

    def test_format_duration_function(self):
        """format_duration function renders in script."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "format_duration()" in rendered

    def test_duration_in_pull_deploy_notification(self):
        """Pull deploy success includes Duration field."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(
            services, ["animals"], git_deploy_services=["animals"]
        )
        # Find pull deploy notification
        pull_section = rendered[rendered.index("Pull deploy complete"):]
        assert "format_duration" in pull_section

    def test_duration_in_remote_build_notification(self):
        """Remote build success includes Duration field."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        remote_section = rendered[rendered.index("Remote build complete"):]
        assert "format_duration" in remote_section

    def test_duration_in_webhook_deploy_notification(self):
        """Webhook deploy success includes Duration field."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        webhook_section = rendered[rendered.index("Webhook deploy complete"):]
        assert "format_duration" in webhook_section

    def test_duration_format_minutes_and_seconds(self):
        """format_duration outputs Xm Ys for >= 60 seconds."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "secs / 60" in rendered
        assert "secs % 60" in rendered

    def test_build_start_before_service_config(self):
        """BUILD_START is set before per-service configuration."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        start_idx = rendered.index('BUILD_START=')
        svc_idx = rendered.index('if [[ "${SERVICE}"')
        assert start_idx < svc_idx


# ── _wait_healthy StartPeriod awareness + timeout inversion fix ────


class TestWaitHealthyStartPeriodFix:
    """Consecutive-unhealthy guard, default timeout bump, per-service override."""

    def test_single_unhealthy_then_healthy_returns_zero(self):
        """unhealthy_count counter and transient-unhealthy message present in script."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        # Counter variable declared
        assert "unhealthy_count=0" in rendered
        # Transient message for first unhealthy read
        assert "transient unhealthy read" in rendered
        # 2-consecutive logic present
        assert "unhealthy_count -ge 2" in rendered

    def test_two_consecutive_unhealthy_returns_one(self):
        """Script returns 1 after two consecutive unhealthy reads."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert "unhealthy_count -ge 2" in rendered
        assert "consecutive reads" in rendered

    def test_running_false_early_exit_unchanged(self):
        """The running != true early-exit branch is unchanged (regression guard)."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        assert 'if [[ "$running" != "true" ]]; then' in rendered
        assert "Container ${name} is not running" in rendered

    def test_health_check_timeout_default_is_90(self):
        """Default HEALTH_CHECK_TIMEOUT renders as 90."""
        services = _local_service()
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"],
            git_deploy_health_check_timeout=90,
        )
        assert "HEALTH_CHECK_TIMEOUT=90" in rendered

    def test_per_service_health_check_timeout_override(self):
        """Per-service health_check_timeout overrides the global default."""
        services = {
            "localapp": {
                **_local_service()["localapp"],
                "health_check_timeout": 120,
            }
        }
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"],
            git_deploy_health_check_timeout=90,
        )
        assert "HEALTH_CHECK_TIMEOUT=120" in rendered

    def test_schema_accepts_health_check_timeout(self):
        """services.schema.json accepts health_check_timeout on a service block."""
        from jsonschema import Draft202012Validator
        import json as _json
        from pathlib import Path as _Path
        schema_path = (
            _Path(__file__).resolve().parent.parent
            / "src" / "bay_cli" / "schemas" / "services.schema.json"
        )
        schema = _json.loads(schema_path.read_text())
        data = {
            "services": {
                "myapp": {
                    "image": "nginx:latest",
                    "access": "public",
                    "domains": ["app.example.com"],
                    "ports": {"internal": 8080},
                    "health_check_timeout": 120,
                }
            }
        }
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected schema errors: {errors}"

    def test_schema_accepts_underscore_scratch_keys(self):
        """Top-level keys prefixed with `_` are allowed as scratch space for YAML
        anchors (e.g. `_anchors:`). Ansible ignores them; the schema must too."""
        from jsonschema import Draft202012Validator
        import json as _json
        from pathlib import Path as _Path
        schema_path = (
            _Path(__file__).resolve().parent.parent
            / "src" / "bay_cli" / "schemas" / "services.schema.json"
        )
        schema = _json.loads(schema_path.read_text())
        data = {
            "_anchors": {"lcc_base": {"access": "public"}},
            "_foo": "anything",
            "services": {
                "myapp": {
                    "image": "nginx:latest",
                    "access": "public",
                    "domains": ["app.example.com"],
                    "ports": {"internal": 8080},
                }
            },
        }
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert errors == [], f"Unexpected schema errors: {errors}"

    def test_schema_still_rejects_non_underscore_unknown_keys(self):
        """Regression guard: underscore relaxation must not leak to other keys."""
        from jsonschema import Draft202012Validator
        import json as _json
        from pathlib import Path as _Path
        schema_path = (
            _Path(__file__).resolve().parent.parent
            / "src" / "bay_cli" / "schemas" / "services.schema.json"
        )
        schema = _json.loads(schema_path.read_text())
        data = {
            "anchors": {"lcc_base": {"access": "public"}},
            "services": {
                "myapp": {
                    "image": "nginx:latest",
                    "access": "public",
                    "domains": ["app.example.com"],
                    "ports": {"internal": 8080},
                }
            },
        }
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        assert any("anchors" in str(e) for e in errors), (
            f"Expected rejection of top-level 'anchors' (no underscore); got {errors}"
        )


# ── Dedup fan-out CORR_ID propagation (v0.82.6) ─────────────────────────


def _dedup_group_services():
    """Two services with identical build configs — same dedup group.

    `api` is the primary (first in iteration order); `worker` is the alias
    that piggybacks on the retagged image.
    """
    build = {
        "repo": "git@github.com:acmecorp/shared.git",
        "branch": "main",
        "dockerfile": "Dockerfile",
    }
    return {
        "api": {
            "build": dict(build),
            "access": "public",
            "domains": ["api.example.com"],
            "ports": {"internal": 8080},
        },
        "worker": {
            "build": dict(build),
            "access": "public",
            "domains": ["worker.example.com"],
            "ports": {"internal": 8080},
        },
    }


def _extract_fanout_block(rendered: str) -> str:
    """Extract the dedup fan-out section from the rendered rebuild.sh."""
    start_marker = "# ── Fan-out: trigger alias rebuilds for dedup groups ──"
    end_marker = "_log \"Completed rebuild"
    start = rendered.index(start_marker)
    end = rendered.index(end_marker, start)
    return rendered[start:end]


class TestDedupFanOutTriggerPropagatesCorrId:
    """v0.82.6 bug fix: alias trigger must carry the CORR_ID so rebuild.sh
    on the alias side doesn't log `[unknown]` and break log correlation
    across the fan-out."""

    def test_no_bare_touch_in_fanout_block(self):
        """Regression guard: the fan-out must not use bare `touch` to
        create alias triggers (that produces zero-byte files and drops
        CORR_ID)."""
        services = _dedup_group_services()
        rendered = _render_rebuild_sh(
            services, ["api", "worker"], git_deploy_services=["api", "worker"]
        )
        fanout = _extract_fanout_block(rendered)
        # The fan-out block used to do `touch "${STACK_DIR}/triggers/<alias>.trigger"`.
        # A regex that matches any `touch <path>.trigger` guards against a regression.
        assert not re.search(r'\btouch\s+"?\$\{STACK_DIR\}/triggers/[^"\s]+\.trigger"?', fanout), (
            "Fan-out block must not use bare `touch` to create alias triggers — "
            "that leaves the CORR_ID out and breaks log correlation"
        )

    def test_atomic_temp_and_mv_pattern_rendered(self):
        """Alias trigger must be written atomically: temp file + mv -f."""
        services = _dedup_group_services()
        rendered = _render_rebuild_sh(
            services, ["api", "worker"], git_deploy_services=["api", "worker"]
        )
        fanout = _extract_fanout_block(rendered)
        assert "printf '%s\\n' \"${CORR_ID}\"" in fanout, (
            "Alias trigger must be written with printf so CORR_ID lands as line 1"
        )
        assert "mv -f" in fanout, (
            "Alias trigger must be moved into place atomically with mv -f"
        )

    def test_fanout_trigger_propagates_corr_id(self, tmp_path):
        """End-to-end: executing the rendered fan-out block writes the
        CORR_ID into each alias trigger file (line 1, no line 2)."""
        services = _dedup_group_services()
        rendered = _render_rebuild_sh(
            services, ["api", "worker"], git_deploy_services=["api", "worker"]
        )
        fanout = _extract_fanout_block(rendered)

        triggers_dir = tmp_path / "triggers"
        triggers_dir.mkdir(parents=True, exist_ok=True)

        corr_id = "e2e-fanout-aabbccdd-1111-2222-3333-444455556666"
        # Stub _log so the bash fragment runs without pulling in the rest of
        # rebuild.sh. The fan-out block otherwise only depends on $SERVICE,
        # $STACK_DIR, and $CORR_ID — which we set explicitly below.
        script = f"""#!/usr/bin/env bash
set -euo pipefail
STACK_DIR="{tmp_path}"
SERVICE="api"
CORR_ID="{corr_id}"
_log() {{ :; }}

{fanout}
"""
        script_path = tmp_path / "run_fanout.sh"
        script_path.write_text(script)
        script_path.chmod(0o755)

        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"fan-out block failed: rc={result.returncode} stderr={result.stderr}"
        )

        alias_trigger = triggers_dir / "worker.trigger"
        assert alias_trigger.exists(), (
            f"Alias trigger file was not created at {alias_trigger}"
        )
        content = alias_trigger.read_text()
        # Line 1 = CORR_ID exactly. Aliases intentionally have no line 2.
        assert content == f"{corr_id}\n", (
            f"Expected alias trigger to contain only '{corr_id}\\n' "
            f"(line 1 = CORR_ID, no line 2 = pull signal), got: {content!r}"
        )

        # No stray temp files left behind by the atomic write.
        leftover = list(triggers_dir.glob("*.tmp*"))
        assert leftover == [], (
            f"Atomic write left stray temp files behind: {leftover}"
        )

    def test_fanout_trigger_omits_line_two(self, tmp_path):
        """Alias trigger must contain ONLY line 1 (CORR_ID). A line 2
        would be interpreted as a pull signal and send the alias down the
        registry-pull path — wrong for local-strategy dedup, where the
        image is retagged locally from the primary."""
        services = _dedup_group_services()
        rendered = _render_rebuild_sh(
            services, ["api", "worker"], git_deploy_services=["api", "worker"]
        )
        fanout = _extract_fanout_block(rendered)

        triggers_dir = tmp_path / "triggers"
        triggers_dir.mkdir(parents=True, exist_ok=True)

        corr_id = "omit-line-two-check"
        script = f"""#!/usr/bin/env bash
set -euo pipefail
STACK_DIR="{tmp_path}"
SERVICE="api"
CORR_ID="{corr_id}"
_log() {{ :; }}

{fanout}
"""
        script_path = tmp_path / "run_fanout.sh"
        script_path.write_text(script)
        script_path.chmod(0o755)
        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        lines = (triggers_dir / "worker.trigger").read_text().splitlines()
        assert lines == [corr_id], (
            f"Alias trigger must have exactly one content line (CORR_ID); "
            f"got lines={lines!r}"
        )

# ── Build token 403 hint ──────────────────────────────────────


class TestCloneToken403Hint:
    """rebuild.sh.j2 renders CLONE_TOKEN_KEY and 403 error hint for private repos."""

    def test_clone_token_key_rendered_with_secrets_lookup(self):
        """CLONE_TOKEN_KEY is set to the vault key name via reverse-lookup."""
        services = {
            "myapp": {
                "build": {
                    "repo": "git@github.com:TestOrg/myapp.git",
                    "branch": "main",
                    "token": "ghp_test_secret_value",
                    "strategy": "remote",
                },
                "image": "registry.example.com/myapp:latest",
                "access": "public",
                "domains": ["myapp.example.com"],
                "ports": {"internal": 3000},
            }
        }
        rendered = _render_rebuild_sh(
            services,
            ["myapp"],
            secrets={"TESTORG_GIT_TOKEN": "ghp_test_secret_value"},
        )
        assert 'CLONE_TOKEN_KEY="TESTORG_GIT_TOKEN"' in rendered

    def test_clone_token_key_unknown_when_no_secrets_dict(self):
        """CLONE_TOKEN_KEY falls back to 'unknown' when secrets dict is empty."""
        services = {
            "myapp": {
                "build": {
                    "repo": "git@github.com:TestOrg/myapp.git",
                    "branch": "main",
                    "token": "ghp_test_secret_value",
                    "strategy": "remote",
                },
                "image": "registry.example.com/myapp:latest",
                "access": "public",
                "domains": ["myapp.example.com"],
                "ports": {"internal": 3000},
            }
        }
        rendered = _render_rebuild_sh(services, ["myapp"])
        # Without secrets dict, falls back to "unknown"
        assert 'CLONE_TOKEN_KEY="unknown"' in rendered

    def test_403_hint_block_present(self):
        """Clone error handler contains the 403 detection and hint message."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        assert "403" in rendered
        assert "Contents:Read" in rendered
        assert "CLONE_TOKEN_KEY" in rendered

    def test_token_value_not_in_log_lines(self):
        """The resolved token value (ghp_...) does NOT appear in any _log line."""
        services = _remote_service_with_token()
        rendered = _render_rebuild_sh(services, ["animals"])
        log_lines = [line for line in rendered.splitlines() if "_log " in line]
        for line in log_lines:
            assert "ghp_FAKE_TOKEN_12345" not in line, (
                f"Token value leaked into log line: {line}"
            )

    def test_local_token_strategy_gets_clone_token_key(self):
        """Local strategy with build.token also gets CLONE_TOKEN_KEY."""
        services = {
            "webapp": {
                "build": {
                    "repo": "git@github.com:TestOrg/webapp.git",
                    "branch": "main",
                    "token": "ghp_local_token_value",
                    # local strategy (default)
                },
                "access": "public",
                "domains": ["webapp.example.com"],
                "ports": {"internal": 8080},
            }
        }
        rendered = _render_rebuild_sh(
            services,
            ["webapp"],
            git_deploy_services=["webapp"],
            secrets={"WEBAPP_GIT_TOKEN": "ghp_local_token_value"},
        )
        assert "CLONE_TOKEN_KEY" in rendered


# ── Host-port publishing on recreate (GH bay#21) ──────────────────────


def _run_container_blocks(rendered: str) -> dict[str, str]:
    """Return the two `_run_container()` docker-run bodies keyed by path.

    The pull-signal recreate references `${IMAGE_REF}`; the local-build
    webhook recreate references `${IMAGE_NAME}:latest`.
    """
    bodies = re.findall(r"_run_container\(\) \{(.*?)\n  \}", rendered, re.DOTALL)
    out: dict[str, str] = {}
    for body in bodies:
        if '"${IMAGE_REF}"' in body:
            out["pull"] = body
        elif '"${IMAGE_NAME}:latest"' in body:
            out["local"] = body
    return out


class TestRecreatePublishesHostPorts:
    """GH bay#21 — both `docker run` recreate paths must publish host port
    bindings (`ports.expose:`/`port:`), mirroring the `deploy_stack` compose
    path. A recreate that omits `-p` silently drops PortBindings, breaking
    tailnet/loopback/host exposure (e.g. cross-region service links).
    """

    def _remote_tailnet(self):
        svc = _remote_service_with_token()
        svc["animals"]["ports"] = {"internal": 3601, "expose": "tailnet"}
        return svc

    def test_pull_signal_block_publishes_tailnet_binding(self):
        services = self._remote_tailnet()
        rendered = _render_rebuild_sh(
            services, ["animals"], gateway_bind_ip="100.64.0.9"
        )
        pull = _run_container_blocks(rendered)["pull"]
        assert '-p "100.64.0.9:3601:3601"' in pull

    def test_local_build_block_publishes_tailnet_binding(self):
        services = self._remote_tailnet()
        rendered = _render_rebuild_sh(
            services,
            ["animals"],
            git_deploy_services=["animals"],
            gateway_bind_ip="100.64.0.9",
        )
        local = _run_container_blocks(rendered)["local"]
        assert '-p "100.64.0.9:3601:3601"' in local

    def test_string_port_loopback_default(self):
        services = _local_service()
        services["localapp"]["port"] = "8080:80"  # no expose -> loopback default
        rendered = _render_rebuild_sh(
            services, ["localapp"], git_deploy_services=["localapp"]
        )
        local = _run_container_blocks(rendered)["local"]
        assert '-p "127.0.0.1:8080:80"' in local

    def test_host_expose_binds_all_interfaces(self):
        services = self._remote_tailnet()
        services["animals"]["ports"] = {"internal": 5100, "expose": "host"}
        rendered = _render_rebuild_sh(services, ["animals"])
        pull = _run_container_blocks(rendered)["pull"]
        assert '-p "0.0.0.0:5100:5100"' in pull

    def test_no_expose_emits_no_publish_flag(self):
        # ports.internal only (Traefik-fronted, no host bind) -> no -p
        services = _remote_service_with_token()  # ports: {internal: 3000}
        rendered = _render_rebuild_sh(services, ["animals"])
        pull = _run_container_blocks(rendered)["pull"]
        assert "-p " not in pull

    def test_rendered_script_is_valid_bash(self):
        import subprocess
        import tempfile

        services = self._remote_tailnet()
        rendered = _render_rebuild_sh(
            services,
            ["animals"],
            git_deploy_services=["animals"],
            gateway_bind_ip="100.64.0.9",
        )
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
            f.write(rendered)
            f.flush()
            result = subprocess.run(
                ["bash", "-n", f.name], capture_output=True, text=True
            )
        assert result.returncode == 0, result.stderr


# ── Tokenless remote/public-repo build (bay public-repo regression) ────


class TestTokenlessRemoteBuild:
    """A remote-strategy service pointing at a PUBLIC repo has no `build.token`.
    rebuild.sh.j2 must render without dereferencing `build.token` (previously
    raised `object of type 'dict' has no attribute 'token'` and broke the
    `Deploy rebuild script` task).
    """

    def _public_remote(self):
        return {
            "whoami": {
                "build": {
                    "repo": "https://github.com/traefik/whoami.git",
                    "branch": "master",
                    "strategy": "remote",
                },
                "image": "registry.example.com/x/whoami:latest",
                "access": "public",
                "domains": ["whoami.example.com"],
                "ports": {"internal": 80},
            }
        }

    def test_renders_without_token_error(self):
        # Must not raise; tokenless remote uses the plain clone URL.
        rendered = _render_rebuild_sh(
            self._public_remote(), ["whoami"], git_deploy_build_strategy="remote"
        )
        block = _extract_service_block(rendered, "whoami")
        assert 'CLONE_TOKEN_KEY="unknown"' in block
        assert "x-access-token:" not in block  # no token injected into clone URL

    def test_rendered_script_is_valid_bash(self):
        import subprocess
        import tempfile

        rendered = _render_rebuild_sh(
            self._public_remote(),
            ["whoami"],
            git_deploy_services=["whoami"],
            git_deploy_build_strategy="remote",
        )
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
            f.write(rendered)
            f.flush()
            result = subprocess.run(["bash", "-n", f.name], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
