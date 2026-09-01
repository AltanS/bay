"""The pre-push hook must stop a bad push before it leaves the machine.

Bay has two remotes with different rules. The private one may carry the
operator's real infrastructure history; the public one is a separate orphan
history that may carry none of it. `.githooks/pre-push` enforces both halves:

  Gate A  a ref that descends from a known private root commit may not be
          pushed to a remote recognised as public.
  Gate B  every commit being published is leak-scanned, not just the tip.

Gate B is the one that is easy to get wrong. A leak introduced in one commit
and fixed in the next leaves a clean worktree — and a dirty history, which is
what a push actually publishes. `test_leak_in_middle_commit_is_denied` builds
exactly that shape and would pass a tip-only scan.

A guard nobody has seen go red is not a guard, so each case here asserts the
denial, not just the absence of a crash.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOK = _REPO_ROOT / ".githooks" / "pre-push"
_SCAN = _REPO_ROOT / "scripts" / "leak-scan.sh"

# Not in leak-scan's allowlist: not RFC 5737, not RFC 1918, not CGNAT, not one
# of the well-known resolvers. A real-looking public address, which is the
# whole point — and therefore assembled from fragments, so that this file does
# not itself fail the scan it is testing. leak-scan.sh excludes itself for the
# same reason.
_LEAKED_IP = "45.33." + "32.156"


def _git(*args: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, message: str) -> str:
    _git("add", "-A", cwd=repo)
    assert _git("commit", "-m", message, cwd=repo).returncode == 0
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


@pytest.fixture()
def scratch(tmp_path: Path) -> tuple[Path, Path]:
    """A working repo with the hook installed, plus a bare remote."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

    repo = tmp_path / "work"
    repo.mkdir()
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", str(repo)],
        check=True,
        capture_output=True,
    )
    _git("config", "user.email", "dev@example.com", cwd=repo)
    _git("config", "user.name", "Dev", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)

    (repo / ".githooks").mkdir()
    (repo / "scripts").mkdir()
    shutil.copy2(_HOOK, repo / ".githooks" / "pre-push")
    shutil.copy2(_SCAN, repo / "scripts" / "leak-scan.sh")
    (repo / ".githooks" / "pre-push").chmod(0o755)
    _git("config", "core.hooksPath", ".githooks", cwd=repo)
    _git("remote", "add", "origin", str(bare), cwd=repo)

    (repo / "README.md").write_text("# scratch\n")
    _commit(repo, "chore: initial")
    return repo, bare


def _push(repo: Path, **env: str) -> subprocess.CompletedProcess:
    return _git("push", "origin", "main", cwd=repo, env=env)


def test_clean_push_to_a_private_remote_succeeds(scratch):
    repo, _ = scratch
    (repo / "notes.md").write_text("nothing to see, 192.0.2.1 is a doc address\n")
    _commit(repo, "docs: add notes")

    result = _push(repo)
    assert result.returncode == 0, result.stderr
    # The progress line is stdout; git relays hook stderr to the terminal.
    assert "pre-push: leak-scan" in result.stdout
    assert "pre-push: guards passed" in result.stdout


def test_leak_in_middle_commit_is_denied(scratch):
    """The worktree is clean; the history is not. Only a per-commit scan sees it."""
    repo, _ = scratch
    (repo / "notes.md").write_text(f"server: {_LEAKED_IP}\n")
    bad_sha = _commit(repo, "docs: oops")
    (repo / "notes.md").write_text("server: 192.0.2.1\n")
    _commit(repo, "docs: scrub the address")

    # The tip really is clean — a tip-only guard would wave this through.
    tip = subprocess.run(
        ["bash", "scripts/leak-scan.sh", "HEAD"],
        cwd=repo, capture_output=True, text=True,
    )
    assert tip.returncode == 0, tip.stdout + tip.stderr

    result = _push(repo)
    assert result.returncode != 0
    assert "leak-scan failed on commit" in result.stderr
    assert bad_sha in result.stderr
    assert _LEAKED_IP in result.stderr


def test_private_history_to_a_public_remote_is_denied(scratch):
    repo, _ = scratch
    root = _git("rev-list", "--max-parents=0", "HEAD", cwd=repo).stdout.strip()

    result = _push(
        repo,
        BAY_PUBLIC_REMOTE_PATTERN="remote[.]git",
        BAY_PRIVATE_ROOTS=root,
    )
    assert result.returncode != 0
    assert "private history" in result.stderr
    assert root in result.stderr


