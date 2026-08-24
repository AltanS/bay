"""Tests for the .claude/skills/bay/SKILL.md link created on install.

SKILL.md carries Claude-skill frontmatter, but it lives at the framework root
inside a gitignored .bay/ — no skill router looks there, so without this link
the frontmatter is decorative. `install`, `update` and `dev-unlink` all call
the helper, so it must be idempotent and must never clobber an operator's own
skill file.
"""

from __future__ import annotations

import os
from pathlib import Path

from bay_cli.commands.framework import _ensure_skill_link


def _make_consumer(tmp_path: Path, *, with_skill: bool = True) -> tuple[Path, Path]:
    """Return (consumer_root, framework_path) with .bay/ as a real directory."""
    root = tmp_path / "consumer"
    framework = root / ".bay"
    framework.mkdir(parents=True)
    if with_skill:
        (framework / "SKILL.md").write_text("---\nname: bay\n---\n")
    return root, framework


class TestEnsureSkillLink:
    def test_creates_link_pointing_through_the_framework_dir(self, tmp_path: Path) -> None:
        root, framework = _make_consumer(tmp_path)

        _ensure_skill_link(framework, root)

        link = root / ".claude" / "skills" / "bay" / "SKILL.md"
        assert link.is_symlink()
        # Relative, and routed through .bay/ rather than resolved — that is
        # what makes it follow a dev-link and survive the repo moving.
        assert os.readlink(link) == "../../../.bay/SKILL.md"
        assert link.read_text().startswith("---")

    def test_is_idempotent(self, tmp_path: Path) -> None:
        root, framework = _make_consumer(tmp_path)

        _ensure_skill_link(framework, root)
        link = root / ".claude" / "skills" / "bay" / "SKILL.md"
        before = os.readlink(link)
        _ensure_skill_link(framework, root)

        assert os.readlink(link) == before

    def test_repairs_a_link_pointing_elsewhere(self, tmp_path: Path) -> None:
        root, framework = _make_consumer(tmp_path)
        link = root / ".claude" / "skills" / "bay" / "SKILL.md"
        link.parent.mkdir(parents=True)
        os.symlink("../../../somewhere-else/SKILL.md", link)

        _ensure_skill_link(framework, root)

        assert os.readlink(link) == "../../../.bay/SKILL.md"

    def test_leaves_a_real_file_alone(self, tmp_path: Path) -> None:
        """An operator's own skill file is theirs — never overwrite it."""
        root, framework = _make_consumer(tmp_path)
        link = root / ".claude" / "skills" / "bay" / "SKILL.md"
        link.parent.mkdir(parents=True)
        link.write_text("my own skill\n")

        _ensure_skill_link(framework, root)

        assert not link.is_symlink()
        assert link.read_text() == "my own skill\n"

    def test_noop_when_framework_predates_skill_md(self, tmp_path: Path) -> None:
        """Consumers can pin an older framework; install must not fail there."""
        root, framework = _make_consumer(tmp_path, with_skill=False)

        _ensure_skill_link(framework, root)

        assert not (root / ".claude").exists()
