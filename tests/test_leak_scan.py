"""`scripts/leak-scan.sh` must actually go RED.

This repo has been bitten by a green guard once already: the first version of
leak-scan matched a lowercase denylist case-sensitively against occurrences
that were all uppercase, and had no credential check at all, so it reported
"clean" for its entire life while a live API key sat in `tests/`. It had no
test. This is that test.

Every case below plants ONE shape into an otherwise clean scratch repo, runs
the real script against it, and asserts a non-zero exit AND that the offending
value is named in the output. The re-injection discipline: a guard nobody has
watched go red is not a guard.

Reproduce any single case by hand with, e.g.:

    tmp=$(mktemp -d) && cd "$tmp" && git init -q . && mkdir scripts \\
      && cp ~/projects/bay-public/scripts/leak-scan.sh scripts/ \\
      && printf 'key = "kaqs5jwr4rzaug5jlv5cvj9agu8olh4s05clrx5t"\\n' > planted.py \\
      && git add -A && bash scripts/leak-scan.sh; echo "exit=$?"

Every planted value in this file is ASSEMBLED FROM FRAGMENTS. A literal here
would make this file fail the very scan it is testing — which is also why
leak-scan.sh excludes itself.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAN = _REPO_ROOT / "scripts" / "leak-scan.sh"

# ── planted shapes, all assembled from fragments ────────────────────────
# 40 chars of [a-z0-9]. Shannon entropy ~4.26 bits/char. This is the shape
# tier (b2) was added for: tier (b) requires an uppercase character, so this
# used to walk straight through.
LOWER_KEY = "kaqs5jwr4rzaug5jlv5" + "cvj9agu8olh4s05clrx5t"
# Mixed case + digits: the original tier (b).
MIXED_KEY = "aB3xY9zQ7wE2rT5yU8iO" + "1pA4sD6fG0hJ"
# 40 hex chars: tier (c).
HEX_BLOB = "9f2c1ab47de03516" + "82bc4409ff17ea2d5c60b731"
# Section 1 denylist.
IDENTIFIER = "test" + "lab"
# Section 2: a real-looking public address, not RFC 5737 / RFC 1918 / CGNAT
# and not a well-known resolver.
LEAKED_IP = "45.33." + "32.156"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture()
def scratch(tmp_path: Path) -> Path:
    """A clean git repo carrying a copy of the real scan script."""
    repo = tmp_path / "work"
    (repo / "scripts").mkdir(parents=True)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", str(repo)],
        check=True,
        capture_output=True,
    )
    _git("config", "user.email", "dev@example.com", cwd=repo)
    _git("config", "user.name", "Dev", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    shutil.copy2(_SCAN, repo / "scripts" / "leak-scan.sh")
    (repo / "README.md").write_text(
        "# scratch\n\nDocs use 192.0.2.1 and example.com, as they should.\n"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "chore: initial", cwd=repo)
    return repo


def _scan(repo: Path, ref: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["bash", "scripts/leak-scan.sh"]
    if ref:
        cmd.append(ref)
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True)


def _plant(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content)
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", f"test: plant {name}", cwd=repo)


def test_clean_tree_is_green(scratch):
    """The negative control. Without it, a scan that always fails looks perfect."""
    result = _scan(scratch)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout


def test_lowercase_digit_key_is_red(scratch):
    """The S14 gap: [a-z0-9] only, so tier (b)'s uppercase requirement misses it."""
    _plant(scratch, "planted.py", f'API_KEY = "{LOWER_KEY}"\n')
    result = _scan(scratch)
    assert result.returncode != 0, result.stdout + result.stderr
    assert LOWER_KEY in result.stdout + result.stderr


def test_mixed_case_blob_is_red(scratch):
    _plant(scratch, "planted.py", f'TOKEN = "{MIXED_KEY}"\n')
    result = _scan(scratch)
    assert result.returncode != 0
    assert MIXED_KEY in result.stdout + result.stderr


def test_bare_hex_blob_is_red(scratch):
    _plant(scratch, "planted.py", f'SECRET = "{HEX_BLOB}"\n')
    result = _scan(scratch)
    assert result.returncode != 0
    assert HEX_BLOB in result.stdout + result.stderr


def test_hex_with_digest_context_is_green(scratch):
    """A `sha256:` digest is a pin, not a secret — the context allowlist."""
    _plant(scratch, "planted.yml", f"image: example/app@sha256:{HEX_BLOB}\n")
    result = _scan(scratch)
    assert result.returncode == 0, result.stdout + result.stderr


def test_denylisted_identifier_is_red(scratch):
    _plant(scratch, "planted.md", f"Deployed for {IDENTIFIER} last week.\n")
    result = _scan(scratch)
    assert result.returncode != 0
    assert IDENTIFIER in (result.stdout + result.stderr).lower()


def test_denylisted_identifier_is_red_in_any_case(scratch):
    """The original bug: a lowercase denylist grepped case-sensitively."""
    _plant(scratch, "planted.md", f"Deployed for {IDENTIFIER.upper()} last week.\n")
    result = _scan(scratch)
    assert result.returncode != 0


def test_real_public_ip_is_red(scratch):
    _plant(scratch, "planted.yml", f"ansible_host: {LEAKED_IP}\n")
    result = _scan(scratch)
    assert result.returncode != 0
    assert LEAKED_IP in result.stdout + result.stderr


def test_documentation_ip_stays_green(scratch):
    _plant(scratch, "planted.yml", "ansible_host: 203.0.113.9\n")
    result = _scan(scratch)
    assert result.returncode == 0, result.stdout + result.stderr


def test_scan_of_a_past_ref_sees_a_leak_the_worktree_no_longer_has(scratch):
    """The pre-push hook depends on this: history, not worktree."""
    _plant(scratch, "planted.py", f'API_KEY = "{LOWER_KEY}"\n')
    bad = _git("rev-parse", "HEAD", cwd=scratch).stdout.strip()
    _plant(scratch, "planted.py", 'API_KEY = "redacted"\n')

    assert _scan(scratch).returncode == 0
    stale = _scan(scratch, bad)
    assert stale.returncode != 0
    assert LOWER_KEY in stale.stdout + stale.stderr
