"""Formatted output helpers — success, error, warning, info, header.

In JSON mode, messages are buffered instead of printed. The final
JSON output is emitted by ``emit_result()`` or the root error handler.
"""

from __future__ import annotations

import json

from rich.console import Console

console = Console()

# Module-level mode flags — set by root Typer callback
_json_mode: bool = False
_yes_mode: bool = False
_message_buffer: list[str] = []


def set_json_mode(val: bool) -> None:
    global _json_mode
    _json_mode = val


def set_yes_mode(val: bool) -> None:
    global _yes_mode
    _yes_mode = val


def is_json_mode() -> bool:
    return _json_mode


def is_yes_mode() -> bool:
    return _yes_mode


def _buffer_or_print(prefix: str, msg: str, style: str) -> None:
    """Buffer in JSON mode, print Rich in human mode."""
    if _json_mode:
        _message_buffer.append(f"{prefix} {msg}")
    else:
        console.print(f"  [{style}]{prefix}[/{style}] {msg}")


def success(msg: str) -> None:
    _buffer_or_print("\u2713", msg, "green")


def error(msg: str) -> None:
    _buffer_or_print("\u2717", msg, "red")


def warning(msg: str) -> None:
    _buffer_or_print("!", msg, "yellow")


def info(msg: str) -> None:
    _buffer_or_print("i", msg, "blue")


def header(msg: str) -> None:
    if not _json_mode:
        console.print(f"\n[bold]{msg}[/bold]")


def emit_result(data: dict, *, command: str = "") -> None:
    """Emit a structured result.

    In JSON mode, prints ``{"ok": true, ...}`` to stdout.
    In human mode, this is a no-op (callers use Rich separately).
    """
    if not _json_mode:
        return
    result = {
        "ok": True,
        "command": command,
        "data": data,
        "messages": list(_message_buffer),
    }
    _message_buffer.clear()
    print(json.dumps(result, indent=2, default=str))


def emit_error(errors: list[dict], *, command: str = "") -> None:
    """Emit a structured error result in JSON mode."""
    if not _json_mode:
        return
    result = {
        "ok": False,
        "command": command,
        "errors": errors,
        "messages": list(_message_buffer),
    }
    _message_buffer.clear()
    print(json.dumps(result, indent=2, default=str))


def drain_messages() -> list[str]:
    """Return and clear the message buffer."""
    msgs = list(_message_buffer)
    _message_buffer.clear()
    return msgs
