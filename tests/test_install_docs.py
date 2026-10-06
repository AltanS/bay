"""The README and the install page give one install path, and the CLI backs it.

The old quick start was a wrapper script, a clone inside the project and a pin
file. The only supported path now is: clone the checkout, install it once per
machine, then make a fleet. These tests keep the docs and the commands in step.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from bay_cli.cli import app

ROOT = Path(__file__).resolve().parent.parent
CLONE = "git clone https://github.com/AltanS/bay ~/.local/share/bay/framework"
REMOVED = re.compile(r"\.bay-version|\.bay/|bay:setup|bay (setup|install|update|guide)\b|dev-link|dev-unlink")


def _text(name: str) -> str:
    return (ROOT / name).read_text()


def test_readme_and_install_page_use_the_one_checkout_path() -> None:
    for doc in ("README.md", "docs/install.md"):
        text = _text(doc)
        assert CLONE in text, doc
        assert "bootstrap.sh" in text or "uv tool install --editable" in text, doc


def test_install_page_documents_the_self_and_fleet_commands() -> None:
    text = _text("docs/install.md")
    for command in ("bay self update", "bay self version", "bay fleet init", "bay fleet ls", "--fleet"):
        assert command in text, command


def test_every_documented_install_command_exists() -> None:
    runner = CliRunner()
    for args in (
        ["self", "update", "--help"],
        ["self", "version", "--help"],
        ["fleet", "init", "--help"],
        ["fleet", "ls", "--help"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)


def test_removed_setup_commands_are_gone() -> None:
    runner = CliRunner()
    for command in ("setup", "install", "update", "guide", "dev-link", "dev-unlink"):
        result = runner.invoke(app, [command])
        assert result.exit_code != 0, command


def test_user_docs_do_not_describe_the_removed_model() -> None:
    for doc in ("README.md", "docs/install.md", "docs/onboarding.md", "SKILL.md"):
        hits = REMOVED.findall(_text(doc))
        assert hits == [], (doc, hits)
