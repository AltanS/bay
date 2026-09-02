"""The dynamic-config mount must not be gated on the DNS-01 challenge.

`tls.options` is DYNAMIC Traefik configuration. The same block in the
static `traefik.yml` is parsed and then ignored, so the "TLS 1.2 floor"
shipped as a static block was a silent no-op. The floor now lives in
`{{ stack_dir }}/dynamic/tls-options.yml`, which only reaches Traefik if
BOTH the file provider and the `/etc/traefik/dynamic` bind mount are
unconditional.

Two mount paths must agree, because the reconciler builds container specs
from `build_specs.yml` and ignores the compose partials entirely:

  - `roles/container_lifecycle/tasks/build_specs.yml` — source of truth.
  - `roles/deploy_stack/templates/_traefik.j2` — the compose mirror.

Also guards F18: the webhook receiver writes its Telegram-failure log to
`/state`, which the compose mirror mounted and the reconciler spec did
not. Unmounted, those writes land in the container's writable layer and
are lost on every recreate.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
BUILD_SPECS = ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
TRAEFIK_PARTIAL = ROOT / "roles" / "deploy_stack" / "templates" / "_traefik.j2"
WEBHOOK_PARTIAL = ROOT / "roles" / "deploy_stack" / "templates" / "_webhook_receiver.j2"

DYNAMIC_MOUNT = "/dynamic:/etc/traefik/dynamic:ro"
STATE_MOUNT = "/state:/state"


def _task(name: str) -> dict:
    for task in yaml.safe_load(BUILD_SPECS.read_text()):
        if task.get("name") == name:
            return task
    raise AssertionError(f"task not found: {name}")


class TestTraefikDynamicMount:
    def test_reconciler_spec_mounts_the_dynamic_dir(self):
        spec = _task("Build traefik container spec")["vars"]["_traefik_spec"]
        assert DYNAMIC_MOUNT in spec["volumes"]

    def test_reconciler_dynamic_mount_is_not_dns_gated(self):
        """It must sit in the base list, not the `if _traefik_dns_enabled` one."""
        gated = _task("Build traefik container spec")["vars"]["_traefik_dns_volumes"]
        assert DYNAMIC_MOUNT not in gated
        assert "acme-dns.json" in gated  # the DNS-01 store IS still gated

    def test_compose_mirror_mounts_the_dynamic_dir_unconditionally(self):
        """The mount line must not live inside the DNS-01 `{% if %}` block."""
        text = TRAEFIK_PARTIAL.read_text()
        mount_idx = text.index(DYNAMIC_MOUNT)
        gate_idx = text.index("{% if traefik_dns_challenge_enabled")
        assert mount_idx < gate_idx


class TestWebhookStateMount:
    def test_reconciler_spec_mounts_state(self):
        """app.py writes /state/telegram-failures.log."""
        spec = _task("Build webhook receiver container spec")["vars"]["_webhook_spec"]
        volumes = " ".join(spec["volumes"])
        assert STATE_MOUNT in volumes

    def test_compose_mirror_also_mounts_state(self):
        assert STATE_MOUNT in WEBHOOK_PARTIAL.read_text()
