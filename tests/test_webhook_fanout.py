"""Tests for M69 cross-region webhook fan-out and M75-S2 pull signal handling.

Covers:
- S1: webhook-config.json.j2 renders from unfiltered services with
  regions + peer_urls, independent of which host renders it.
- S3: compute_routing() — write-local vs forward decisions, loop
  protection via is_forwarded flag, backward compat with missing fields.
- S3: end-to-end do_POST forwarding with HMAC signature pass-through.
- M75-S2: pull signal — app.py writes "pull" to trigger, no fan-out,
  loop safety, HMAC validation still enforced.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
from helpers import make_ansible_env

_webhook_dir = str(
    Path(__file__).resolve().parent.parent
    / "roles"
    / "git_deploy"
    / "files"
    / "webhook"
)
if _webhook_dir not in sys.path:
    sys.path.insert(0, _webhook_dir)

import app as webhook_app  # noqa: E402
from app import compute_routing  # noqa: E402


# ── Template rendering ───────────────────────────────────────────────────

TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "git_deploy"
    / "templates"
)


def _nice_json(obj):
    return json.dumps(obj, indent=4, sort_keys=True)


def _dict2items(d):
    return [{"key": k, "value": v} for k, v in d.items()]


def _render_template(services: dict, peer_urls: dict | None = None) -> dict:
    env = make_ansible_env(TEMPLATE_DIR)
    env.filters["to_nice_json"] = _nice_json
    env.filters["dict2items"] = _dict2items
    tmpl = env.get_template("webhook-config.json.j2")
    rendered = tmpl.render(
        services=services,
        git_deploy_peer_webhook_urls=peer_urls or {},
    )
    return json.loads(rendered)


def _storefront_services() -> dict:
    return {
        "storefront-de": {
            "build": {"repo": "git@github.com:acmecorp/storefront.git", "branch": "master"},
            "regions": ["eu"],
        },
        "storefront-com": {
            "build": {"repo": "git@github.com:acmecorp/storefront.git", "branch": "master"},
            "regions": ["na"],
        },
        "platform": {
            # No regions — global service
            "build": {"repo": "git@github.com:acmecorp/platform.git", "branch": "main"},
        },
        "no-build-service": {
            "access": "public",
            "regions": ["eu"],
        },
    }


class TestTemplateRendering:
    """S1: template must render all build services regardless of host region."""

    def test_renders_all_build_services_regardless_of_host(self):
        peer_urls = {
            "eu": "https://deploy.eu.example.com",
            "na": "https://deploy.na.example.com",
        }
        config = _render_template(_storefront_services(), peer_urls)
        assert set(config.keys()) == {
            "storefront-de",
            "storefront-com",
            "platform",
        }

    def test_services_without_build_block_excluded(self):
        config = _render_template(_storefront_services(), {})
        assert "no-build-service" not in config

    def test_regions_field_populated(self):
        config = _render_template(_storefront_services(), {})
        assert config["storefront-de"]["regions"] == ["eu"]
        assert config["storefront-com"]["regions"] == ["na"]
        assert config["platform"]["regions"] == []

    def test_peer_urls_field_populated(self):
        peer_urls = {
            "eu": "https://deploy.eu.example.com",
            "na": "https://deploy.na.example.com",
        }
        config = _render_template(_storefront_services(), peer_urls)
        for entry in config.values():
            assert entry["peer_urls"] == peer_urls

    def test_empty_peer_urls_does_not_crash(self):
        config = _render_template(_storefront_services(), {})
        assert config["storefront-com"]["peer_urls"] == {}

    def test_branch_and_paths_preserved(self):
        services = {
            "svc": {
                "build": {
                    "repo": "git@x:y/z.git",
                    "branch": "develop",
                    "paths": {
                        "include": ["src/**"],
                        "exclude": ["*.md"],
                    },
                },
                "regions": ["eu"],
            },
        }
        config = _render_template(services, {"eu": "https://x", "na": "https://y"})
        assert config["svc"]["branch"] == "develop"
        assert config["svc"]["paths"] == {
            "include": ["src/**"],
            "exclude": ["*.md"],
        }


# ── Routing helper ───────────────────────────────────────────────────────


class TestComputeRouting:
    """S3: compute_routing() decides local vs fan-out."""

    PEER_URLS = {
        "eu": "https://deploy.eu.example.com",
        "na": "https://deploy.na.example.com",
    }

    def test_eu_host_local_service(self):
        write_local, targets = compute_routing("eu", ["eu"], self.PEER_URLS)
        assert write_local is True
        assert targets == []

    def test_eu_host_na_only_service_forwards(self):
        write_local, targets = compute_routing("eu", ["na"], self.PEER_URLS)
        assert write_local is False
        assert targets == [("na", "https://deploy.na.example.com")]

    def test_eu_host_multi_region_service_writes_and_forwards(self):
        write_local, targets = compute_routing(
            "eu", ["eu", "na"], self.PEER_URLS
        )
        assert write_local is True
        assert targets == [("na", "https://deploy.na.example.com")]

    def test_forwarded_request_does_not_fan_out_again(self):
        write_local, targets = compute_routing(
            "na", ["eu", "na"], self.PEER_URLS, is_forwarded=True
        )
        assert write_local is True
        assert targets == []

    def test_empty_service_regions_treated_as_local_only(self):
        write_local, targets = compute_routing("eu", [], self.PEER_URLS)
        assert write_local is True
        assert targets == []

    def test_missing_peer_urls_means_no_forward(self):
        write_local, targets = compute_routing("eu", ["na"], {})
        assert write_local is False
        assert targets == []

    def test_peer_with_unknown_region_ignored(self):
        # peer_urls has 'ap' but service doesn't run there — skip
        peers = {"na": "https://na", "ap": "https://ap"}
        write_local, targets = compute_routing("eu", ["na"], peers)
        assert write_local is False
        assert targets == [("na", "https://na")]

    def test_empty_local_region_is_legacy_mode(self):
        # Single-region consumers have no `region` var — LOCAL_REGION is empty.
        # Must always write local and never fan out, even if the service
        # declares regions or peer_urls (accidental config from framework bump).
        write_local, targets = compute_routing("", ["eu"], {"eu": "https://eu"})
        assert write_local is True
        assert targets == []


# ── End-to-end HTTP fan-out with signature pass-through ──────────────────


def _hmac_sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


def _make_push_payload(branch: str = "master") -> bytes:
    payload = {
        "ref": f"refs/heads/{branch}",
        "commits": [
            {
                "id": "abc123def456",
                "message": "test commit",
                "added": ["src/foo.ts"],
                "modified": [],
                "removed": [],
            }
        ],
        "head_commit": {"id": "abc123def456", "message": "test commit"},
        "pusher": {"name": "tester"},
        "repository": {"full_name": "test/repo"},
        "forced": False,
    }
    return json.dumps(payload).encode()


@pytest.fixture
def webhook_env(tmp_path, monkeypatch):
    """Configure webhook app env + config for tests."""
    trigger_dir = tmp_path / "triggers"
    trigger_dir.mkdir()
    monkeypatch.setattr(webhook_app, "WEBHOOK_SECRET", "testsecret")
    monkeypatch.setattr(webhook_app, "TRIGGER_DIR", trigger_dir)
    monkeypatch.setattr(webhook_app, "LOCAL_REGION", "eu")
    monkeypatch.setattr(webhook_app, "PEER_TIMEOUT", 2.0)
    monkeypatch.setattr(webhook_app, "HOSTNAME", "test-eu")
    monkeypatch.setattr(webhook_app, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(webhook_app, "TELEGRAM_CHAT_ID", "")
    return trigger_dir


class _StubPeerHandler:
    """Stub HTTP handler that records incoming forwarded requests."""

    received: list[dict] = []

    @classmethod
    def make(cls):
        from http.server import BaseHTTPRequestHandler

        received = cls.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                received.append(
                    {
                        "path": self.path,
                        "body": body,
                        "signature": self.headers.get("X-Hub-Signature-256"),
                        "forwarded_flag": self.headers.get(
                            "X-Bay-Webhook-Forwarded"
                        ),
                        "event": self.headers.get("X-GitHub-Event"),
                    }
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args, **kwargs):
                pass

        return Handler


@pytest.fixture
def peer_server():
    _StubPeerHandler.received = []
    handler = _StubPeerHandler.make()
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _StubPeerHandler.received
    server.shutdown()
    server.server_close()


def _run_primary(service_config: dict):
    """Start the real webhook HTTPServer in a thread using the app module."""
    monkey_config = service_config
    webhook_app.SERVICE_CONFIG = monkey_config
    server = HTTPServer(("127.0.0.1", 0), webhook_app.WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class TestFanOutEndToEnd:
    """S3: full do_POST path with real HTTP servers."""

    def _post(self, url: str, body: bytes, extra_headers=None):
        import urllib.request

        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _hmac_sign("testsecret", body),
            "X-GitHub-Event": "push",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def test_na_only_service_forwards_without_local_trigger(
        self, webhook_env, peer_server
    ):
        peer_url, peer_received = peer_server
        config = {
            "storefront-com": {
                "branch": "master",
                "regions": ["na"],
                "peer_urls": {"eu": "http://unused", "na": peer_url},
            }
        }
        server, base = _run_primary(config)
        try:
            body = _make_push_payload("master")
            status, resp_body = self._post(f"{base}/webhook/storefront-com", body)
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "forwarded"
        assert resp_body["local"] is False
        assert resp_body["forwarded"] == ["na"]
        # No local trigger
        assert list(webhook_env.iterdir()) == []
        # Peer received the forwarded push with pass-through signature
        assert len(peer_received) == 1
        forwarded = peer_received[0]
        assert forwarded["path"] == "/webhook/storefront-com"
        assert forwarded["body"] == body
        assert forwarded["signature"] == _hmac_sign("testsecret", body)
        assert forwarded["forwarded_flag"] == "1"
        assert forwarded["event"] == "push"

    def test_local_service_writes_trigger_and_skips_fanout(
        self, webhook_env, peer_server
    ):
        peer_url, peer_received = peer_server
        config = {
            "storefront-de": {
                "branch": "master",
                "regions": ["eu"],
                "peer_urls": {"eu": "http://unused", "na": peer_url},
            }
        }
        server, base = _run_primary(config)
        try:
            body = _make_push_payload("master")
            status, resp_body = self._post(f"{base}/webhook/storefront-de", body)
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "triggered"
        assert resp_body["local"] is True
        assert resp_body["forwarded"] == []
        assert (webhook_env / "storefront-de.trigger").exists()
        assert peer_received == []

    def test_multi_region_service_writes_and_forwards(
        self, webhook_env, peer_server
    ):
        peer_url, peer_received = peer_server
        config = {
            "multi-svc": {
                "branch": "master",
                "regions": ["eu", "na"],
                "peer_urls": {"eu": "http://unused", "na": peer_url},
            }
        }
        server, base = _run_primary(config)
        try:
            body = _make_push_payload("master")
            status, resp_body = self._post(f"{base}/webhook/multi-svc", body)
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["local"] is True
        assert resp_body["forwarded"] == ["na"]
        assert (webhook_env / "multi-svc.trigger").exists()
        assert len(peer_received) == 1

    def test_forwarded_request_writes_local_but_does_not_refan(
        self, webhook_env, peer_server
    ):
        peer_url, peer_received = peer_server
        # This primary is "eu" (webhook_env fixture) but receives a forwarded
        # request for a multi-region service. It should write its trigger but
        # not bounce back.
        config = {
            "multi-svc": {
                "branch": "master",
                "regions": ["eu", "na"],
                "peer_urls": {"eu": "http://unused", "na": peer_url},
            }
        }
        server, base = _run_primary(config)
        try:
            body = _make_push_payload("master")
            status, _resp = self._post(
                f"{base}/webhook/multi-svc",
                body,
                extra_headers={"X-Bay-Webhook-Forwarded": "1"},
            )
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert (webhook_env / "multi-svc.trigger").exists()
        # Forward flag must suppress fan-out
        assert peer_received == []

    def test_forward_failure_returns_200_and_reports_failure(
        self, webhook_env
    ):
        # Peer URL points at a port nothing is listening on.
        config = {
            "storefront-com": {
                "branch": "master",
                "regions": ["na"],
                "peer_urls": {"na": "http://127.0.0.1:1"},  # invalid port
            }
        }
        server, base = _run_primary(config)
        try:
            body = _make_push_payload("master")
            status, resp_body = self._post(f"{base}/webhook/storefront-com", body)
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "forward_failed"
        assert resp_body["forward_failures"] == ["na"]
        assert resp_body["forwarded"] == []
        assert list(webhook_env.iterdir()) == []

    def test_backward_compat_config_without_regions(self, webhook_env, peer_server):
        peer_url, peer_received = peer_server
        # Legacy entry with no regions / peer_urls fields
        config = {
            "legacy": {"branch": "main"},
        }
        server, base = _run_primary(config)
        try:
            body = _make_push_payload("main")
            status, resp_body = self._post(f"{base}/webhook/legacy", body)
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["local"] is True
        assert resp_body["forwarded"] == []
        assert (webhook_env / "legacy.trigger").exists()
        assert peer_received == []


# ── M75-S2: Pull signal handling ─────────────────────────────────────────


class TestPullSignal:
    """M75-S2: pull signals from build server to deployment servers."""

    def _post(self, url: str, body: bytes, extra_headers=None):
        import urllib.request

        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _hmac_sign("testsecret", body),
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def test_pull_signal_writes_pull_to_trigger_file(self, webhook_env):
        """Pull signal writes 'pull' (not empty) to the trigger file."""
        config = {
            "animals": {
                "branch": "main",
                "regions": ["eu"],
                "peer_urls": {},
            }
        }
        server, base = _run_primary(config)
        try:
            body = b'{"service":"animals"}'
            status, resp_body = self._post(
                f"{base}/webhook/animals",
                body,
                extra_headers={"X-Bay-Pull-Signal": "1"},
            )
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "pull_triggered"
        assert resp_body["service"] == "animals"
        trigger = webhook_env / "animals.trigger"
        assert trigger.exists()
        trigger_content = trigger.read_text()
        assert trigger_content.endswith("\npull"), (
            f"Pull signal trigger should end with '\\npull', got: {trigger_content!r}"
        )

    def test_pull_signal_requires_hmac(self, webhook_env):
        """Pull signal without valid HMAC is rejected."""
        import urllib.request
        import urllib.error

        config = {
            "animals": {"branch": "main", "regions": ["eu"], "peer_urls": {}},
        }
        server, base = _run_primary(config)
        try:
            body = b'{"service":"animals"}'
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=invalid",
                "X-Bay-Pull-Signal": "1",
            }
            req = urllib.request.Request(
                f"{base}/webhook/animals", data=body, headers=headers, method="POST"
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                assert False, "Should have raised HTTPError"
            except urllib.error.HTTPError as e:
                assert e.code == 403
        finally:
            server.shutdown()
            server.server_close()

    def test_pull_signal_does_not_fan_out(self, webhook_env, peer_server):
        """Pull signal is terminal — never forwarded to peers."""
        peer_url, peer_received = peer_server
        config = {
            "multi-svc": {
                "branch": "main",
                "regions": ["eu", "na"],
                "peer_urls": {"eu": "http://unused", "na": peer_url},
            }
        }
        server, base = _run_primary(config)
        try:
            body = b'{"service":"multi-svc"}'
            status, resp_body = self._post(
                f"{base}/webhook/multi-svc",
                body,
                extra_headers={"X-Bay-Pull-Signal": "1"},
            )
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "pull_triggered"
        # Pull signal is terminal — no fan-out to peers
        assert peer_received == []
        # But trigger file is written
        trigger = webhook_env / "multi-svc.trigger"
        assert trigger.exists()
        trigger_content = trigger.read_text()
        assert trigger_content.endswith("\npull"), (
            f"Pull signal trigger should end with '\\npull', got: {trigger_content!r}"
        )

    def test_pull_signal_unknown_service_returns_404(self, webhook_env):
        """Pull signal for unknown service returns 404."""
        import urllib.request
        import urllib.error

        config = {
            "animals": {"branch": "main", "regions": ["eu"], "peer_urls": {}},
        }
        server, base = _run_primary(config)
        try:
            body = b'{"service":"unknown"}'
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _hmac_sign("testsecret", body),
                "X-Bay-Pull-Signal": "1",
            }
            req = urllib.request.Request(
                f"{base}/webhook/unknown", data=body, headers=headers, method="POST"
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                assert False, "Should have raised HTTPError"
            except urllib.error.HTTPError as e:
                assert e.code == 404
        finally:
            server.shutdown()
            server.server_close()

    def test_pull_signal_body_not_required_to_be_json(self, webhook_env):
        """Pull signal body doesn't need to be valid push payload JSON."""
        config = {
            "animals": {"branch": "main", "regions": ["eu"], "peer_urls": {}},
        }
        server, base = _run_primary(config)
        try:
            # Body is arbitrary — pull signal skips JSON parsing
            body = b"not-json-at-all"
            status, resp_body = self._post(
                f"{base}/webhook/animals",
                body,
                extra_headers={"X-Bay-Pull-Signal": "1"},
            )
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "pull_triggered"
        trigger_content = (webhook_env / "animals.trigger").read_text()
        assert trigger_content.endswith("\npull"), (
            f"Pull signal trigger should end with '\\npull', got: {trigger_content!r}"
        )

    def test_normal_push_still_touches_trigger_empty(self, webhook_env):
        """Regular push (no pull signal) still touches trigger as empty file."""
        config = {
            "animals": {"branch": "main", "regions": ["eu"], "peer_urls": {}},
        }
        server, base = _run_primary(config)
        try:
            body = _make_push_payload("main")
            status, resp_body = self._post(f"{base}/webhook/animals", body)
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "triggered"
        trigger = webhook_env / "animals.trigger"
        assert trigger.exists()
        # Regular push writes "{corr_id}\n" (v2 format), not empty
        trigger_content = trigger.read_text()
        assert not trigger_content.endswith("\npull"), (
            f"Regular push trigger should not end with '\\npull', got: {trigger_content!r}"
        )
        # The trigger should contain a UUID-like corr_id
        assert len(trigger_content.strip()) > 0, "Trigger should contain corr_id"


