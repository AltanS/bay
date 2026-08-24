#!/usr/bin/env python3
"""Tailnet identity ForwardAuth sidecar.

Traefik calls this service as a ForwardAuth middleware for tailnet-proxy routes.
It maps the *real client tailnet IP* (which infra's Traefik sees, before the
identity-collapsing hop to the upstream node) to the client's Headscale device
name, and returns it as a response header that Traefik copies onto the upstream
request (`authResponseHeaders`). The downstream app (e.g. homelab-app) trusts the
header only because a Headscale ACL makes infra the sole node that can reach the
upstream — so the header cannot be forged from another tailnet node.

Two resolution sources (IDENTITY_SOURCE):
  - sqlite: read-only query of Headscale's DB. No API key, no container exec,
    no deploy-ordering hazard. Schema-coupled to the pinned headscale_version
    (the `nodes` table's `ipv4` column).
  - api: Headscale HTTP API (GET /api/v1/node, Bearer key). Version-stable
    interface; requires an API key provisioned at deploy.

Which one you get depends on how the service was started. With IDENTITY_SOURCE
unset the code below falls back to `sqlite`, but the Ansible role always renders
IDENTITY_SOURCE explicitly and its default (tailnet_identity_source in
roles/tailnet_identity/defaults/main.yml) is `api` — so a deployed instance is
on `api` unless the consumer overrides it. The `sqlite` fallback only applies to
a hand-started process.

The service is reachable ONLY from Traefik over the docker network (no host
port, no public router) — see _tailnet_identity.j2.
"""

import json
import os
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IDENTITY_SOURCE = os.environ.get("IDENTITY_SOURCE", "sqlite").lower()
HEADSCALE_DB = os.environ.get("HEADSCALE_DB", "/data/db.sqlite")
HEADSCALE_API_URL = os.environ.get("HEADSCALE_API_URL", "http://headscale:8080")
HEADSCALE_API_KEY = os.environ.get("HEADSCALE_API_KEY", "")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9100"))
# What to do when the client IP maps to no known device:
#   pass  -> 200 + DEVICE_HEADER: "unknown" (app decides)
#   deny  -> 403 (fail closed at the edge)
UNKNOWN_ACTION = os.environ.get("UNKNOWN_ACTION", "pass").lower()
UNKNOWN_VALUE = os.environ.get("UNKNOWN_VALUE", "unknown")
DEVICE_HEADER = os.environ.get("DEVICE_HEADER", "X-Tailnet-Device")
DEVICE_ID_HEADER = os.environ.get("DEVICE_ID_HEADER", "X-Tailnet-Device-Id")
NODE_REFRESH_SECONDS = float(os.environ.get("NODE_REFRESH_SECONDS", "30"))
API_TIMEOUT = float(os.environ.get("API_TIMEOUT", "5"))


def _log(msg: str) -> None:
    print(f"[tailnet-identity] {msg}", flush=True)


