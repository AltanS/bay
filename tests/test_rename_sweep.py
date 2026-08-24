"""Rename sweep guard for the Argo -> Bay rename.

Walks every file tracked by `git ls-files` and fails on any occurrence of the
substring "argo" (case-insensitive) outside an explicit allowlist. This is the
guard described in `docs/rename-map.md`: it ships *before* any file
is renamed, so it is red-by-default (via PENDING) rather than being added last
and never having verifiably caught anything (see the leak-scan.sh incident in
project memory: a guard nobody ever saw go red is not a guard).

As each later spec (S02-S05) renames a surface, delete the matching entry from
PENDING. The milestone is done when PENDING is empty and test_no_stray_argo
runs with zero exclusions.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Case-insensitive, deliberately a plain substring match on "argo". We checked
# the actual inventory (git grep) and found no unrelated English words or
# identifiers that merely embed "argo" -- every hit is a real Argo->Bay
# rename candidate -- so a word-boundary regex would add complexity without
# excluding anything real.
_ARGO_RE = re.compile(r"argo", re.IGNORECASE)

# The tag that marks a line as an intentional, reviewed back-compat reference
# to the old name (e.g. a dual-read shim, an alias, or migration-role code
# that must literally name the old artifact to remove/migrate it). Matching
# is case-sensitive and exact -- this is a mechanism, not free text.
_LEGACY_TAG = "legacy-argo"

# Exact paths / path prefixes excused from the sweep entirely. Keep this
# short and deliberate -- do NOT add directories here to make the guard pass;
# use PENDING (below) for surfaces that are legitimately not renamed yet.
ALLOWLIST: tuple[str, ...] = (
    "CHANGELOG.md",
    # Generated from the CLI by scripts/gen_skill.py, which strips the
    # trailing `# legacy-argo:` tags its sources carry. The real surface is
    # src/bay_cli/, which IS swept and IS tagged — guarding the artifact too
    # would only ever fail for a violation already excused upstream.
    "SKILL.md",
    "docs/rename-map.md",
    "tests/test_rename_sweep.py",
    "vendor/",
)

# Path prefixes for surfaces not yet renamed. Each entry names the spec that
# will remove it. A file matching one of these prefixes is excused from
# test_no_stray_argo, but test_pending_entries_still_needed asserts every
# entry here still excuses at least one real violation -- so a stale entry
# (the surface already got renamed) fails the build instead of silently
# widening the allowlist forever.
PENDING: tuple[str, ...] = ()


@dataclass(frozen=True)
class Violation:
    path: str
    line_no: int
    line: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line_no}: {self.line.strip()}"


def _is_allowlisted(path: str) -> bool:
    return any(path == entry or path.startswith(entry) for entry in ALLOWLIST)


def _is_pending(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in PENDING)


def _tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def scan(root: Path, *, excuse_pending: bool = True) -> list[Violation]:
    """Scan all git-tracked files under root for stray "argo" occurrences.

    A line is flagged unless:
    - the file path is in ALLOWLIST, or
    - the file path matches a PENDING prefix (only when excuse_pending is
      True -- the "still needed" self-check calls this with False to see
      the raw violation set), or
    - the line itself contains the `legacy-argo` tag.
    """
    violations: list[Violation] = []
    for rel_path in _tracked_files(root):
        if _is_allowlisted(rel_path):
            continue
        if excuse_pending and _is_pending(rel_path):
            continue

        full_path = root / rel_path
        try:
            raw = full_path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, OSError):
            # Deleted-but-tracked (rare race) or a submodule gitlink entry.
            continue

        if _looks_binary(raw[:8192]):
            continue

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue

        for line_no, line in enumerate(text.splitlines(), start=1):
            if _LEGACY_TAG in line:
                continue
            if _ARGO_RE.search(line):
                violations.append(Violation(rel_path, line_no, line))

    return violations


def test_no_stray_argo() -> None:
    violations = scan(REPO_ROOT)
    if violations:
        listing = "\n".join(f"  {v}" for v in violations[:50])
        more = "" if len(violations) <= 50 else f"\n  ... and {len(violations) - 50} more"
        raise AssertionError(
            f"{len(violations)} stray 'argo' occurrence(s) outside PENDING/ALLOWLIST:\n"
            f"{listing}{more}\n"
            "Either add the identifier to docs/rename-map.md and rename it, "
            "or (if the surface is legitimately not renamed yet) confirm it's "
            "covered by an existing PENDING prefix."
        )


def test_pending_entries_still_needed() -> None:
    """Every PENDING prefix must still excuse at least one real violation.

    Once a spec renames a surface, the matching PENDING entry excuses
    nothing -- and must be deleted, not left around as dead weight. This
    test fails the build until that happens, so PENDING can only shrink.
    """
    raw_violations = scan(REPO_ROOT, excuse_pending=False)
    stale: list[str] = []
    for prefix in PENDING:
        if not any(v.path.startswith(prefix) for v in raw_violations):
            stale.append(prefix)

    assert not stale, (
        "PENDING entries that no longer excuse any violation (delete them): "
        f"{stale}"
    )


def test_reinject_detected(tmp_path: Path) -> None:
    """Re-injection self-test: a fresh tree with one stray identifier must be
    flagged by scan(). This is the guard-actually-works check called out in
    leak-scan.sh passed for its whole life without anyone ever
    seeing it go red.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)

    stray_file = tmp_path / "stray.py"
    stray_file.write_text("argo_leftover = 1\n")

    subprocess.run(["git", "add", "stray.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add stray"], cwd=tmp_path, check=True)

    violations = scan(tmp_path)
    assert any("argo_leftover" in v.line for v in violations), (
        f"scan() failed to flag a freshly-injected stray identifier; got: {violations}"
    )
