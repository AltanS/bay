"""One place for ``--log``: where a command's progress and Ansible output go.

``bay up``, ``bay plan``, ``bay remove``, ``bay host upgrade``, ``bay deploy``
and ``bay provision`` take ``--log <file>`` with the meaning given in
:func:`routed_output`. They all use this module, so the redirect is one
implementation.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Annotated

import typer

from bay_cli import console

LogOpt = Annotated[
    Path | None,
    typer.Option(
        "--log",
        help="Append progress and Ansible output to this file instead of the terminal.",
    ),
]


@contextlib.contextmanager
def routed_output(as_json: bool, log: Path | None) -> Iterator[Callable[[str], None]]:
    """Keep stdout for the one result document.

    With ``--log``, progress lines and everything the deploy prints (Ansible
    included, stdout AND stderr) are appended to that file. Otherwise, with
    ``--json``, they go to stderr. Otherwise nothing is redirected.

    A child process gets ``sys.stdout`` and ``sys.stderr`` as its handles
    (``runner.run`` passes them), so both Python streams point at the target.
    Descriptors 1 (and 2 with ``--log``) are redirected too when the target
    is a real file, so a child that only inherits them follows as well.

    Yields the function that prints one progress line.
    """
    if log is None and not as_json:
        yield lambda m: console.info(m)
        return
    from bay_cli import fleetsync

    handle: IO[str] = open(log, "a", encoding="utf-8") if log is not None else sys.stderr
    fds = (1, 2) if log is not None else (1,)
    saved: dict[int, int] = {}
    terminal: IO[str] | None = sys.stderr if log is not None else None
    try:
        on_fd2 = sys.stderr.fileno() == 2
    except (OSError, ValueError, AttributeError):
        on_fd2 = False
    try:
        target_fd = handle.fileno()
        sys.stdout.flush()
        sys.stderr.flush()
        for fd in fds:
            saved[fd] = os.dup(fd)
            os.dup2(target_fd, fd)
    except (OSError, ValueError, AttributeError):  # io.UnsupportedOperation is both
        for fd, copy in saved.items():
            os.dup2(copy, fd)
            os.close(copy)
        saved = {}
    owned = 2 in saved and on_fd2
    if owned:
        # sys.stderr wrote to fd 2, which is the log now: the terminal is the saved copy.
        terminal = os.fdopen(os.dup(saved[2]), "w", encoding="utf-8")
    if terminal is not None:
        fleetsync.TERMINAL.append(terminal)

    def say(message: str) -> None:
        print(message, file=handle, flush=True)

    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(handle))
            if log is not None:
                stack.enter_context(contextlib.redirect_stderr(handle))
            yield say
    finally:
        if terminal is not None:
            fleetsync.TERMINAL.remove(terminal)
            if owned:
                terminal.close()
        handle.flush()
        for fd, copy in saved.items():
            os.dup2(copy, fd)
            os.close(copy)
        if log is not None:
            handle.close()


def route_command_output(ctx: typer.Context, log: Path | None) -> None:
    """:func:`routed_output` for the whole command, undone when the command ends.

    For a command that has no ``--json`` document (``bay deploy``,
    ``bay provision``): with ``--log`` everything it prints goes to the file,
    Ansible included. Without ``--log`` nothing is redirected. The streams are
    restored when the click context closes, after the command's own cleanups
    (the run lock, scratch folders) and before an error is shown.
    """
    if log is not None:
        ctx.with_resource(routed_output(False, log))


def take_log_option(ctx: typer.Context, log: Path | None) -> Path | None:
    """Rescue ``--log <file>`` or ``--log=<file>`` swallowed into ``ctx.args``.

    Click stops parsing options at the positional ``env`` of ``bay deploy`` and
    ``bay provision``, so a ``--log`` after it lands in the Ansible passthrough.
    The option is removed from ``ctx.args``. Only tokens before a bare ``--`` count:
    after it, the arguments belong to ansible-playbook.
    """
    args = list(ctx.args or [])
    stop = args.index("--") if "--" in args else len(args)
    kept: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if i < stop and token == "--log" and i + 1 < stop:
            log = Path(args[i + 1])
            i += 2
        elif i < stop and token.startswith("--log="):
            log = Path(token.split("=", 1)[1])
            i += 1
        else:
            kept.append(token)
            i += 1
    ctx.args = kept
    return log