class NodeMap:
    """Thread-safe TTL cache of tailnet IP -> (device_name, node_id)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: dict[str, tuple[str, str]] = {}
        self._loaded_at = 0.0
        self._refreshing = False

    def lookup(self, ip: str) -> tuple[str, str] | None:
        # Decide whether THIS thread should refresh, then load OUTSIDE the lock so
        # concurrent /auth requests aren't serialized behind a slow/failing read
        # (the I/O can take up to API_TIMEOUT). Only one thread refreshes at a time;
        # the rest serve the current (possibly stale) map. Fails closed: a refresh
        # error keeps the last good map and backs off, never bypasses auth.
        with self._lock:
            stale = time.monotonic() - self._loaded_at > NODE_REFRESH_SECONDS
            do_refresh = stale and not self._refreshing
            if do_refresh:
                self._refreshing = True
            current = self._map
        if do_refresh:
            try:
                new_map = _load_sqlite() if IDENTITY_SOURCE == "sqlite" else _load_api()
                with self._lock:
                    self._map = new_map
                    self._loaded_at = time.monotonic()
                    self._refreshing = False
                current = new_map
                _log(f"refreshed node map: {len(new_map)} nodes (source={IDENTITY_SOURCE})")
            except Exception as e:  # noqa: BLE001 — never let a refresh error wedge auth
                with self._lock:
                    self._loaded_at = time.monotonic()  # back off; keep stale map
                    self._refreshing = False
                _log(f"WARNING: node map refresh failed ({e}); keeping {len(current)} cached")
        return current.get(ip)


def _load_sqlite() -> dict[str, tuple[str, str]]:
    """Read tailnet IPv4 -> (given_name, id) from Headscale's sqlite DB (read-only).

    NOTE: `immutable=1` is required to open Headscale's WAL-mode DB read-only — a
    plain `mode=ro` open of a live WAL database fails without write access to the
    -shm/-wal files. The trade-off is that immutable reads the base file and may
    miss very recent WAL-only commits (bounded staleness on top of the cache TTL).
    For an identity source this matters; the `api` source has no such caveat, and it
    is what the Ansible role defaults to (tailnet_identity_source: "api"), so a
    deployed instance only lands here if the consumer opted in. Note the code-level
    fallback with IDENTITY_SOURCE unset is `sqlite` — that is the hand-started case,
    not the deployed one. Use sqlite only when you accept slightly-stale reads.
    """
    uri = f"file:{HEADSCALE_DB}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=API_TIMEOUT)
    try:
        rows = conn.execute(
            "SELECT ipv4, given_name, id FROM nodes WHERE ipv4 IS NOT NULL AND ipv4 != ''"
        ).fetchall()
    finally:
        conn.close()
    result: dict[str, tuple[str, str]] = {}
    for ipv4, given_name, node_id in rows:
        if ipv4:
            result[str(ipv4)] = (str(given_name or "unknown"), str(node_id))
    return result


def _load_api() -> dict[str, tuple[str, str]]:
    """Read tailnet IPs -> (givenName, id) from the Headscale HTTP API."""
    req = urllib.request.Request(
        f"{HEADSCALE_API_URL.rstrip('/')}/api/v1/node",
        headers={"Authorization": f"Bearer {HEADSCALE_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
        data = json.load(resp)
    result: dict[str, tuple[str, str]] = {}
    for node in data.get("nodes", []):
        name = node.get("givenName") or node.get("name") or "unknown"
        node_id = str(node.get("id", ""))
        for ip in node.get("ipAddresses", []):
            if "." in ip:  # IPv4 — the address Traefik sees as the client
                result[ip] = (str(name), node_id)
    return result


NODE_MAP = NodeMap()


def _client_ip(headers) -> str | None:
    """The real client tailnet IP, from the leftmost X-Forwarded-For entry.

    SECURITY INVARIANT: the leftmost XFF entry is the real client ONLY because
    Traefik has NO `forwardedHeaders.trustedIPs` configured on its entrypoints —
    so Traefik discards any client-supplied X-Forwarded-For and re-sets it to the
    actual TCP peer (the tailnet IP). If a trusted front proxy / CDN is EVER placed
    before Traefik AND added to `trustedIPs`, Traefik will PRESERVE client-supplied
    XFF and this leftmost read becomes attacker-controlled → identity forgery.
    Before fronting Traefik with anything, revisit this (read the proxy-appended
    hop instead) — see docs/tailnet-ingress.md.
    """
    xff = headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return headers.get("X-Real-Ip")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet default access logging
        pass

    def _send_identity(self, status: int, device: str, node_id: str) -> None:
        self.send_response(status)
        # ALWAYS set BOTH identity headers (even on the unknown path) so Traefik's
        # authResponseHeaders unconditionally REPLACES any client-supplied value —
        # a client cannot smuggle X-Tailnet-Device or X-Tailnet-Device-Id past us.
        self.send_header(DEVICE_HEADER, device)
        self.send_header(DEVICE_ID_HEADER, node_id)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ip = _client_ip(self.headers)
        match = NODE_MAP.lookup(ip) if ip else None
        if match:
            name, node_id = match
            self._send_identity(200, name, node_id)
            return
        # Unknown client — still emit both headers (empty id) to override spoofing.
        if UNKNOWN_ACTION == "deny":
            self._send_identity(403, UNKNOWN_VALUE, "")
        else:
            self._send_identity(200, UNKNOWN_VALUE, "")

    # ForwardAuth issues GET; accept HEAD for health probes too.
    do_HEAD = do_GET


def main() -> None:
    _log(
        f"starting on :{LISTEN_PORT} source={IDENTITY_SOURCE} "
        f"unknown_action={UNKNOWN_ACTION} header={DEVICE_HEADER}"
    )
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