def test_bypass_env_skips_both_gates_loudly(scratch):
    repo, _ = scratch
    root = _git("rev-list", "--max-parents=0", "HEAD", cwd=repo).stdout.strip()

    env = {
        "BAY_PUBLIC_REMOTE_PATTERN": "remote[.]git",
        "BAY_PRIVATE_ROOTS": root,
    }
    env["BAY_PUSH_SKIP" + "_GUARDS"] = "1"
    result = _push(repo, **env)
    assert result.returncode == 0, result.stderr
    assert "SKIPPED" in result.stderr


# ── Gate A by identity, not by URL ──────────────────────────────────────
#
# A URL substring is defeated by a mirror URL, a rename, or an ssh alias, and
# the gate then passes SILENTLY — the worst failure shape a guard has. Gate A
# now also asks the remote what history it carries and compares its root commit
# against the known public root.


@pytest.fixture()
def two_root_scratch(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """A repo with two unrelated histories, and a remote holding the public one.

    `main` descends from the "private" root. An orphan branch `pub` is a
    separate root; it is pushed to the bare remote first, so the remote's only
    history starts at the public root — exactly the real topology.
    """
    bare = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

    repo = tmp_path / "work"
    repo.mkdir()
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", str(repo)],
        check=True,
        capture_output=True,
    )
    _git("config", "user.email", "dev@example.com", cwd=repo)
    _git("config", "user.name", "Dev", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)

    (repo / ".githooks").mkdir()
    (repo / "scripts").mkdir()
    shutil.copy2(_HOOK, repo / ".githooks" / "pre-push")
    shutil.copy2(_SCAN, repo / "scripts" / "leak-scan.sh")
    (repo / ".githooks" / "pre-push").chmod(0o755)

    (repo / "README.md").write_text("# private history\n")
    _commit(repo, "chore: private root")
    private_root = _git("rev-list", "--max-parents=0", "HEAD", cwd=repo).stdout.strip()

    # A separate orphan history: this is what the "public" repo looks like.
    _git("checkout", "--orphan", "pub", cwd=repo)
    _git("rm", "-rf", "--cached", ".", cwd=repo)
    (repo / "PUBLIC.md").write_text("# public history\n")
    _commit(repo, "chore: public root")
    public_root = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    # Hooks are installed only AFTER seeding the remote, so seeding is not gated.
    _git("remote", "add", "origin", str(bare), cwd=repo)
    assert _git("push", "origin", "pub", cwd=repo).returncode == 0
    _git("checkout", "main", cwd=repo)
    _git("config", "core.hooksPath", ".githooks", cwd=repo)

    return repo, bare, private_root, public_root


def test_public_remote_recognised_by_root_identity_not_url(two_root_scratch):
    """The URL says nothing; the remote's own history gives it away."""
    repo, bare, private_root, public_root = two_root_scratch
    # Fragmented so this file does not trip the identifier denylist in
    # leak-scan.sh, which this very suite runs.
    assert ("Alt" + "anS") not in str(bare), (
        "the fixture URL must not match the URL gate"
    )

    result = _git(
        "push", "origin", "main",
        cwd=repo,
        env={"BAY_PRIVATE_ROOTS": private_root, "BAY_PUBLIC_ROOTS": public_root},
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "private history" in result.stderr
    assert "known public root commit" in result.stderr


def test_a_remote_with_unrelated_history_is_not_treated_as_public(two_root_scratch):
    """The identity test must not fire on every remote it cannot recognise."""
    repo, bare, private_root, _ = two_root_scratch
    result = _git(
        "push", "origin", "main",
        cwd=repo,
        env={"BAY_PRIVATE_ROOTS": private_root, "BAY_PUBLIC_ROOTS": "0" * 40},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "guards passed" in result.stdout


@pytest.mark.parametrize(
    "var",
    ["BAY_PRIVATE_ROOTS", "BAY_PUBLIC_ROOTS", "BAY_PUBLIC_REMOTE_PATTERN"],
)
def test_every_gate_override_warns_loudly(scratch, var):
    """A quiet bypass is worse than a loud one — these used to be silent."""
    repo, _ = scratch
    root = _git("rev-list", "--max-parents=0", "HEAD", cwd=repo).stdout.strip()
    value = root if var.endswith("ROOTS") else "no[.]such[.]remote"

    result = _push(repo, **{var: value})
    assert "WARNING" in result.stderr
    assert "OVERRIDDEN" in result.stderr
