"""Tests for dev-link group_vars symlink idempotency.

Regression coverage for the sandbox incident on 2026-04-29 where
`bin/bay dev-link` short-circuited when already in dev-link mode and
never re-checked whether the framework_path/group_vars symlink existed.
With the symlink missing, ansible-playbook's group_vars resolution failed
because playbook_dir resolved to the framework dir (which had no group_vars), surfacing
as `Error processing keyword 'become_user': 'app_user' is undefined`.

The fix moved the group_vars symlink ensure into a helper that runs
unconditionally (both fresh dev-link and re-run-when-already-linked).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bay_cli.commands.framework import _ensure_group_vars_link
from bay_cli.errors import BayError


def _make_consumer_with_group_vars(root: Path) -> Path:
    """Create root/group_vars/all/main.yml so the helper has a target."""
    gv = root / "group_vars" / "all"
    gv.mkdir(parents=True)
    (gv / "main.yml").write_text("---\napp_user: bay\n")
    return root / "group_vars"


def _make_framework(root: Path) -> Path:
    """Create a framework directory under root."""
    fw = root / "bay"
    fw.mkdir()
    return fw


class TestEnsureGroupVarsLink:
    def test_creates_link_when_missing(self, tmp_path: Path) -> None:
        """Helper creates the symlink when framework has no group_vars/."""
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        gv_target = _make_consumer_with_group_vars(consumer)
        framework = _make_framework(tmp_path)

        _ensure_group_vars_link(framework, consumer)

        link = framework / "group_vars"
        assert link.is_symlink()
        assert link.resolve() == gv_target.resolve()
        # Reading through the symlink finds the consumer's vars
        assert (link / "all" / "main.yml").read_text().strip() == "---\napp_user: bay"

    def test_idempotent_when_link_already_correct(self, tmp_path: Path) -> None:
        """Re-running with a correct link is a no-op (no exception, no churn)."""
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        gv_target = _make_consumer_with_group_vars(consumer)
        framework = _make_framework(tmp_path)

        _ensure_group_vars_link(framework, consumer)
        first_target = (framework / "group_vars").resolve()
        first_inode = (framework / "group_vars").stat().st_ino

        # Run again — should not change anything observable
        _ensure_group_vars_link(framework, consumer)

        link = framework / "group_vars"
        assert link.is_symlink()
        assert link.resolve() == first_target == gv_target.resolve()
        # Same inode means we did not unlink+recreate
        assert link.stat().st_ino == first_inode

    def test_repairs_broken_symlink(self, tmp_path: Path) -> None:
        """A symlink pointing at a nonexistent target gets replaced."""
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _make_consumer_with_group_vars(consumer)
        framework = _make_framework(tmp_path)

        # Create a symlink pointing at a nonexistent path
        os.symlink(str(tmp_path / "does-not-exist"), framework / "group_vars")
        assert (framework / "group_vars").is_symlink()
        assert not (framework / "group_vars").resolve(strict=False).is_dir()

        _ensure_group_vars_link(framework, consumer)

        link = framework / "group_vars"
        assert link.is_symlink()
        assert link.resolve() == (consumer / "group_vars").resolve()
        assert (link / "all" / "main.yml").is_file()

    def test_repoints_symlink_to_wrong_consumer(self, tmp_path: Path) -> None:
        """A symlink pointing at a different consumer's group_vars gets re-pointed."""
        wrong_consumer = tmp_path / "wrong"
        wrong_consumer.mkdir()
        wrong_gv = _make_consumer_with_group_vars(wrong_consumer)
        right_consumer = tmp_path / "right"
        right_consumer.mkdir()
        right_gv = _make_consumer_with_group_vars(right_consumer)
        framework = _make_framework(tmp_path)

        os.symlink(str(wrong_gv.resolve()), framework / "group_vars")
        _ensure_group_vars_link(framework, right_consumer)

        link = framework / "group_vars"
        assert link.is_symlink()
        assert link.resolve() == right_gv.resolve()
        assert link.resolve() != wrong_gv.resolve()

    def test_raises_on_real_directory(self, tmp_path: Path) -> None:
        """A real directory (not a symlink) at framework/group_vars raises BayError."""
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _make_consumer_with_group_vars(consumer)
        framework = _make_framework(tmp_path)
        # Create a real directory where the symlink should go
        (framework / "group_vars").mkdir()
        (framework / "group_vars" / "stale.yml").write_text("---\n")

        with pytest.raises(BayError, match="real directory"):
            _ensure_group_vars_link(framework, consumer)

    def test_no_op_when_consumer_has_no_group_vars(self, tmp_path: Path) -> None:
        """Helper does nothing if consumer has no group_vars/ to link to."""
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        framework = _make_framework(tmp_path)

        _ensure_group_vars_link(framework, consumer)
        assert not (framework / "group_vars").exists()
        assert not (framework / "group_vars").is_symlink()
