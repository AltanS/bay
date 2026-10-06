"""Test command."""

import typer

from bay_cli import runner
from bay_cli.context import context_from


def test(ctx: typer.Context) -> None:
    """Run the consumer's infrastructure tests (tests/test_infra.sh).

    Examples:

        bin/bay test
    """
    root = context_from(ctx).fleet_root
    runner.run(
        ["bash", "tests/test_infra.sh"],
        capture=False,
        cwd=root,
    )
