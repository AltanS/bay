"""`bay webhook` is gone (2.2.0).

It ran the root `webhook.yml` playbook without the fleet's inventory and read
deploy keys from `/opt/bay/` only, so it never worked against a v2 fleet. A
`bay deploy <env>` installs the receiver and the deploy keys, and `bay up`
registers each build container with the receiver and enables its trigger.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from bay_cli.cli import app

_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def test_webhook_verb_removed() -> None:
    help_out = runner.invoke(app, ["--help"])
    assert help_out.exit_code == 0, help_out.output
    # A command row in the Rich help table starts with the command name.
    assert not re.search(r"^\W*webhook\s", help_out.output, flags=re.MULTILINE), help_out.output
    # Control: the table does list commands, so the check above can fail.
    assert re.search(r"^\W*prune\s", help_out.output, flags=re.MULTILINE), help_out.output

    result = runner.invoke(app, ["webhook", "production"])
    assert result.exit_code != 0
    assert "No such command" in result.output

    assert not (_ROOT / "src" / "bay_cli" / "commands" / "webhook.py").exists()
    assert not (_ROOT / "webhook.yml").exists()
