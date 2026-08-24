"""Test command."""

from bay_cli import paths, runner


def test() -> None:
    """Run the consumer's infrastructure tests (tests/test_infra.sh).

    Examples:

        bin/bay test
    """
    root = paths.consumer_root()
    runner.run(
        ["bash", "tests/test_infra.sh"],
        capture=False,
        cwd=root,
    )