# ── M75-S6: Image-level pull signal handling ─────────────────────────────


class TestImageLevelPullSignal:
    """M75-S6: image-level pull signals at /webhook/pull-image."""

    def _post(self, url: str, body: bytes, extra_headers=None):
        import urllib.request

        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _hmac_sign("testsecret", body),
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def test_image_pull_triggers_all_matching_services(self, webhook_env):
        """Image-level pull signal writes trigger files for all services using the image."""
        config = {
            "storefront-de": {"branch": "master", "regions": ["eu"]},
        }
        image_map = {
            "registry.example.com/storefront:latest": [
                "storefront-de",
                "storefront-es",
                "storefront-fr",
            ],
        }
        webhook_app.SERVICE_CONFIG = config
        webhook_app.IMAGE_MAP = image_map
        server = HTTPServer(("127.0.0.1", 0), webhook_app.WebhookHandler)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"image": "registry.example.com/storefront:latest"}).encode()
            status, resp_body = self._post(
                f"{base}/webhook/pull-image",
                body,
                extra_headers={"X-Bay-Pull-Signal": "1"},
            )
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "pull_triggered"
        assert resp_body["image"] == "registry.example.com/storefront:latest"
        assert set(resp_body["services"]) == {
            "storefront-de", "storefront-es", "storefront-fr"
        }
        # All three trigger files should exist with pull signal (v2 format: "{corr_id}\npull")
        for svc in ["storefront-de", "storefront-es", "storefront-fr"]:
            trigger = webhook_env / f"{svc}.trigger"
            assert trigger.exists(), f"Trigger file missing for {svc}"
            trigger_content = trigger.read_text()
            assert trigger_content.endswith("\npull"), (
                f"Trigger for {svc} should end with '\\npull' (v2 format), got: {trigger_content!r}"
            )

    def test_image_pull_unknown_image_returns_ignored(self, webhook_env):
        """Image-level pull signal for unmapped image returns ignored."""
        config = {"animals": {"branch": "main"}}
        image_map = {
            "registry.example.com/animals:latest": ["animals"],
        }
        webhook_app.SERVICE_CONFIG = config
        webhook_app.IMAGE_MAP = image_map
        server = HTTPServer(("127.0.0.1", 0), webhook_app.WebhookHandler)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"image": "unknown-image:latest"}).encode()
            status, resp_body = self._post(
                f"{base}/webhook/pull-image",
                body,
                extra_headers={"X-Bay-Pull-Signal": "1"},
            )
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "ignored"
        assert "no local services" in resp_body["reason"]

    def test_image_pull_requires_hmac(self, webhook_env):
        """Image-level pull signal without valid HMAC is rejected."""
        import urllib.request
        import urllib.error

        config = {"animals": {"branch": "main"}}
        image_map = {"registry.example.com/animals:latest": ["animals"]}
        webhook_app.SERVICE_CONFIG = config
        webhook_app.IMAGE_MAP = image_map
        server = HTTPServer(("127.0.0.1", 0), webhook_app.WebhookHandler)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"image": "registry.example.com/animals:latest"}).encode()
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=invalid",
            }
            req = urllib.request.Request(
                f"{base}/webhook/pull-image", data=body, headers=headers, method="POST"
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                assert False, "Should have raised HTTPError"
            except urllib.error.HTTPError as e:
                assert e.code == 403
        finally:
            server.shutdown()
            server.server_close()

    def test_image_pull_missing_image_field(self, webhook_env):
        """Image-level pull signal without image field returns 400."""
        import urllib.request
        import urllib.error

        config = {"animals": {"branch": "main"}}
        image_map = {}
        webhook_app.SERVICE_CONFIG = config
        webhook_app.IMAGE_MAP = image_map
        server = HTTPServer(("127.0.0.1", 0), webhook_app.WebhookHandler)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"not_image": "foo"}).encode()
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _hmac_sign("testsecret", body),
            }
            req = urllib.request.Request(
                f"{base}/webhook/pull-image", data=body, headers=headers, method="POST"
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                assert False, "Should have raised HTTPError"
            except urllib.error.HTTPError as e:
                assert e.code == 400
        finally:
            server.shutdown()
            server.server_close()

    def test_producer_with_siblings_all_receive_pull_M90_GH_13(self, webhook_env):
        """M90 / GH-13 regression guard: an inbound image-level pull webhook
        for an image shared between a build-producer and its pull-only
        siblings must fan out to ALL of them (producer included).

        This is the end-to-end half of the M90 regression fix. The
        receiver builds its recipient list from IMAGE_MAP[image_ref];
        if image-map.json on disk omits the producer (the M87 cutover
        bug), the receiver silently drops it. With spec 02's deploy_stack
        re-render and spec 03's restart handler, the on-disk file is
        current and the in-memory map matches it — so this end-to-end
        path fans out correctly.

        Fixture: one producer (`test-producer`) plus two siblings
        (`sibling-a`, `sibling-b`) sharing `test-producer-img:latest`.
        Asserts the response `services` list contains all three AND a
        trigger file is written for each with the pull-signal marker.

        """
        config = {
            "test-producer": {"branch": "main", "regions": ["eu"]},
            "sibling-a": {"branch": "main", "regions": ["eu"]},
            "sibling-b": {"branch": "main", "regions": ["eu"]},
        }
        image_ref = "registry.example.com/test-producer-img:latest"
        image_map = {
            image_ref: ["test-producer", "sibling-a", "sibling-b"],
        }
        webhook_app.SERVICE_CONFIG = config
        webhook_app.IMAGE_MAP = image_map
        server = HTTPServer(("127.0.0.1", 0), webhook_app.WebhookHandler)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"image": image_ref}).encode()
            status, resp_body = self._post(
                f"{base}/webhook/pull-image",
                body,
                extra_headers={"X-Bay-Pull-Signal": "1"},
            )
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "pull_triggered"
        assert resp_body["image"] == image_ref
        # All three services — producer included — must be in the response.
        assert set(resp_body["services"]) == {
            "test-producer", "sibling-a", "sibling-b"
        }, (
            f"GH-13: producer must be present in the pull-triggered "
            f"services list. Got: {resp_body['services']}. The M87 "
            f"regression was the producer being silently dropped."
        )
        # And the producer's trigger file must actually be written with
        # the pull marker — not just listed in the response.
        for svc in ["test-producer", "sibling-a", "sibling-b"]:
            trigger = webhook_env / f"{svc}.trigger"
            assert trigger.exists(), (
                f"GH-13: trigger file missing for {svc} after pull webhook"
            )
            assert trigger.read_text().endswith("\npull"), (
                f"GH-13: trigger for {svc} should end with '\\npull' "
                f"(v2 pull-signal format)"
            )

    def test_image_pull_empty_image_map(self, webhook_env):
        """Image-level pull signal with empty image map returns ignored."""
        config = {"animals": {"branch": "main"}}
        webhook_app.SERVICE_CONFIG = config
        webhook_app.IMAGE_MAP = {}
        server = HTTPServer(("127.0.0.1", 0), webhook_app.WebhookHandler)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"image": "registry.example.com/animals:latest"}).encode()
            status, resp_body = self._post(
                f"{base}/webhook/pull-image",
                body,
                extra_headers={"X-Bay-Pull-Signal": "1"},
            )
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert resp_body["status"] == "ignored"
