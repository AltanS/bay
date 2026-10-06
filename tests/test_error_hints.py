"""Error hints must name a command that can actually run.

The "no fleet" hint must offer the ways that exist today, and the version
messages in the playbooks must point at the command that updates Bay.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bay_cli.context import NO_FLEET_HINT, NO_FLEET_MESSAGE

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The removed consumer wrapper. Written in two pieces so a grep for the old
#: name finds nothing in the tree.
OLD_WRAPPER = "bin/" "bay"


def test_no_fleet_hint_names_commands_that_exist() -> None:
    assert NO_FLEET_MESSAGE == "no fleet selected"
    assert "bay fleet init" in NO_FLEET_HINT
    for gone in (OLD_WRAPPER, "setup", ".bay/"):
        assert gone not in NO_FLEET_HINT


@pytest.mark.parametrize("playbook", ["provision.yml", "deploy.yml"])
def test_version_hint_names_the_update_command(playbook: str) -> None:
    text = (_REPO_ROOT / playbook).read_text()
    assert "make bay:install" not in text, (
        f"{playbook} points at a Makefile alias instead of the CLI"
    )
    assert "bay install" not in text
    assert text.count("bay self update") == 1
