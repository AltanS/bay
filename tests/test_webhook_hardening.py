"""The webhook receiver's public front door must be bounded and replay-safe.

`roles/git_deploy/files/webhook/app.py` is the only part of Bay that accepts
unauthenticated traffic from the open internet. Three things were missing and
are asserted here:

- **A body cap.** The receiver read `int(Content-Length)` bytes. The only
  bound on how much it would pull into memory was a number the attacker sent.
- **Replay protection.** `X-GitHub-Delivery` was read only to forward it to
  fan-out peers. The HMAC covers the body and the body never changes, so a
  single captured delivery could force rebuilds forever.
- **A quiet `/health`.** It enumerated every configured service name to anyone
  who asked.

There is also a structural assertion: both POST handlers must go through ONE
shared preamble. They used to carry byte-for-byte copies of the body read and
the HMAC check, which is exactly how a cap added in one place silently misses
the other.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

_WEBHOOK_DIR = (
    Path(__file__).resolve().parent.parent
    / "roles" / "git_deploy" / "files" / "webhook"
)
if str(_WEBHOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK_DIR))

import app as webhook_app  # noqa: E402

_SECRET = "testsecret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def receiver(tmp_path, monkeypatch):
    """The real handler on a real socket, with a real trigger directory."""
    trigger_dir = tmp_path / "triggers"
    trigger_dir.mkdir()
    monkeypatch.setattr(webhook_app, "WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(webhook_app, "TRIGGER_DIR", trigger_dir)
    monkeypatch.setattr(webhook_app, "LOCAL_REGION", "")
    monkeypatch.setattr(webhook_app, "HOSTNAME", "test-host")
    monkeypatch.setattr(webhook_app, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(webhook_app, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(webhook_app, "ALERT_WEBHOOK_URL", "")
    monkeypatch.setattr(webhook_app, "SERVICE_CONFIG", {
        "alpha": {"branch": "main"},
        "beta": {"branch": "main"},
    })
    monkeypatch.setattr(webhook_app, "IMAGE_MAP", {"reg/img:tag": ["alpha"]})
    # Each test gets an empty delivery cache; it is module-level state.
    webhook_app._delivery_seen.clear()

    server = HTTPServer(("127.0.0.1", 0), webhook_app.WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address, trigger_dir
    server.shutdown()
    server.server_close()


def _post(addr, path: str, body: bytes, headers: dict | None = None,
          declared_length: int | None = None, send_body: bool = True):
    """POST with full control over Content-Length vs bytes actually sent."""
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    hdrs = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Event": "push",
        "Content-Length": str(
            declared_length if declared_length is not None else len(body)
        ),
    }
    hdrs.update(headers or {})
    conn.putrequest("POST", path, skip_host=False, skip_accept_encoding=True)
    for k, v in hdrs.items():
        conn.putheader(k, v)
    conn.endheaders()
    if send_body:
        conn.send(body)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload


def _push_payload() -> bytes:
    return json.dumps({
        "ref": "refs/heads/main",
        "forced": True,
        "commits": [],
        "head_commit": {"id": "abc1234", "message": "x"},
        "pusher": {"name": "dev"},
    }).encode()


# ── Body cap ────────────────────────────────────────────────────────────

def test_oversized_content_length_is_413(receiver):
    addr, _ = receiver
    body = b"{}"
    status, _ = _post(
        addr, "/webhook/alpha", body,
        declared_length=webhook_app.MAX_BODY_BYTES + 1,
        send_body=False,
    )
    assert status == 413


def test_oversized_body_is_refused_without_reading_it(receiver):
    """The cap must run BEFORE the read, or the memory is already spent.

    The client declares a huge length and then sends nothing. If the handler
    read first it would block until the socket timed out; it answers 413
    immediately instead.
    """
    addr, _ = receiver
    status, _ = _post(
        addr, "/webhook/alpha", b"",
        declared_length=webhook_app.MAX_BODY_BYTES * 64,
        send_body=False,
    )
    assert status == 413


def test_cap_is_one_mebibyte(receiver):
    assert webhook_app.MAX_BODY_BYTES == 1048576


def test_garbage_content_length_is_400(receiver):
    addr, _ = receiver
    status, _ = _post(
        addr, "/webhook/alpha", b"{}",
        headers={"Content-Length": "not-a-number"},
        send_body=False,
    )
    assert status == 400


def test_a_normal_payload_still_gets_through(receiver):
    addr, trigger_dir = receiver
    body = _push_payload()
    status, payload = _post(addr, "/webhook/alpha", body)
    assert status == 200
    assert json.loads(payload)["status"] == "triggered"
    assert (trigger_dir / "alpha.trigger").exists()


def test_pull_image_endpoint_is_capped_too(receiver):
    """Both handlers share the preamble — so both inherit the cap."""
    addr, _ = receiver
    status, _ = _post(
        addr, "/webhook/pull-image", b"{}",
        declared_length=webhook_app.MAX_BODY_BYTES + 1,
        send_body=False,
    )
    assert status == 413


# ── Delivery-ID replay protection ───────────────────────────────────────

def test_replayed_delivery_triggers_no_second_build(receiver):
    addr, trigger_dir = receiver
    body = _push_payload()
    headers = {"X-GitHub-Delivery": "delivery-0001"}

    status, _ = _post(addr, "/webhook/alpha", body, headers=headers)
    assert status == 200
    trigger = trigger_dir / "alpha.trigger"
    assert trigger.exists()
    trigger.unlink()

    status, payload = _post(addr, "/webhook/alpha", body, headers=headers)
    assert status == 200, "a replay must not look like a failure to GitHub"
    assert json.loads(payload)["status"] == "duplicate"
    assert not trigger.exists(), "the replay rebuilt the service"


def test_distinct_deliveries_both_run(receiver):
    addr, trigger_dir = receiver
    body = _push_payload()
    for delivery in ("d-1", "d-2"):
        status, payload = _post(
            addr, "/webhook/alpha", body,
            headers={"X-GitHub-Delivery": delivery},
        )
        assert status == 200
        assert json.loads(payload)["status"] == "triggered"
        (trigger_dir / "alpha.trigger").unlink()


def test_missing_delivery_header_is_never_a_duplicate(receiver):
    """Peers and hand-rolled callers do not always set it; do not break them."""
    addr, trigger_dir = receiver
    body = _push_payload()
    for _ in range(3):
        status, payload = _post(addr, "/webhook/alpha", body)
        assert json.loads(payload)["status"] == "triggered"
        (trigger_dir / "alpha.trigger").unlink()


def test_a_bad_signature_cannot_poison_the_cache(receiver):
    """The dedupe check runs AFTER the HMAC check, not before."""
    addr, trigger_dir = receiver
    body = _push_payload()
    status, _ = _post(
        addr, "/webhook/alpha", body,
        headers={
            "X-GitHub-Delivery": "delivery-xyz",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert status == 403

    status, payload = _post(
        addr, "/webhook/alpha", body,
        headers={"X-GitHub-Delivery": "delivery-xyz"},
    )
    assert json.loads(payload)["status"] == "triggered"
    assert (trigger_dir / "alpha.trigger").exists()


def test_cache_is_bounded_and_evicts_oldest_first(receiver):
    webhook_app._delivery_seen.clear()
    size = webhook_app.DELIVERY_CACHE_SIZE
    assert size == 256

    assert webhook_app._delivery_is_duplicate("first") is False
    for i in range(size):
        assert webhook_app._delivery_is_duplicate(f"id-{i}") is False

    assert len(webhook_app._delivery_seen) == size
    # "first" fell out of the window, so it is accepted again.
    assert webhook_app._delivery_is_duplicate("first") is False
    # The most recent one is still remembered.
    assert webhook_app._delivery_is_duplicate(f"id-{size - 1}") is True


def test_cache_is_lock_guarded_under_concurrency(receiver):
    """ThreadingHTTPServer means two threads can race the same delivery."""
    webhook_app._delivery_seen.clear()
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        dup = webhook_app._delivery_is_duplicate("racy")
        with lock:
            results.append(dup)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(False) == 1, "exactly one caller may win the delivery"


# ── /health ─────────────────────────────────────────────────────────────

def test_health_reports_a_count_not_the_names(receiver):
    addr, _ = receiver
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    conn.request("GET", "/health")
    resp = conn.getresponse()
    payload = resp.read().decode()
    conn.close()

    assert resp.status == 200
    parsed = json.loads(payload)
    assert parsed == {"status": "ok", "services": 2}
    assert "alpha" not in payload
    assert "beta" not in payload


# ── Structure: one preamble, not two ────────────────────────────────────

def test_only_one_body_read_in_the_module():
    """A second copy is how a cap lands on one endpoint and not the other."""
    source = (_WEBHOOK_DIR / "app.py").read_text()
    assert source.count("self.rfile.read") == 1


def test_both_handlers_call_the_shared_preamble():
    source = (_WEBHOOK_DIR / "app.py").read_text()
    assert source.count("self._read_verified_body()") == 2
    assert source.count("hmac.compare_digest") == 1
