"""Subprocess runner with Rich spinner integration."""

import subprocess
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path

from bay_cli.console import SPINNER, console
from bay_cli.console.output import is_json_mode
from bay_cli.errors import BayError


@contextmanager
def _spinner(message: str):
    """Wrap a block with a Rich spinner, printing result on exit."""
    with console.status(f"  {message}", spinner="dots", spinner_style=SPINNER):
        yield
    # Status context manager clears the spinner on exit —
    # success/failure mark is printed by the caller after yield returns.


def run(
    cmd: list[str],
    *,
    message: str = "",
    capture: bool = True,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute a command with optional spinner and output capture.

    Args:
        cmd: Command and arguments.
        message: Spinner message. Empty string means no spinner.
        capture: If True, capture stdout/stderr and only show on failure.
                 If False, stream output live to terminal.
        cwd: Working directory.
        env: Additional environment variables (merged with os.environ).
        check: Raise BayError on non-zero exit code.
    """
    import os

    full_env = {**os.environ, **(env or {})}

    # ── Stream mode (capture=False): forward to terminal directly ────
    if not capture:
        result = subprocess.run(
            cmd,
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True,
            cwd=cwd,
            env=full_env,
        )
        if result.returncode != 0 and check:
            raise BayError(f"Command failed with exit code {result.returncode}")
        return result

    # ── Capture mode: run with optional spinner ──────────────────────
    use_spinner = bool(message) and not is_json_mode()
    ctx = _spinner(message) if use_spinner else nullcontext()

    with ctx:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=full_env,
        )

    # ── Report result ────────────────────────────────────────────────
    if use_spinner:
        if result.returncode == 0:
            console.print(f"  [green]\u2713[/green] {message}")
        else:
            console.print(f"  [red]\u2717[/red] {message}")

    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        if output.strip():
            if use_spinner:
                console.print()
            console.print(output.rstrip())
            if use_spinner:
                console.print()
        if check:
            raise BayError(f"Command failed: {' '.join(cmd)}")

    return result
