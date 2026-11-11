"""One place for ``--log``: where a command's progress and Ansible output go.

``bay up``, ``bay plan``, ``bay remove``, ``bay host upgrade``, ``bay deploy``
and ``bay provision`` take ``--log <file>`` with the meaning given in
:func:`routed_output`. They all use this module, so the redirect is one
implementation.

``bay deploy``, ``bay provision`` and ``bay host upgrade`` with ``--log`` and
no ``--json`` end with a short summary on the terminal: the result word, the
PLAY RECAP counts per host and the log path (:func:`recap_lines`).
``bay host upgrade`` puts its key facts between the result and the recap. Without it
the terminal showed only the ``fleet:`` line, also for a failed run.
"""

from __future__ import annotations

import contextlib
import os
import re
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


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_RECAP_HOST = re.compile(r"^(?P<host>\S+)\s*:\s*(?P<counts>(?:\w+=\d+\s*)+)$")


def log_offset(log: Path | None) -> int:
    """The size of ``log`` now: the summary reads only what this run appends."""
    try:
        return log.stat().st_size if log is not None else 0
    except OSError:
        return 0


def play_recap(text: str) -> dict[str, dict[str, int]]:
    """The PLAY RECAP counts per host in ``text``, the last block winning per host.

    Colour codes are removed first: the fleet env forces Ansible's colour.
    """
    hosts: dict[str, dict[str, int]] = {}
    in_recap = False
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).strip()
        if line.startswith("PLAY RECAP"):
            in_recap = True
            continue
        if not in_recap:
            continue
        match = _RECAP_HOST.match(line)
        if match is None:
            if line:
                in_recap = False
            continue
        counts = dict(part.split("=", 1) for part in match["counts"].split())
        hosts[match["host"]] = {k: int(v) for k, v in counts.items()}
    return hosts


def _read_since(log: Path, offset: int) -> str:
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            return fh.read()
    except OSError:
        return ""


def recap_lines(log: Path, offset: int = 0) -> list[str]:
    """One line per host from the PLAY RECAP this run wrote to ``log``, then the log path.

    A host line has ok, changed, unreachable (when not 0) and failed.
    """
    lines: list[str] = []
    recap = play_recap(_read_since(log, offset))
    if not recap:
        lines.append("  no PLAY RECAP in the log: the playbook did not run to its end")
    for host, counts in recap.items():
        parts = [f"ok={counts.get('ok', 0)}", f"changed={counts.get('changed', 0)}"]
        if counts.get("unreachable"):
            parts.append(f"unreachable={counts['unreachable']}")
        parts.append(f"failed={counts.get('failed', 0)}")
        lines.append(f"  {host}: {' '.join(parts)}")
    lines.append(f"  log: {log}")
    return lines


def summary_lines(command: str, *, ok: bool, log: Path, offset: int = 0) -> list[str]:
    """The end summary of a run whose output went to ``log``: the result word, then :func:`recap_lines`.

    A host with failed or unreachable tasks in the recap makes the word FAILED.
    """
    recap = play_recap(_read_since(log, offset))
    if any(c.get("failed") or c.get("unreachable") for c in recap.values()):
        ok = False
    return [f"{command}: {'ok' if ok else 'FAILED'}", *recap_lines(log, offset)]


def print_summary(lines: list[str]) -> None:
    """Print the summary on the terminal (call it after the redirect is undone)."""
    print("\n".join(lines), file=sys.stderr, flush=True)


@contextlib.contextmanager
def _summary_at_end(command: str, log: Path) -> Iterator[None]:
    """Print :func:`summary_lines` when the command ends, on success and on failure.

    Click passes the exception that ends the command into the resource, so a
    ``typer.Exit(0)`` counts as ok and every other exception as failed.
    """
    from bay_cli.click_compat import exit_code_of

    offset = log_offset(log)
    try:
        yield
    except BaseException as exc:
        ok = exit_code_of(exc) == 0
        print_summary(summary_lines(command, ok=ok, log=log, offset=offset))
        raise
    print_summary(summary_lines(command, ok=True, log=log, offset=offset))


def route_command_output(ctx: typer.Context, log: Path | None) -> None:
    """:func:`routed_output` for the whole command, undone when the command ends.

    For a command that has no ``--json`` document (``bay deploy``,
    ``bay provision``): with ``--log`` everything it prints goes to the file,
    Ansible included, and the terminal gets the end summary
    (:func:`summary_lines`). Without ``--log`` nothing is redirected. The
    streams are restored when the click context closes, after the command's
    own cleanups (the run lock, scratch folders) and before an error is shown.
    """
    if log is not None:
        # Resources close in reverse order: the redirect is undone first, so
        # the summary reaches the terminal.
        ctx.with_resource(_summary_at_end(f"bay {ctx.info_name or 'run'}", log))
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
