"""SKILL.md must stay in sync with the CLI and the doc index.

SKILL.md is what an agent reads to learn this framework; a stale command tree
in it is worse than no command tree, because it reads as authoritative. The
generated blocks are therefore checked the same way the alert table is: run
the generator, assert no diff.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL = _REPO_ROOT / "SKILL.md"


def test_skill_md_exists_with_frontmatter():
    text = _SKILL.read_text()
    assert text.startswith("---\n"), "SKILL.md needs YAML frontmatter"
    frontmatter = text.split("---\n", 2)[1]
    assert "name: bay" in frontmatter
    assert "description:" in frontmatter


def test_skill_md_is_in_sync_with_the_cli():
    """--check compares in memory, so verifying never writes to the tree.

    --frozen because a plain `uv run` may re-resolve dependencies, and an
    environment failure that reads as documentation drift wastes an
    afternoon.
    """
    result = subprocess.run(
        ["uv", "run", "--frozen", "python", "scripts/gen_skill.py", "--check"],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"{result.stdout}{result.stderr}\n"
        "SKILL.md is out of sync with the CLI or docs/README.md. "
        "Run `make docs-skill` and commit the result."
    )


def test_skill_md_covers_the_top_level_commands():
    """A generator that silently emitted an empty tree would still be 'in sync'."""
    text = _SKILL.read_text()
    for command in ("deploy", "provision", "validate", "vault edit", "gateway enroll"):
        assert f"`bin/bay {command}" in text, f"{command} missing from SKILL.md"


def test_generated_summaries_carry_no_source_comments():
    """Docstrings carry `# legacy-argo:`-style tags; SKILL.md ships to consumers.

    One of these leaked into the first cut of the file, so the stripping is
    asserted rather than assumed.
    """
    generated = _SKILL.read_text().split("BEGIN GENERATED CLI REFERENCE")[1]
    offenders = [
        line for line in generated.splitlines()
        if line.startswith("- `bin/bay") and " # " in line
    ]
    assert not offenders, f"source comments leaked into SKILL.md: {offenders}"


def test_doc_map_fails_loud_on_an_unindexed_doc():
    """The silent-skip failure mode this guard exists to prevent, seen going red.

    A doc added to docs/ but never linked from docs/README.md used to vanish
    from the map with a green regeneration.
    """
    stray = _REPO_ROOT / "docs" / "zz-unindexed-guard-probe.md"
    stray.write_text("# probe\n")
    try:
        result = subprocess.run(
            ["uv", "run", "--frozen", "python", "scripts/gen_skill.py", "--check"],
            cwd=_REPO_ROOT, capture_output=True, text=True,
        )
    finally:
        stray.unlink()
    assert result.returncode != 0, "generator accepted a doc missing from the index"
    assert "zz-unindexed-guard-probe" in result.stderr


def test_skill_flag_prints_the_file():
    result = subprocess.run(
        ["uv", "run", "--frozen", "bay", "--skill"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    )
    assert result.stdout == _SKILL.read_text()
