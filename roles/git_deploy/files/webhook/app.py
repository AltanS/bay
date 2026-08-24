#!/usr/bin/env python3
"""Bay webhook receiver — validates GitHub push events and writes trigger files."""

import hashlib
import hmac
import json
import os
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pathspec

from bay_alert import bay_send_webhook

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TRIGGER_DIR = Path(os.environ.get("TRIGGER_DIR", "/triggers"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HOSTNAME = os.environ.get("HOSTNAME", "unknown")
MSG_HEADER = os.environ.get("TELEGRAM_HEADER", "")
LOCAL_REGION = os.environ.get("LOCAL_REGION", "")
PEER_TIMEOUT = float(os.environ.get("WEBHOOK_PEER_TIMEOUT", "10"))
IS_BUILD_SERVER = os.environ.get("IS_BUILD_SERVER", "").lower() in ("1", "true", "yes")

# Header used to break fan-out loops. When a webhook forwards a push to a peer,
# it sets this header to "1"; the receiving peer writes its local trigger but
# must not forward again. Without this, services that run in multiple regions
# would bounce forever between peers.
FORWARD_HEADER = "X-Bay-Webhook-Forwarded"
# A peer still running the pre-1.0 receiver forwards under the old header
# name; accept it on receive so a half-upgraded fleet cannot bounce forever.
LEGACY_FORWARD_HEADER = "X-Argo-Webhook-Forwarded"  # legacy-argo: dual-read, remove in a future major release

# Header sent by the build server after a successful remote build+push.
# When a deployment server receives this, it skips branch/path filtering
# and writes "pull" to the trigger file, which tells rebuild.sh to
# pull the image and restart the container (no build).
# Pull signals are inherently terminal — they are NEVER forwarded again.
PULL_SIGNAL_HEADER = "X-Bay-Pull-Signal"
# A build server still on the pre-1.0 rebuild.sh sends the old header.
LEGACY_PULL_SIGNAL_HEADER = "X-Argo-Pull-Signal"  # legacy-argo: dual-read, remove in a future major release

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/config/services.json"))
IMAGE_MAP_FILE = Path(os.environ.get("IMAGE_MAP_FILE", "/config/image-map.json"))
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
ALERT_WEBHOOK_FORMAT = os.environ.get("ALERT_WEBHOOK_FORMAT", "campfire")
ALERT_WEBHOOK_TIMEOUT = float(os.environ.get("ALERT_WEBHOOK_TIMEOUT", "10"))
ALERT_WEBHOOK_MAX_CHARS = int(os.environ.get("ALERT_WEBHOOK_MAX_CHARS", "3500"))
TELEGRAM_FAILURES_LOG = Path(
    os.environ.get("TELEGRAM_FAILURES_LOG", "/state/telegram-failures.log")
)

# Service config: {service_name: {"branch": "main", "paths": {"exclude": [...]}}}
SERVICE_CONFIG: dict[str, dict] = {}

# Image-to-services map: {"image_ref": ["svc1", "svc2", ...]}
# Used by the image-level pull signal handler to know which local containers
# to restart when a new image is pulled. Rendered at deploy time.
IMAGE_MAP: dict[str, list[str]] = {}


def _load_config() -> dict[str, dict]:
    """Load service config from JSON file, falling back to SERVICE_BRANCHES env var."""
    # Try JSON config file first
    if CONFIG_FILE.is_file():
        try:
            with CONFIG_FILE.open() as f:
                config = json.load(f)
            if isinstance(config, dict):
                print(f"[webhook] Loaded config from {CONFIG_FILE}", flush=True)
                return config
        except (json.JSONDecodeError, OSError) as e:
            print(
                f"[webhook] WARNING: Failed to load {CONFIG_FILE}: {e} "
                f"— falling back to SERVICE_BRANCHES env var",
                flush=True,
            )

    # Fall back to SERVICE_BRANCHES env var (backwards compat)
    config = {}
    for entry in os.environ.get("SERVICE_BRANCHES", "").split(","):
        entry = entry.strip()
        if ":" in entry:
            svc, branch = entry.split(":", 1)
            config[svc.strip()] = {"branch": branch.strip()}
    if config:
        print(
            f"[webhook] DEPRECATION: Using SERVICE_BRANCHES env var. "
            f"Mount a config file at {CONFIG_FILE} instead.",
            flush=True,
        )
    return config


def _load_image_map() -> dict[str, list[str]]:
    """Load image-to-services mapping from JSON file.

    Returns a dict mapping image refs to lists of local service names.
    Empty dict if the file doesn't exist (backwards compatible with
    deployments that predate image-level pull signals).
    """
    if IMAGE_MAP_FILE.is_file():
        try:
            with IMAGE_MAP_FILE.open() as f:
                data = json.load(f)
            if isinstance(data, dict):
                print(f"[webhook] Loaded image map from {IMAGE_MAP_FILE}", flush=True)
                return data
        except (json.JSONDecodeError, OSError) as e:
            print(
                f"[webhook] WARNING: Failed to load {IMAGE_MAP_FILE}: {e}",
                flush=True,
            )
    return {}


def _extract_changed_files(payload: dict) -> set[str]:
    """Aggregate all changed file paths from all commits in the push payload."""
    changed = set()
    for commit in payload.get("commits", []):
        changed.update(commit.get("added", []))
        changed.update(commit.get("removed", []))
        changed.update(commit.get("modified", []))
    return changed


def should_rebuild(
    changed_files: set[str], paths_config: dict | None
) -> tuple[bool, str]:
    """Determine if a rebuild should be triggered based on changed files.

    Returns (should_rebuild, reason).

    Semantics:
    - include only: rebuild if ANY file matches an include pattern
    - exclude only: rebuild unless ALL files match exclude patterns
    - both: include first (must match), then exclude removes matches;
            rebuild if any files remain after both filters
    """
    if not paths_config:
        return True, "no path filtering configured"

    include = paths_config.get("include")
    exclude = paths_config.get("exclude")

    if not include and not exclude:
        return True, "no path filter patterns configured"

    if not changed_files:
        return False, "no files changed"

    relevant = set(changed_files)

    # Include filter: keep only files matching at least one include pattern
    if include:
        try:
            include_spec = pathspec.PathSpec.from_lines("gitignore", include)
        except Exception as e:
            print(f"[webhook] WARNING: Invalid include patterns: {e}", flush=True)
            return True, f"invalid include patterns: {e}"
        relevant = {f for f in relevant if include_spec.match_file(f)}
        if not relevant:
            return False, (
                f"0/{len(changed_files)} file(s) matched include patterns"
            )

    # Exclude filter: remove files matching any exclude pattern
    if exclude:
        try:
            exclude_spec = pathspec.PathSpec.from_lines("gitignore", exclude)
        except Exception as e:
            print(f"[webhook] WARNING: Invalid exclude patterns: {e}", flush=True)
            return True, f"invalid exclude patterns: {e}"
        relevant = {f for f in relevant if not exclude_spec.match_file(f)}

    if relevant:
        return True, f"{len(relevant)} file(s) matched after filtering"
    return False, f"all {len(changed_files)} file(s) filtered out"


def compute_routing(
    local_region: str,
    service_regions: list,
    peer_urls: dict,
    is_forwarded: bool = False,
) -> tuple[bool, list]:
    """Decide whether to write a trigger locally and which peers to forward to.

    Returns (write_local, forward_targets) where forward_targets is a list of
    (peer_region, peer_base_url) tuples. A forwarded request (is_forwarded=True)
    never fans out again — that breaks loops for services running in multiple
    regions whose peer maps reference each other.

    An empty `local_region` means the consumer is single-region (no `region`
    variable defined) — stay in legacy behaviour: always write local, never
    fan out. This keeps upgrades from v0.63.x safe for single-region consumers.
    """
    if not local_region:
        return True, []
    if not service_regions:
        service_regions = [local_region]
    write_local = local_region in service_regions
    if is_forwarded:
        return write_local, []
    forward_targets = [
        (region, url)
        for region, url in peer_urls.items()
        if region != local_region and region in service_regions
    ]
    return write_local, forward_targets


def format_timestamp():
    from datetime import datetime

    return datetime.utcnow().strftime("%b %d, %H:%M UTC")


def _record_telegram_failure(reason: str) -> None:
    """Best-effort append to the Telegram failures log — never raises.

    Observability contract Phase 1: every Telegram send failure must be
    recorded so operators can detect alert-pipeline outages. See
    docs/build-pipeline-observability-contract.md.
    """
    try:
        TELEGRAM_FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with TELEGRAM_FAILURES_LOG.open("a") as f:
            f.write(f"[{ts}] send_alert failed on {HOSTNAME}: {reason}\n")
    except OSError:
        pass


def _record_webhook_failure(reason: str) -> None:
    """Same best-effort record as Telegram, for the generic sink."""
    try:
        TELEGRAM_FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with TELEGRAM_FAILURES_LOG.open("a") as f:
            f.write(f"[{ts}] webhook send failed on {HOSTNAME}: {reason}\n")
    except OSError:
        pass


def send_alert(alert_id, message):
    """Fan one alert out to every configured sink (best-effort, never blocks).

    `alert_id` is the alert's registry ID from alerts/registry.yml, and must be
    a literal at every call site — tests/test_alert_registry.py enforces that,
    because the drift check scans for literals and a variable would make it
    under-report silently. Accepted and unused here; M106-S3 routes on it.

    See roles/alert_channel/files/bay_alert.py for the shared adapters.
    """
    del alert_id
    body = MSG_HEADER + message

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": body,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[webhook] Telegram send failed: {e}", flush=True)
            _record_telegram_failure(str(e))

    def _on_webhook_error(reason: str) -> None:
        print(f"[webhook] alert webhook send failed: {reason}", flush=True)
        _record_webhook_failure(reason)

    bay_send_webhook(
        body,
        ALERT_WEBHOOK_URL,
        ALERT_WEBHOOK_FORMAT,
        timeout=ALERT_WEBHOOK_TIMEOUT,
        max_chars=ALERT_WEBHOOK_MAX_CHARS,
        on_error=_on_webhook_error,
    )


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Health endpoint
        if self.path.rstrip("/") == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = json.dumps({
                "status": "ok",
                "services": list(SERVICE_CONFIG.keys()),
            })
            self.wfile.write(body.encode())
            return

        self.send_error(404, "Not found")

    def do_POST(self):
        # Parse path: /webhook/<service> or /webhook/pull-image
        parts = self.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "webhook":
            self.send_error(404, "Not found")
            return

        target = parts[1]
        corr_id = str(uuid.uuid4())

        # ── Image-level pull signal: /webhook/pull-image ──
        # Build server POSTs here after a successful remote build+push.
        # Body contains {"image": "<image_ref>"}.
        # The handler pulls the image once, then looks up all local
        # containers using that image and writes trigger files for each.
        if target == "pull-image":
            self._handle_pull_image()
            return

        service = target
        if service not in SERVICE_CONFIG:
            self.send_error(404, f"Unknown service: {service}")
            return

        svc_config = SERVICE_CONFIG[service]
        expected_branch = svc_config.get("branch", "main")

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Validate HMAC signature
        signature_header = self.headers.get("X-Hub-Signature-256")
        if not signature_header:
            self.send_error(401, "Missing signature")
            return

        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature_header):
            self.send_error(403, "Invalid signature")
            return

        # ── Per-service pull signal (legacy): build server notifying for one service ──
        # Skips branch matching, path filtering, and fan-out routing.
        # Writes "pull" to the trigger file so rebuild.sh runs the pull-only path.
        if (
            self.headers.get(PULL_SIGNAL_HEADER) == "1"
            or self.headers.get(LEGACY_PULL_SIGNAL_HEADER) == "1"  # legacy-argo: dual-read, remove in a future major release
        ):
            TRIGGER_DIR.mkdir(parents=True, exist_ok=True)
            trigger_path = TRIGGER_DIR / f"{service}.trigger"
            trigger_path.write_text(f"{corr_id}\npull")

            print(
                f"[webhook] [{corr_id}] Pull signal received for {service} — "
                f"trigger written",
                flush=True,
            )
            send_alert(
                "webhook.pull_signal_received",
                "\U0001f4e6 <b>Pull signal received</b>\n\n"
                f"\u25b8 Host:       <code>{HOSTNAME}</code>\n"
                f"\u25b8 Service:    <code>{service}</code>\n"
                f"\u25b8 Time:       {format_timestamp()}\n\n"
                "Pulling new image from registry."
            )

            self._respond_json(200, {
                "status": "pull_triggered",
                "service": service,
                "corr_id": corr_id,
            })
            return

        # Parse payload and check branch
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        # Branch deletion — skip entirely
        if payload.get("deleted"):
            self._respond_json(200, {
                "status": "skipped",
                "reason": "branch deleted",
                "service": service,
            })
            return

        ref = payload.get("ref", "")
        if ref != f"refs/heads/{expected_branch}":
            self._respond_json(200, {
                "status": "ignored",
                "reason": f"push to {ref}, tracking {expected_branch}",
                "service": service,
            })
            return

        # Safety heuristics — always rebuild for force pushes and truncated commits
        force_rebuild = False
        force_reason = ""

        if payload.get("forced"):
            force_rebuild = True
            force_reason = "force push detected, rebuilding regardless of path filters"

        commits = payload.get("commits", [])
        if len(commits) >= 20 and not force_rebuild:
            force_rebuild = True
            force_reason = "large push (20+ commits), rebuilding to be safe — file list may be incomplete"

        # Path filtering (skip if force_rebuild is set)
        changed_files = _extract_changed_files(payload)
        paths_config = svc_config.get("paths")

        if not force_rebuild:
            rebuild, reason = should_rebuild(changed_files, paths_config)
            if not rebuild:
                print(
                    f"[webhook] Skipping {service}: {reason} "
                    f"(files: {sorted(changed_files)})",
                    flush=True,
                )
                self._respond_json(200, {
                    "status": "skipped",
                    "reason": reason,
                    "service": service,
                    "files_changed": len(changed_files),
                })
                return
        else:
            reason = force_reason
            print(f"[webhook] {service}: {force_reason}", flush=True)

        # Decide local-write vs cross-region fan-out.
        # Build server always writes a local trigger and never does
        # region-based fan-out — rebuild.sh handles post-build fan-out
        # via pull signals after the image is built and pushed.
        if IS_BUILD_SERVER:
            write_local = True
            forward_targets = []
        else:
            service_regions = svc_config.get("regions") or [LOCAL_REGION]
            peer_urls = svc_config.get("peer_urls") or {}
            is_forwarded = (
                self.headers.get(FORWARD_HEADER) == "1"
                or self.headers.get(LEGACY_FORWARD_HEADER) == "1"  # legacy-argo: dual-read, remove in a future major release
            )
            write_local, forward_targets = compute_routing(
                LOCAL_REGION, service_regions, peer_urls, is_forwarded
            )

        if not write_local and not forward_targets:
            print(
                f"[webhook] {service}: not in region {LOCAL_REGION!r} "
                f"and no peer URL configured — dropping",
                flush=True,
            )
            self._respond_json(200, {
                "status": "ignored",
                "reason": (
                    f"service not in region {LOCAL_REGION!r} and no peer URL"
                ),
                "service": service,
            })
            return

        # Extract commit context once for notifications.
        head_commit = payload.get("head_commit", {})
        commit_sha = head_commit.get("id", "unknown")[:7]
        commit_msg = (head_commit.get("message", "") or "").split("\n")[0][:80]
        pusher = payload.get("pusher", {}).get("name", "unknown")
        repo = payload.get("repository", {}).get("full_name", "unknown")

        if write_local:
            TRIGGER_DIR.mkdir(parents=True, exist_ok=True)
            trigger_path = TRIGGER_DIR / f"{service}.trigger"
            trigger_path.write_text(f"{corr_id}\n")

            send_alert(
                "webhook.received",
                "\U0001f514 <b>Webhook received</b>\n\n"
                f"\u25b8 Host:       <code>{HOSTNAME}</code>\n"
                f"\u25b8 Service:    <code>{service}</code>\n"
                f"\u25b8 Repo:       <code>{repo}</code>\n"
                f"\u25b8 Branch:     <code>{expected_branch}</code>\n"
                f"\u25b8 Commit:     <code>{commit_sha}</code> {commit_msg}\n"
                f"\u25b8 Pusher:     <code>{pusher}</code>\n"
                f"\u25b8 Time:       {format_timestamp()}"
            )
            print(
                f"[webhook] [{corr_id}] Triggered rebuild for {service} "
                f"(branch: {expected_branch}, files: {len(changed_files)})",
                flush=True,
            )

        forwarded_ok: list[str] = []
        forwarded_fail: list[str] = []
        for peer_region, peer_base_url in forward_targets:
            peer_url = f"{peer_base_url.rstrip('/')}/webhook/{service}"
            try:
                peer_req = urllib.request.Request(
                    peer_url,
                    data=body,
                    headers={
                        "Content-Type": self.headers.get(
                            "Content-Type", "application/json"
                        ),
                        "X-Hub-Signature-256": signature_header,
                        "X-GitHub-Event": self.headers.get(
                            "X-GitHub-Event", "push"
                        ),
                        "X-GitHub-Delivery": self.headers.get(
                            "X-GitHub-Delivery", ""
                        ),
                        FORWARD_HEADER: "1",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(peer_req, timeout=PEER_TIMEOUT) as resp:
                    status = resp.status
                print(
                    f"[fanout] forwarded {service} to {peer_region} "
                    f"({peer_url}) — {status}",
                    flush=True,
                )
                forwarded_ok.append(peer_region)
            except Exception as exc:
                print(
                    f"[fanout] forward failed {peer_region} ({peer_url}): {exc}",
                    flush=True,
                )
                forwarded_fail.append(peer_region)
                send_alert(
                    "webhook.fanout_failed",
                    "\u26a0\ufe0f <b>Webhook fan-out failed</b>\n\n"
                    f"\u25b8 Host:       <code>{HOSTNAME}</code>\n"
                    f"\u25b8 Service:    <code>{service}</code>\n"
                    f"\u25b8 Peer:       <code>{peer_region}</code>\n"
                    f"\u25b8 URL:        <code>{peer_url}</code>\n"
                    f"\u25b8 Error:      <code>{exc}</code>"
                )

        if write_local:
            status = "triggered"
        elif forwarded_ok:
            status = "forwarded"
        else:
            status = "forward_failed"

        self._respond_json(200, {
            "status": status,
            "service": service,
            "corr_id": corr_id,
            "files_changed": len(changed_files),
            "local": write_local,
            "forwarded": forwarded_ok,
            "forward_failures": forwarded_fail,
        })

    def _handle_pull_image(self):
        """Handle image-level pull signal at /webhook/pull-image.

        The build server POSTs here after a successful remote build+push
        with the image ref in the JSON body. This handler:
        1. Validates the HMAC signature
        2. Extracts the image ref from the body
        3. Looks up all local services using that image (from IMAGE_MAP)
        4. Writes "pull" trigger files for each matching service
        5. Sends a consolidated Telegram notification
        """
        corr_id = str(uuid.uuid4())

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Validate HMAC signature
        signature_header = self.headers.get("X-Hub-Signature-256")
        if not signature_header:
            self.send_error(401, "Missing signature")
            return

        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature_header):
            self.send_error(403, "Invalid signature")
            return

        # Parse JSON body for image ref
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON body")
            return

        image_ref = payload.get("image", "")
        if not image_ref:
            self.send_error(400, "Missing 'image' field in body")
            return

        # Look up local services using this image
        local_services = IMAGE_MAP.get(image_ref, [])
        if not local_services:
            print(
                f"[webhook] Image-level pull signal for {image_ref} — "
                f"no local services use this image, ignoring",
                flush=True,
            )
            self._respond_json(200, {
                "status": "ignored",
                "reason": "no local services use this image",
                "image": image_ref,
            })
            return

        # Write "pull" trigger for each local service
        TRIGGER_DIR.mkdir(parents=True, exist_ok=True)
        triggered = []
        for svc_name in local_services:
            trigger_path = TRIGGER_DIR / f"{svc_name}.trigger"
            trigger_path.write_text(f"{corr_id}\npull")
            triggered.append(svc_name)

        print(
            f"[webhook] [{corr_id}] Image-level pull signal for {image_ref} — "
            f"triggered {len(triggered)} services: {', '.join(triggered)}",
            flush=True,
        )

        svc_list = ", ".join(f"<code>{s}</code>" for s in triggered)
        send_alert(
            "webhook.image_pull_signal_received",
            "\U0001f4e6 <b>Image pull signal received</b>\n\n"
            f"\u25b8 Host:       <code>{HOSTNAME}</code>\n"
            f"\u25b8 Image:      <code>{image_ref}</code>\n"
            f"\u25b8 Services:   {svc_list}\n"
            f"\u25b8 Time:       {format_timestamp()}\n\n"
            f"Pulling image and restarting {len(triggered)} container(s)."
        )

        self._respond_json(200, {
            "status": "pull_triggered",
            "image": image_ref,
            "services": triggered,
            "corr_id": corr_id,
        })

    def _respond_json(self, code: int, body: dict) -> None:
        """Send a JSON response."""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format, *args):
        print(f"[webhook] {args[0]}", flush=True)


