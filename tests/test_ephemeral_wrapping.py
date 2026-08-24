"""A displayed secret must never be hard-wrapped mid-token (GH bay#29).

Rich word-wraps by inserting real newlines. A headscale pre-auth key is a
single long token, so in a normal-width terminal it was split across lines;
copying it yielded a corrupted key, and because the key is single-use the
join failed with nothing pointing back at the copy as the cause. The
documented workaround was `COLUMNS=400 bin/bay gateway key <user>`.

`soft_wrap=True` leaves the line intact and lets the terminal wrap it
visually, so a selection still carries the whole token.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from bay_cli.utils import ephemeral

# Longer than any sane terminal width, and a single unbroken token.
_KEY = "hskey-auth-" + "0123456789abcdef" * 6


@pytest.fixture(autouse=True)
def _human_mode(monkeypatch):
    """Non-tty, non-JSON: show_ephemeral takes the plain-print path."""
    monkeypatch.setattr(ephemeral, "is_json_mode", lambda: False)
    monkeypatch.setattr(ephemeral.sys.stdout, "isatty", lambda: False, raising=False)


def _capture(width: int) -> str:
    """Render through a narrow console and return what landed on screen."""
    console = Console(width=width, file=None, record=True)
    ephemeral._console = console
    ephemeral.show_ephemeral(f"\n  [bold]Pre-auth key:[/bold]\n\n  {_KEY}\n")
    return console.export_text()


def test_key_survives_a_narrow_terminal():
    out = _capture(width=60)
    assert _KEY in out, (
        "the key was hard-wrapped across lines — copy-paste yields a corrupted "
        "single-use token"
    )


def test_key_survives_an_even_narrower_terminal():
    assert _KEY in _capture(width=40)


def test_key_intact_at_normal_width():
    assert _KEY in _capture(width=80)
