"""There is one documented entry path, and every document states it the same way.

Bay used to ship four quick starts across four documents — an SSH clone in the
README, `make bay:setup BAY_REPO=…` in the onboarding guide, `make bay:setup`
then `bin/bay install` in SKILL.md, and bootstrap-or-Makefile "equivalents" in
the example README. A reader who followed the wrong one ended up without a
`bin/bay` wrapper. These tests pin the single path so it cannot fan back out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The one entry path, in order. Every onboarding document must state it.
QUICK_START = (
    "git clone https://github.com/AltanS/bay.git .bay",
    ".bay/bootstrap.sh",
    "bin/bay setup",
)

#: The documents a new reader lands on.
ONBOARDING_DOCS = (
    "README.md",
    "SKILL.md",
    "docs/onboarding.md",
    "example/README.md",
)

SSH_CLONE = "git@github.com:AltanS/bay.git"


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text()


@pytest.mark.parametrize("doc", ONBOARDING_DOCS)
def test_document_states_the_quick_start_in_order(doc: str) -> None:
    text = _read(doc)
    cursor = 0
    for command in QUICK_START:
        index = text.find(command, cursor)
        assert index != -1, (
            f"{doc} does not state '{command}' after the preceding quick-start "
            f"command — the four onboarding documents must agree, in order: "
            f"{QUICK_START}"
        )
        cursor = index + len(command)


@pytest.mark.parametrize("doc", ONBOARDING_DOCS + ("bootstrap.sh",))
def test_no_ssh_clone_url_is_documented_as_the_default(doc: str) -> None:
    """HTTPS is the documented clone.

    Bay is a public MIT repo; a reader without a GitHub SSH key is the default
    case, and an SSH clone fails at the very first command. The SSH URL may
    survive only as an explicitly-labelled `BAY_REPO` override.
    """
    assert SSH_CLONE not in _read(doc), f"{doc} still clones over SSH"


@pytest.mark.parametrize("doc", ONBOARDING_DOCS)
def test_make_bay_setup_is_mentioned_at_most_once(doc: str) -> None:
    """`make bay:setup` is a convenience, not a second path.

    Mentioned more than once per document it starts to read as an alternative
    with its own steps, which is exactly how the paths diverged before.
    """
    text = _read(doc)
    assert text.count("make bay:setup") <= 1, (
        f"{doc} mentions 'make bay:setup' more than once; it is an equivalent, "
        "not a separate entry path"
    )


def test_skill_md_does_not_teach_the_two_step_setup() -> None:
    """`bin/bay install` after bootstrap is redundant — bootstrap already pinned."""
    text = _read("SKILL.md")
    assert "`make bay:setup` then `bin/bay install`" not in text
    assert "bootstrap.sh" in text