def main():
    global SERVICE_CONFIG, IMAGE_MAP

    if not WEBHOOK_SECRET:
        print("ERROR: WEBHOOK_SECRET environment variable is required", file=sys.stderr)
        sys.exit(1)

    if not LOCAL_REGION:
        print(
            "[webhook] LOCAL_REGION not set — running in single-region mode "
            "(all pushes write local triggers, no cross-region fan-out). "
            "Set LOCAL_REGION in the consumer's group_vars/<region>/main.yml "
            "to enable fan-out.",
            flush=True,
        )

    SERVICE_CONFIG = _load_config()
    if not SERVICE_CONFIG:
        print("ERROR: No services configured", file=sys.stderr)
        sys.exit(1)

    IMAGE_MAP = _load_image_map()
    if IMAGE_MAP:
        print(f"Image map: {len(IMAGE_MAP)} image(s) mapped", flush=True)
        for img, svcs in IMAGE_MAP.items():
            print(f"  {img}: {', '.join(svcs)}", flush=True)

    port = int(os.environ.get("PORT", "9000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebhookHandler)
    print(f"Webhook receiver listening on :{port}", flush=True)
    print(f"Local region: {LOCAL_REGION}", flush=True)
    print(f"Services: {', '.join(SERVICE_CONFIG.keys())}", flush=True)
    for svc_name, svc_conf in SERVICE_CONFIG.items():
        regions = svc_conf.get("regions") or []
        peers = svc_conf.get("peer_urls") or {}
        if regions:
            print(f"  {svc_name}: regions={regions} peers={list(peers)}", flush=True)
    for svc_name, svc_conf in SERVICE_CONFIG.items():
        paths = svc_conf.get("paths", {})
        if paths.get("exclude"):
            print(
                f"  {svc_name}: exclude {paths['exclude']}",
                flush=True,
            )
    server.serve_forever()


if __name__ == "__main__":
    main()
