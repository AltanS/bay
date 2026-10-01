"""Tests for the dev-link target guard.

A consumer is a git repository too, so a dev-link target that only had to
be a git repository could be the consumer itself. That happened on a
workspace where the consumer is reached through a symlink: Path.cwd() is
the physical path, so the default ../bay resolved to the consumer, and
dev-link would have deleted .bay/ and linked the consumer into itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bay_cli.commands.framework import dev_link
from bay_cli.errors import BayError


def _make_consumer(root: Path) -> Path:
    """Create a consumer with a .git dir and a pinned .bay/ clone."""
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".bay-version").write_text("v0.7.0\n")
    bay = root / ".bay"
    bay.mkdir()
    (bay / "version.yml").write_text('---\nbay_version: "0.7.0"\n')
    gv = root / "group_vars" / "all"
    gv.mkdir(parents=True)
    (gv / "main.yml").write_text("---\napp_user: bay\n")
    return root


def _make_framework(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "version.yml").write_text('---\nbay_version: "0.7.0"\n')
    return root


class TestDevLinkTargetGuard:
    def test_refuses_the_consumer_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The symlink layout: ../bay from the physical dir is the consumer."""
        consumer = _make_consumer(tmp_path / "workspace-client" / "bay")
        (tmp_path / "workspace-dev").mkdir()
        (tmp_path / "workspace-dev" / "client").symlink_to(consumer)
        monkeypatch.chdir(tmp_path / "workspace-dev" / "client")

        with pytest.raises(BayError, match="this consumer"):
            dev_link(path="../bay")

        # The pinned clone is untouched and no sentinel was written
        assert (consumer / ".bay").is_dir()
        assert not (consumer / ".bay").is_symlink()
        assert not (consumer / ".bay-dev").exists()

    def test_refuses_a_git_repo_without_version_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        consumer = _make_consumer(tmp_path / "consumer")
        other = tmp_path / "other-repo"
        other.mkdir()
        (other / ".git").mkdir()
        monkeypatch.chdir(consumer)

        with pytest.raises(BayError, match="no version.yml"):
            dev_link(path=str(other))

        assert (consumer / ".bay").is_dir()
        assert not (consumer / ".bay").is_symlink()

    def test_links_a_real_framework(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        consumer = _make_consumer(tmp_path / "consumer")
        framework = _make_framework(tmp_path / "bay")
        monkeypatch.chdir(consumer)

        dev_link(path="../bay")

        assert (consumer / ".bay").is_symlink()
        assert os.path.realpath(consumer / ".bay") == str(framework.resolve())
        assert (consumer / ".bay-dev").exists()
