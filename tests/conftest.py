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
