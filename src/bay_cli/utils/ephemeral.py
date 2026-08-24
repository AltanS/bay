"""Ephemeral secret display — alternate screen buffer to prevent scrollback persistence.

Secrets shown via show_ephemeral() are rendered in the terminal's alternate screen
buffer (the same mechanism vim/less/htop use). When the user dismisses the display,
the terminal switches back to the main buffer and the secret is gone from scrollback.

Bypass rules:
- JSON mode (--json flag): prints content normally (machine consumption)
- Non-interactive terminal (piped stdout): prints content normally
- Clipboard copy is opt-in: user presses 'c' to copy while viewing
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import termios
import tty
from typing import IO

from rich.console import Console

from bay_cli.console.output import is_json_mode

_console = Console()

# A clipboard tool must hand back control quickly: wl-copy/xclip/xsel all fork and
# serve the selection from a background child, so the process we wait on is expected
# to exit at once. One that doesn't is wedged (seen in the wild: wl-copy blocking
# forever on a Wayland session whose clipboard had died, which hung the whole key
# loop). Bound the wait and move on.
_CLIPBOARD_TIMEOUT_S = 1.0


def emit_osc52(text: str, out: IO[str]) -> None:
    """Ask the terminal itself to copy text, via the OSC 52 escape sequence.

    Needs no helper binary and works over SSH, where wl-copy/xclip talk to the
    wrong machine's clipboard (or no display at all). Fire-and-forget: terminals
    send no acknowledgement and some refuse OSC 52 outright, so a caller can never
    report this as confirmed success.
    """
    payload = base64.b64encode(text.encode()).decode("ascii")
    seq = f"\033]52;c;{payload}\a"
    if os.environ.get("TMUX"):
        # tmux swallows OSC unless it's wrapped in a passthrough sequence.
        seq = f"\033Ptmux;\033{seq}\033\\"
    out.write(seq)
    out.flush()


def try_copy_to_clipboard(text: str) -> bool:
    """Attempt to copy text to the system clipboard using an external tool.

    Tries each tool in order: pbcopy (macOS), wl-copy (Wayland), xclip, xsel (X11).
    Returns True only on a confirmed clean exit.
    """
    clipboard_cmds = [
        ["pbcopy"],
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]
    for cmd in clipboard_cmds:
        try:
            # start_new_session detaches the tool from our terminal's process group,
            # so a Ctrl-C aimed at this screen can't take its resident child with it.
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError):
            continue
        try:
            proc.communicate(input=text.encode(), timeout=_CLIPBOARD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            continue
        if proc.returncode == 0:
            return True
    return False


def show_ephemeral(
    content: str,
    *,
    clipboard: str | None = None,
    json_content: dict | None = None,
) -> None:
    """Display content in the alternate screen buffer, then clear on dismiss.

    Parameters
    ----------
    content:
        Rich-markup string to display in the alternate screen.
    clipboard:
        If provided, the user can press 'c' to copy this value to clipboard.
    json_content:
        If provided and --json mode is active, emit this dict as structured
        JSON output instead of the human-readable content.
    """
    # JSON mode: emit structured output, skip alternate screen
    if is_json_mode():
        if json_content is not None:
            from bay_cli.console.output import emit_result

            emit_result(json_content)
        else:
            _console.print(content, soft_wrap=True)
        return

    # Non-interactive (piped stdout): print normally
    if not sys.stdout.isatty():
        _console.print(content, soft_wrap=True)
        return

    # Interactive terminal: use alternate screen buffer
    _show_in_alternate_screen(content, clipboard)


def _show_in_alternate_screen(content: str, clipboard: str | None) -> None:
    """Render content in the alternate screen buffer with a dismiss prompt."""
    # Open /dev/tty directly — do NOT use sys.stdin which may have stale
    # buffered data from getpass or Rich spinners.
    tty_fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    tty_out = os.fdopen(os.dup(tty_fd), "w")
    try:
        tty_out.write("\033[?1049h")  # Switch to alternate screen
        tty_out.write("\033[H\033[2J")  # Clear
        tty_out.flush()

        c = Console(file=tty_out)
        # soft_wrap: Rich word-wraps by inserting REAL newlines, which splits a
        # long single-use token mid-string — selecting it then yields a corrupted
        # key that fails to join, with no hint as to why (GH bay#29). Soft wrap
        # leaves it one logical line and lets the terminal handle the display.
        c.print(content, soft_wrap=True)
        c.print()

        if clipboard:
            c.print("  [dim]Press [bold]c[/bold] to copy, [bold]Enter[/bold] to dismiss[/dim]")
        else:
            c.print("  [dim]Press [bold]Enter[/bold] to dismiss[/dim]")

        old_settings = termios.tcgetattr(tty_fd)
        try:
            termios.tcflush(tty_fd, termios.TCIFLUSH)
            tty.setcbreak(tty_fd)

            # Once-guarded: acting on every 'c' press would append a status line
            # each time and scroll the secret out of view.
            copy_attempted = False
            while True:
                ch = os.read(tty_fd, 1).decode("utf-8", errors="replace")
                if ch in ("c", "C") and clipboard and not copy_attempted:
                    copy_attempted = True
                    # Stay in cbreak: clipboard tools never touch the tty, and
                    # restoring cooked mode here would echo every subsequent
                    # keypress into the screen.
                    emit_osc52(clipboard, tty_out)
                    if try_copy_to_clipboard(clipboard):
                        c.print("  [green]✓[/green] [dim]Copied to clipboard.[/dim]")
                    else:
                        c.print(
                            "  [green]✓[/green] [dim]Copy sent to the terminal (OSC 52). "
                            "No local clipboard tool answered — if nothing pastes, your "
                            "terminal may block OSC 52; copy manually.[/dim]"
                        )
                elif ch in ("\r", "\n", "q", "Q", "\x1b", "\x03"):
                    break
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            termios.tcsetattr(tty_fd, termios.TCSAFLUSH, old_settings)
    finally:
        tty_out.write("\033[?1049l")  # Switch back to main screen
        tty_out.flush()
        tty_out.close()
        os.close(tty_fd)
