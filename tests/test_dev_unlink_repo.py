"""`dev-unlink` must re-clone from the consumer's own BAY_REPO.

It hard-coded the clone URL until v1.5.1, so a consumer who had pointed
BAY_REPO at a fork, an HTTPS remote, or a local path -- which is exactly what
the bootstrap test does -- silently got the hard-coded repo back instead of
their own. `make bay:setup` honours the var; this was the one place that did
not.
"""

from __future__ import annotations

import textwrap

import pytest

from bay_cli.commands.framework import DEFAULT_BAY_REPO, _consumer_bay_repo


def _consumer(tmp_path, makefile: str | None):
    if makefile is not None:
        (tmp_path / "Makefile").write_text(textwrap.dedent(makefile))
    return tmp_path


@pytest.mark.parametrize(
    "assignment,expected",
    [
        ("BAY_REPO := git@github.com:someone/fork.git", "git@github.com:someone/fork.git"),
        ("BAY_REPO = https://github.com/AltanS/bay.git", "https://github.com/AltanS/bay.git"),
        ("BAY_REPO ?= /srv/mirrors/bay.git", "/srv/mirrors/bay.git"),
        ("BAY_REPO:=git@github.com:tight/spacing.git", "git@github.com:tight/spacing.git"),
    ],
)
def test_reads_bay_repo_in_every_make_assignment_form(tmp_path, assignment, expected):
    root = _consumer(tmp_path, f"""
        BAY_DIR := .bay
        {assignment}
        ARGS ?=
        """)
    assert _consumer_bay_repo(root) == expected


def test_local_path_is_honoured(tmp_path):
    """The bootstrap test clones from a working-tree path, not GitHub."""
    root = _consumer(tmp_path, "BAY_REPO := /tmp/some/checkout\n")
    assert _consumer_bay_repo(root) == "/tmp/some/checkout"


@pytest.mark.parametrize(
    "makefile",
    [
        None,                          # no Makefile at all
        "BAY_DIR := .bay\nARGS ?=\n",  # Makefile predating the var
    ],
)
def test_falls_back_to_the_public_default(tmp_path, makefile):
    root = _consumer(tmp_path, makefile)
    assert _consumer_bay_repo(root) == DEFAULT_BAY_REPO


def test_default_points_at_the_public_repo():
    """Guard the sweep: the shipped default must be the public clone URL.

    Asserted positively rather than by denying the private org name. Spelling
    that name here would itself be a finding for scripts/leak-scan.sh -- which
    is exactly what happened on this test's first release run. A guard test is
    not exempt from the guard.
    """
    assert DEFAULT_BAY_REPO == "git@github.com:AltanS/bay.git"


def test_example_makefile_matches_the_default(tmp_path):
    """example/ is what a stranger copies; it must not drift from the default."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "example"
    assert _consumer_bay_repo(example) == DEFAULT_BAY_REPO
