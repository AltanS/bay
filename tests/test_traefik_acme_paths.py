"""Pin the ACME certificate-storage contract across all four places it lives.

Traefik persists issued certificates to the `storage:` path declared on each
resolver in `traefik.yml`. That path is inside the container, so it only
survives a container recreate if a volume mount puts it on the host. The
mount target and the `storage:` value must therefore agree exactly.

If they silently drift, nothing fails loudly: Traefik starts, serves traffic,
and re-issues certificates into a non-persistent path. The damage shows up
later as Let's Encrypt rate-limit exhaustion after a few recreates.

Four files carry a piece of this contract and must stay consistent:

1. ``roles/traefik/defaults/main.yml``           — host-side paths
2. ``roles/traefik/templates/traefik.yml.j2``    — container-side ``storage:``
3. ``roles/container_lifecycle/tasks/build_specs.yml`` — reconciler mounts
   (this is the live deploy path — see docs/reconciler.md)
4. ``roles/deploy_stack/templates/_traefik.j2``  — compose-partial mounts

Regression context: a repo-wide rename during the open-sourcing scrub
rewrote ``acme.json`` to ``demo.json`` in (1), (2), (3) and (4). The full
suite stayed green — the only test that referenced ``traefik_acme_path``
supplied its own fixture value and never asserted the role default. These
tests close that gap.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from helpers import make_ansible_env

_ROOT = Path(__file__).resolve().parent.parent

_TRAEFIK_DEFAULTS = _ROOT / "roles/traefik/defaults/main.yml"
_TRAEFIK_TEMPLATE_DIR = _ROOT / "roles/traefik/templates"
_BUILD_SPECS = _ROOT / "roles/container_lifecycle/tasks/build_specs.yml"
_TRAEFIK_PARTIAL = _ROOT / "roles/deploy_stack/templates/_traefik.j2"

# The container-side paths Traefik writes certificates to. Changing either
# value is a breaking change requiring a documented migration — the existing
# store must be moved on every deployed host or all certs are re-issued.
HTTP01_STORE = "/etc/traefik/acme.json"
DNS01_STORE = "/etc/traefik/acme-dns.json"


def _render_traefik_yml(*, dns_challenge: bool) -> dict:
    """Render the static config and parse it as YAML."""
    env = make_ansible_env(_TRAEFIK_TEMPLATE_DIR)
    rendered = env.get_template("traefik.yml.j2").render(
        ansible_managed="managed by bay",
        letsencrypt_email="ops@example.com",
        traefik_docker_network="bay",
        traefik_metrics_enabled=False,
        traefik_access_log_fields_headers={},
        traefik_dns_challenge_enabled=dns_challenge,
        traefik_dns_resolver_name="tailnet",
        traefik_dns_provider="cloudflare",
        traefik_acme_dns_email="dns@example.com",
    )
    return yaml.safe_load(rendered)


def _resolver_stores(config: dict) -> dict[str, str]:
    """Map resolver name -> its ACME storage path."""
    resolvers = config["certificatesResolvers"]
    stores = {}
    for name, block in resolvers.items():
        # Traefik requires the challenge config to sit under an `acme` key.
        assert "acme" in block, f"resolver {name!r} has no `acme` block"
        stores[name] = block["acme"]["storage"]
    return stores


def _mount_targets(text: str) -> set[str]:
    """Container-side targets of any /etc/traefik/*.json volume mount."""
    return set(re.findall(r"(/etc/traefik/[\w.-]+\.json)", text))


# ── Role defaults: host-side paths ──────────────────────────────────────

def test_defaults_declare_acme_store_filenames():
    defaults = yaml.safe_load(_TRAEFIK_DEFAULTS.read_text())
    assert defaults["traefik_acme_path"].endswith("/acme.json")
    assert defaults["traefik_acme_dns_path"].endswith("/acme-dns.json")


def test_defaults_place_stores_under_stack_dir():
    """Both stores must live in the stack dir so backup/restore picks them up."""
    defaults = _TRAEFIK_DEFAULTS.read_text()
    assert 'traefik_acme_path: "{{ stack_dir }}/acme.json"' in defaults
    assert 'traefik_acme_dns_path: "{{ stack_dir }}/acme-dns.json"' in defaults


# ── Static config: container-side storage ───────────────────────────────

def test_http01_resolver_storage_is_pinned():
    stores = _resolver_stores(_render_traefik_yml(dns_challenge=False))
    assert stores == {"letsencrypt": HTTP01_STORE}


def test_dns01_resolver_storage_is_pinned():
    stores = _resolver_stores(_render_traefik_yml(dns_challenge=True))
    assert stores == {"letsencrypt": HTTP01_STORE, "tailnet": DNS01_STORE}


def test_dns01_resolver_is_absent_when_challenge_disabled():
    """A non-ingress host must not declare a resolver it has no token for."""
    stores = _resolver_stores(_render_traefik_yml(dns_challenge=False))
    assert "tailnet" not in stores


# ── The invariant: every storage path is actually mounted ───────────────

@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("reconciler (live deploy path)", _BUILD_SPECS),
        ("compose partial", _TRAEFIK_PARTIAL),
    ],
)
def test_every_resolver_store_is_backed_by_a_mount(label, source):
    """Certificates written to an unmounted path are lost on recreate."""
    stores = set(_resolver_stores(_render_traefik_yml(dns_challenge=True)).values())
    mounts = _mount_targets(source.read_text())
    missing = stores - mounts
    assert not missing, (
        f"{label}: resolver storage {sorted(missing)} has no volume mount in "
        f"{source.relative_to(_ROOT)} — certs would not survive a recreate"
    )


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("reconciler (live deploy path)", _BUILD_SPECS),
        ("compose partial", _TRAEFIK_PARTIAL),
    ],
)
def test_mounts_use_the_role_default_paths_on_the_host_side(label, source):
    """The host side must fall back to the documented stack_dir filenames."""
    text = source.read_text()
    assert "traefik_acme_path" in text
    assert "traefik_acme_dns_path" in text
    # Inline fallbacks must not drift from roles/traefik/defaults/main.yml.
    for fallback in re.findall(r"stack_dir ~ '(/acme[\w.-]*\.json)'", text):
        assert fallback in ("/acme.json", "/acme-dns.json"), (
            f"{label}: unexpected ACME fallback path {fallback!r}"
        )


def test_reconciler_and_compose_partial_mount_the_same_targets():
    """The two deploy paths must produce identical cert mounts (M85 parity)."""
    assert _mount_targets(_BUILD_SPECS.read_text()) == _mount_targets(
        _TRAEFIK_PARTIAL.read_text()
    )
