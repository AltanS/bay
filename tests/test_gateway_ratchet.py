"""Access-gateway adapter ratchet (M107).

Neutral framework code must talk to the active gateway through the adapter
contract — `gateway_enabled`, `gateway_bind_ip`, `gateway_cidrs`,
`gateway_identity_supported` — and never reach past it for the headscale
backend's own vocabulary.

This test is the enforcement mechanism, and it exists because the failure it
guards is SILENT. `headscale_server_tailnet_ip` carries a play-wide default of
100.64.0.1, so a role left unconverted does not error under
`access_gateway: none` — it quietly binds, pins or exempts a phantom address
that belongs to nobody. Review cannot reliably catch that; a red test can.

The allowlist below only ever SHRINKS. Adding a file to it is a regression and
should be argued for in review, not merged quietly. Removing one is progress.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Files permitted to name the headscale backend's own vocabulary. Everything
# here either IS the backend, or is the one place the adapter resolves it.
BACKEND_OWNED = {
    # The adapter itself — the single dispatch site, by design.
    "roles/access_gateway/defaults/main.yml",
    "roles/access_gateway/tasks/main.yml",
    # The headscale backend proper.
    "roles/headscale/",
    "roles/tailscale_node/",
    "roles/tailscale_register/",
    "roles/tailnet_identity/",
    # Backs up headscale's own state; gated on the backend being active.
    "roles/backup/tasks/main.yml",
    # Builds the headscale container spec; gated on the backend being active.
    "roles/container_lifecycle/tasks/build_specs.yml",
    "roles/deploy_stack/templates/_headscale.j2",
    # The cross-host resolvers. Role defaults and play vars are absent from
    # hostvars[<other_host>] (verified), so these two functions are the only
    # places allowed to fall back to the incumbent var name.
    "filter_plugins/bay_filters.py",
    "roles/crowdsec_allowlist/library/crowdsec_allowlist_sync.py",
    # CLI: reads consumer config to choose a backend and to gate commands.
    "src/bay_cli/",
    # Docs, examples, changelog and the wizard describe backends by name.
    "docs/",
    "example/",
    "CHANGELOG.md",
    "README.md",
    "tests/",
}

BACKEND_VOCABULARY = [
    re.compile(r"headscale_server_tailnet_ip"),
    re.compile(r"access_gateway[^\n]{0,40}==\s*'headscale'"),
    re.compile(r'access_gateway[^\n]{0,40}==\s*"headscale"'),
]


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line]


def _is_allowlisted(path: str) -> bool:
    return any(
        path == entry or path.startswith(entry)
        for entry in BACKEND_OWNED
    )


def test_backend_vocabulary_confined_to_allowlist():
    """No neutral file may name the headscale backend directly."""
    offenders: list[str] = []
    for rel in _tracked_files():
        if _is_allowlisted(rel):
            continue
        full = REPO / rel
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in BACKEND_VOCABULARY:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{rel}:{line_no}: {match.group(0)}")

    assert not offenders, (
        "Access-gateway backend vocabulary leaked outside the adapter (M107).\n"
        "Read gateway_bind_ip / gateway_enabled / gateway_cidrs instead:\n  "
        + "\n  ".join(offenders)
    )


def test_allowlist_entries_all_exist():
    """A stale allowlist entry hides a regression behind a path that moved."""
    missing = [e for e in BACKEND_OWNED if not (REPO / e.rstrip("/")).exists()]
    assert not missing, f"allowlist names paths that no longer exist: {missing}"


def test_allowlist_does_not_grow():
    """Pin the allowlist size so widening it is a deliberate, reviewed act."""
    assert len(BACKEND_OWNED) <= 18, (
        "The M107 ratchet allowlist grew. It is supposed to shrink only — "
        "convert the role to the adapter contract instead of allowlisting it."
    )
