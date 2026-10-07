"""`bay service add|edit|remove` are gone: they wrote the generated services.yml by hand.

The read verbs, `prune-webhooks` and `bay server add|remove` stay: none of them
writes services.yml.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from bay_cli.cli import app

runner = CliRunner()


def _commands(*group: str) -> str:
    result = runner.invoke(app, [*group, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def test_service_write_verbs_removed() -> None:
    out = _commands("service")
    for kept in ("list", "show", "catalog", "prune-webhooks"):
        assert kept in out, f"bay service {kept} must stay"
    for gone in ("add", "edit", "remove"):
        # a command row starts with its name in the help table
        assert f"│ {gone} " not in out, f"bay service {gone} must be gone"


@pytest.mark.parametrize("verb", ["add", "edit", "remove"])
def test_service_write_verb_is_not_a_command(verb: str) -> None:
    result = runner.invoke(app, ["service", verb, "x"])
    assert result.exit_code == 2
    assert "No such command" in result.output


def test_server_add_and_remove_stay() -> None:
    out = _commands("server")
    assert "add" in out and "remove" in out
