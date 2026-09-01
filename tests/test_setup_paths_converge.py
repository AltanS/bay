"""`make bay:setup` and `.bay/bootstrap.sh` must land on the same tree.

The generated Makefile used to carry its own copy of the bootstrap: clone, pin
`.bay-version`, symlink `group_vars`, `uv sync`, install Galaxy roles and
collections. It had already drifted — it never created `bin/bay`, so the
documented `make bay:setup` then `bin/bay setup` sequence could not work. The
target is now a clone plus `exec .bay/bootstrap.sh`, and these tests keep the
duplication from growing back.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAKEFILES = ("src/bay_cli/wizard/templates/makefile.j2", "example/Makefile")

#: Steps that belong to bootstrap.sh alone. Any of these in a Makefile is a
#: second implementation of the same thing.
DUPLICATED_STEPS = ("uv sync", "ansible-galaxy", ".bay-version", "ln -sfn")


@pytest.mark.parametrize("rel", _MAKEFILES)
def test_makefile_delegates_to_bootstrap(rel: str) -> None:
    text = (_REPO_ROOT / rel).read_text()
    assert "bootstrap.sh" in text, f"{rel} does not call bootstrap.sh"
    for step in DUPLICATED_STEPS:
        assert step not in text, (
            f"{rel} re-implements '{step}' — that belongs to bootstrap.sh"
        )


@pytest.mark.parametrize("rel", _MAKEFILES)
def test_bay_repo_defaults_to_https(rel: str) -> None:
    text = (_REPO_ROOT / rel).read_text()
    match = re.search(r"^BAY_REPO := (.+)$", text, re.MULTILINE)
    assert match, f"{rel} has no BAY_REPO default"
    assert match.group(1).strip() == "https://github.com/AltanS/bay.git"


def test_example_makefile_matches_the_generated_one() -> None:
    """The example scaffold and the wizard template are the same file."""
    template = (_REPO_ROOT / "src/bay_cli/wizard/templates/makefile.j2").read_text()
    example = (_REPO_ROOT / "example/Makefile").read_text()
    assert template == example


def test_makefile_setup_target_creates_bin_bay(tmp_path: Path) -> None:
    """End to end: `make bay:setup` alone must leave a working bin/bay.

    Clones this working tree over a local path (no network, no SSH key) and
    runs only the clone half of the target — the bootstrap half is covered by
    tests/test_bootstrap.sh, which is far slower because it syncs Ansible.
    """
    project = tmp_path / "my-infra"
    project.mkdir()
    (project / "Makefile").write_text(
        (_REPO_ROOT / "example" / "Makefile").read_text()
    )

    # Stub bootstrap: assert the target reaches it, with the clone in place.
    fake_repo = tmp_path / "fake-bay"
    fake_repo.mkdir()
    (fake_repo / "bootstrap.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'cd "$(dirname "$SCRIPT_DIR")"\n'
        "mkdir -p bin\n"
        'cp "$SCRIPT_DIR/wrapper" bin/bay\n'
        "chmod +x bin/bay\n"
    )
    (fake_repo / "wrapper").write_text(
        (_REPO_ROOT / "scripts" / "bin-bay-wrapper.sh").read_text()
    )
    (fake_repo / "bootstrap.sh").chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=fake_repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=fake_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.test", "-c", "user.name=t",
         "commit", "-qm", "init"],
        cwd=fake_repo, check=True,
    )

    result = subprocess.run(
        ["make", "bay:setup", f"BAY_REPO={fake_repo}"],
        cwd=project, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    wrapper = project / "bin" / "bay"
    assert wrapper.is_file(), "make bay:setup left no bin/bay wrapper"
    assert wrapper.stat().st_mode & 0o111
    assert wrapper.read_text() == (
        _REPO_ROOT / "scripts" / "bin-bay-wrapper.sh"
    ).read_text()


def test_makefile_setup_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "my-infra"
    project.mkdir()
    (project / "Makefile").write_text(
        (_REPO_ROOT / "example" / "Makefile").read_text()
    )
    (project / ".bay").mkdir()
    result = subprocess.run(
        ["make", "bay:setup"], cwd=project, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "Already initialized" in result.stdout
