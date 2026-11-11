"""The Click exception classes, found through Typer.

Never write ``import click`` in ``bay_cli``. Newer Typer (0.27 and later) ships
Click inside itself, so a ``uv tool install`` has no top-level ``click``. The
classes Typer raises are then its own copies, and a separately installed Click
would not match in ``isinstance`` checks. Older Typer uses the real ``click``.

``typer.Exit``, ``typer.Abort`` and ``typer.BadParameter`` exist in both. The
``ClickException`` base is reached through ``BadParameter``, whose base chain
holds it in both. A test (``tests/test_click_compat.py``) bans ``import click``
anywhere else under ``src/``.
"""

from __future__ import annotations

import typer

Exit = typer.Exit
Abort = typer.Abort


def _click_exception_base() -> type[Exception]:
    for cls in typer.BadParameter.__mro__:
        if cls.__name__ == "ClickException":
            return cls
    raise RuntimeError("typer.BadParameter has no ClickException base")  # pragma: no cover


ClickException = _click_exception_base()

#: What Click passes through unchanged: a clean exit, an abort or a usage error.
PASSTHROUGH: tuple[type[BaseException], ...] = (Exit, Abort, ClickException)


def exit_code_of(exc: BaseException) -> int | None:
    """The exit code if ``exc`` is a Typer/Click ``Exit``, else None."""
    return exc.exit_code if isinstance(exc, Exit) else None
