"""Suite-wide pytest configuration.

CLI help-text assertions (e.g. `assert "--path" in result.output`) are
environment-dependent in a way that isn't about terminal *width*: Typer's
`rich_utils` forces ANSI rendering whenever `GITHUB_ACTIONS` (or
`FORCE_COLOR`/`PY_COLORS`) is set in the environment — see
`typer.rich_utils.FORCE_TERMINAL` — regardless of whether stdout is a real
tty. GitHub Actions runners always export `GITHUB_ACTIONS=true`, so CI
renders `--help` output with Rich's option/negative-option highlighter,
which splits a flag like `--path` into two separately-styled spans
(`-` then `-path`) joined by escape codes. The escape codes land *between*
the characters, so a plain `"--path" in result.output` substring check
fails even though the flag is right there, rendered — this reproduces
locally with `GITHUB_ACTIONS=true uv run pytest ...` regardless of
`COLUMNS`. Pinning terminal width does not fix it.

Typer ships its own escape hatch for this (`_TYPER_FORCE_DISABLE_TERMINAL`,
read once at import time by `typer.rich_utils`), so we set it here, as
early as possible, before any test module can import `bay_cli` (and
transitively `typer.rich_utils`). This makes `CliRunner` help output
plain text everywhere — locally and in CI alike — so help-text assertions
stop depending on the invoking environment.
"""

from __future__ import annotations

import os

os.environ.setdefault("_TYPER_FORCE_DISABLE_TERMINAL", "1")


import pytest  # noqa: E402


def _reset_console_modes() -> None:
    from bay_cli.console import output

    output.set_json_mode(False)
    output.set_yes_mode(False)
    output._message_buffer.clear()


@pytest.fixture(autouse=True)
def _reset_console_state():
    """The root Typer callback sets JSON and yes mode on module globals.

    Nothing resets them after a CLI run, so a `--json` test would leak its
    mode into the next test on the same worker. Every test starts and ends
    with the defaults and an empty message buffer.
    """
    _reset_console_modes()
    yield
    _reset_console_modes()


@pytest.fixture(autouse=True)
def _unbind_fleet():
    """`Context.resolve` binds the fleet for Ansible commands (bay_cli/ansible.py).

    That is process state, right for a CLI run and wrong across tests, so every
    test starts and ends unbound.
    """
    from bay_cli import ansible

    ansible.bind_fleet(None)
    yield
    ansible.bind_fleet(None)
