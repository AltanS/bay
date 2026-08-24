"""Enforce the alert registry against reality, in both directions.

`alerts/registry.yml` is the single source of truth for Bay's alert surface —
the CLI enumerates it, the docs are generated from it, and recipient routing is
resolved against it at render time. A registry that drifts from the call sites
is worse than no registry: it makes `bay alerts list` confidently wrong.

Two directions, both enforced:

  * every literal ID emitted by a call site is declared in the registry
  * every registry entry is emitted by at least one call site

The second direction is what stops the registry accumulating aspirational
entries for alerts nobody sends.

**This only works because literal IDs are a linted invariant.** If a call site
could pass a variable, the scan below would silently under-report and the whole
guard would go quietly green — so `test_no_call_site_passes_a_non_literal_id`
is load-bearing, not stylistic.

Call-site conventions (all three are scanned):

  shell    bay_notify <id> "<message>"        # bare literal, no quotes/$
           notify_build <id> "<message>"       # rebuild.sh's correlation wrapper
  python   send_alert("<id>", message)
  ansible  _bay_alert_id: <id>                # set_fact beside the uri tasks
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REGISTRY = _REPO_ROOT / "alerts" / "registry.yml"
_ROLES = _REPO_ROOT / "roles"

_LADDER = ("debug", "info", "warn", "critical")
_REQUIRED_FIELDS = {
    "level",
    "source",
    "summary",
    "enabled_by_default",
    "rate_limited",
}

# An ID is <domain>.<event>: lowercase, digits and underscores within a segment.
_ID = r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+"

_SHELL_EMIT = re.compile(rf"\b(?:bay_notify|notify_build)\s+({_ID})(?=\s|$)")
_PY_EMIT = re.compile(rf"\bsend_alert\(\s*[\"']({_ID})[\"']")
_ANSIBLE_EMIT = re.compile(rf"_bay_alert_id:\s*[\"']?({_ID})[\"']?")

# A first argument that starts with a quote, a dollar, or an opening brace is
# not a literal. This is the invariant the two-way check rests on.
_SHELL_NON_LITERAL = re.compile(r"\b(?:bay_notify|notify_build)\s+(?![a-z])(\S)")
_PY_NON_LITERAL = re.compile(r"(?<!def )\bsend_alert\(\s*(?![\"'])(\w)")

# The single sanctioned opt-out: a wrapper forwarding an ID it was passed.
# Explicit and greppable — `grep -rn "bay-alert-id: forwarder" roles/` lists
# every one of them.
_FORWARDER_MARKER = "bay-alert-id: forwarder"

# Files that define the emitters themselves, rather than calling them. Their
# `bay_notify` mentions are definitions and documentation, not call sites.
_DEFINITION_FILES = {
    "_notify.sh.j2",
    "bay_alert.py",
}


def _scan_files() -> list[Path]:
    """Every role file that could contain an alert call site."""
    out: list[Path] = []
    for pattern in ("**/*.j2", "**/*.py", "**/*.yml"):
        for path in _ROLES.glob(pattern):
            if path.name in _DEFINITION_FILES:
                continue
            if "__pycache__" in path.parts:
                continue
            out.append(path)
    return out


def _emitted_ids() -> dict[str, list[str]]:
    """Map alert ID -> the files that emit it."""
    found: dict[str, list[str]] = {}
    for path in _scan_files():
        text = path.read_text(errors="replace")
        for pattern in (_SHELL_EMIT, _PY_EMIT, _ANSIBLE_EMIT):
            for match in pattern.finditer(text):
                rel = str(path.relative_to(_REPO_ROOT))
                found.setdefault(match.group(1), []).append(rel)
    return found


@pytest.fixture(scope="module")
def registry() -> dict:
    yaml = YAML(typ="safe")
    with _REGISTRY.open() as handle:
        data = yaml.load(handle)
    assert isinstance(data, dict) and data, "registry.yml is empty or not a mapping"
    return data


# ── Registry shape ───────────────────────────────────────────────────────────


def test_every_entry_has_all_required_fields(registry):
    incomplete = {
        alert_id: sorted(_REQUIRED_FIELDS - set(fields))
        for alert_id, fields in registry.items()
        if not _REQUIRED_FIELDS <= set(fields or {})
    }
    assert not incomplete, f"Registry entries missing fields: {incomplete}"


def test_every_level_is_on_the_ladder(registry):
    """A level off the ladder cannot be compared against a recipient min_level."""
    bad = {
        alert_id: fields["level"]
        for alert_id, fields in registry.items()
        if fields.get("level") not in _LADDER
    }
    assert not bad, (
        f"Registry levels outside the ladder {_LADDER}: {bad}. "
        "The ladder is defined in docs/build-pipeline-observability-contract.md."
    )


def test_every_id_follows_the_naming_convention(registry):
    bad = [a for a in registry if not re.fullmatch(_ID, a)]
    assert not bad, (
        f"Alert IDs must be <domain>.<event>, lowercase: {bad}. "
        "The scanners in this file match that shape — an off-convention ID "
        "would be invisible to the drift check."
    )


def test_summaries_are_one_line_and_present(registry):
    bad = [
        alert_id
        for alert_id, fields in registry.items()
        if not str(fields.get("summary", "")).strip()
        or "\n" in str(fields.get("summary", "")).strip()
    ]
    assert not bad, f"Summaries must be one non-empty line (rendered into docs): {bad}"


# ── Drift, both directions ───────────────────────────────────────────────────


def test_every_emitted_id_is_declared(registry):
    emitted = _emitted_ids()
    undeclared = {
        alert_id: sorted(set(files))
        for alert_id, files in emitted.items()
        if alert_id not in registry
    }
    assert not undeclared, (
        "Call sites emit alert IDs that alerts/registry.yml does not declare:\n"
        + "\n".join(f"  {a} <- {f}" for a, f in undeclared.items())
    )


def test_every_declared_id_is_emitted(registry):
    """Stops the registry accumulating alerts nobody actually sends."""
    emitted = set(_emitted_ids())
    # `alerts.test` is emitted by the CLI (src/), not a role template.
    exempt = {"alerts.test"}
    orphans = sorted(set(registry) - emitted - exempt)
    assert not orphans, (
        "Registry entries with no call site:\n  "
        + "\n  ".join(orphans)
        + "\nRemove them, or add the missing emitter."
    )


# ── The invariant the drift check depends on ─────────────────────────────────


def test_no_call_site_passes_a_non_literal_id():
    """A variable ID would make the scan under-report and the guard go green.

    This is the reason the two-way drift check can be trusted at all.
    """
    violations: list[str] = []
    for path in _scan_files():
        text = path.read_text(errors="replace")
        for num, line in enumerate(text.splitlines(), start=1):
            # Wrappers that forward an ID they were handed (notify_build, and
            # bay_notify itself) are the one legitimate non-literal. They opt
            # out explicitly and greppably — never silently.
            if _FORWARDER_MARKER in line:
                continue
            if _SHELL_NON_LITERAL.search(line) or _PY_NON_LITERAL.search(line):
                rel = path.relative_to(_REPO_ROOT)
                violations.append(f"{rel}:{num}: {line.strip()[:80]}")
    assert not violations, (
        "Alert call sites must pass a LITERAL alert ID as the first argument.\n"
        "A variable defeats the registry drift check silently.\n  "
        + "\n  ".join(violations)
    )


def test_scanner_actually_finds_call_sites():
    """Guard the guard: an empty scan would make every drift test vacuously pass.

    If a refactor renames the emitters, the two-way checks above would go green
    on zero data. This asserts the scan is finding real work.
    """
    emitted = _emitted_ids()
    assert len(emitted) >= 20, (
        f"Only {len(emitted)} alert call sites found — the scanners are probably "
        "no longer matching the emitter names. Check _SHELL_EMIT / _PY_EMIT / "
        "_ANSIBLE_EMIT against the current call-site convention."
    )


# ── Generated docs ───────────────────────────────────────────────────────


def test_alert_table_in_docs_is_in_sync_with_the_registry():
    """A hand-copied registry table in prose is a second source of truth.

    Regenerating must produce no diff, so the docs cannot silently fall behind
    a new alert.
    """
    import subprocess

    doc = _REPO_ROOT / "docs" / "alerting.md"
    before = doc.read_text()
    subprocess.run(
        ["uv", "run", "python", "scripts/gen_alert_docs.py"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    )
    after = doc.read_text()
    if before != after:
        doc.write_text(before)
    assert before == after, (
        "docs/alerting.md is out of sync with alerts/registry.yml. "
        "Run `make docs-alerts` and commit the result."
    )


def test_docs_mention_every_registry_alert(registry):
    doc = (_REPO_ROOT / "docs" / "alerting.md").read_text()
    missing = [a for a in registry if f"`{a}`" not in doc]
    assert not missing, f"alerts absent from docs/alerting.md: {missing}"
