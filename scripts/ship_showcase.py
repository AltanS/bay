#!/usr/bin/env python3
"""Showcase ship variations for the Bay CLI banner.

All use C2 (blue) palette with the ▄▀█ block font.
Ordered from smallest to largest.

Run: uv run python scripts/ship_showcase.py
"""

from rich.console import Console
from rich.rule import Rule
from rich.text import Text

c = Console()

B = "bold blue"
M = "blue"
D = "dim blue"


def show(name: str, lines: list[str | Text]) -> None:
    c.print()
    c.print(Rule(f"[bold]{name}[/bold]", style="dim"))
    c.print()
    for line in lines:
        if isinstance(line, Text):
            c.print(line)
        else:
            c.print(line)
    c.print()


def t(text: str, style: str = "") -> Text:
    return Text(text, style=style)


# ══════════════════════════════════════════════════════════════════════
# NO SHIP — text-only baselines
# ══════════════════════════════════════════════════════════════════════

c.print()
c.print(Rule("[bold magenta]BASELINES — no ship[/bold magenta]", style="magenta"))

show("S0a: text only — tight", [
    t("  ▄▀█ █▀█ █▀▀ █▀█", B),
    t("  █▀█ █▀▄ █▄█ █▄█", D),
    t("  v0.21.0", "dim"),
])

show("S0b: text only — wide + wave", [
    t("  ▄▀█  █▀█  █▀▀  █▀█", B),
    t("  █▀█  █▀▄  █▄█  █▄█", D),
    t("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈", D),
    t("  v0.21.0", "dim"),
])

show("S0c: text only — wide + underline", [
    t("  ▄▀█  █▀█  █▀▀  █▀█", B),
    t("  █▀█  █▀▄  █▄█  █▄█", D),
    t("  ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁", D),
    t("  v0.21.0", "dim"),
])

# ══════════════════════════════════════════════════════════════════════
# TINY SHIPS (1-2 lines of ship)
# ══════════════════════════════════════════════════════════════════════

c.print()
c.print(Rule("[bold magenta]TINY SHIPS[/bold magenta]", style="magenta"))

show("S1: micro sail — inline", [
    t("  ▄▀█  █▀█  █▀▀  █▀█   ⛵", B),
    t("  █▀█  █▀▄  █▄█  █▄█", D),
    t("  v0.21.0", "dim"),
])

show("S2: single mast — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█    .|", B),
    t("  █▀█  █▀▄  █▄█  █▄█   _/|_", D),
    t("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈", D),
    t("  v0.21.0", "dim"),
])

show("S3: tiny triangle sail", [
    t("  ▄▀█  █▀█  █▀▀  █▀█     |", B),
    t("  █▀█  █▀▄  █▄█  █▄█    /|", M),
    t("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈_/≈≈", D),
    t("  v0.21.0", "dim"),
])

show("S4: tiny boat — below", [
    t("  ▄▀█  █▀█  █▀▀  █▀█", B),
    t("  █▀█  █▀▄  █▄█  █▄█", D),
    t("        |", D),
    t("       /|\\", D),
    t("  ~~~~/_|_\\~~~~", D),
    t("  v0.21.0", "dim"),
])

show("S5: flag + hull — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█    ⚑|", B),
    t("  █▀█  █▀▄  █▄█  █▄█   \\__/", D),
    t("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈", D),
    t("  v0.21.0", "dim"),
])

# ══════════════════════════════════════════════════════════════════════
# SMALL SHIPS (3-4 lines of ship)
# ══════════════════════════════════════════════════════════════════════

c.print()
c.print(Rule("[bold magenta]SMALL SHIPS[/bold magenta]", style="magenta"))

show("S6: simple sail — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█      |", B),
    t("  █▀█  █▀▄  █▄█  █▄█     /|", M),
    t("                         / |", D),
    t("  v0.21.0          ≈≈≈≈/__|≈≈≈", D),
])

show("S7: two sails — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█      |", B),
    t("  █▀█  █▀▄  █▄█  █▄█    |\\|", M),
    t("                         | \\|", D),
    t("  v0.21.0          ≈≈≈\\___/≈≈≈", D),
])

show("S8: paper boat — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█       _", B),
    t("  █▀█  █▀▄  █▄█  █▄█      / \\", M),
    t("                       ___/   \\___", D),
    t("  v0.21.0              \\         /", D),
    t("                    ≈≈≈≈\\_______/≈≈≈≈", D),
])

show("S9: small sail — below text", [
    t("  ▄▀█  █▀█  █▀▀  █▀█", B),
    t("  █▀█  █▀▄  █▄█  █▄█", D),
    t("           |", D),
    t("          /|\\", D),
    t("         / | \\", D),
    t("  ≈≈≈≈≈≈/__|__\\≈≈≈≈≈≈", D),
    t("  v0.21.0", "dim"),
])

show("S10: pennant sail — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█      |>", B),
    t("  █▀█  █▀▄  █▄█  █▄█      |", M),
    t("                        __/|\\__", D),
    t("  v0.21.0               \\_____/", D),
    t("                      ≈≈≈≈≈≈≈≈≈", D),
])

# ══════════════════════════════════════════════════════════════════════
# MEDIUM SHIPS (5-6 lines of ship)
# ══════════════════════════════════════════════════════════════════════

c.print()
c.print(Rule("[bold magenta]MEDIUM SHIPS[/bold magenta]", style="magenta"))

show("S11: classic sailboat — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█        |", B),
    t("  █▀█  █▀▄  █▄█  █▄█       /|\\", M),
    t("                           / | \\", M),
    t("                          /  |  \\", D),
    t("  v0.21.0            ____/   |   \\", D),
    t("                     \\           /", D),
    t("                  ≈≈≈≈\\_________/≈≈≈≈", D),
])

show("S12: sloop — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█        |", B),
    t("  █▀█  █▀▄  █▄█  █▄█       /|", M),
    t("                           / |", M),
    t("                     _____/  |", D),
    t("  v0.21.0            \\       |", D),
    t("                      \\______|", D),
    t("                   ≈≈≈≈≈≈≈≈≈≈≈≈≈", D),
])

show("S13: schooner — two masts", [
    t("  ▄▀█  █▀█  █▀▀  █▀█       |   |", B),
    t("  █▀█  █▀▄  █▄█  █▄█      /|  /|", M),
    t("                          / | / |", M),
    t("                    _____/  |/  |", D),
    t("  v0.21.0           \\           |", D),
    t("                     \\_________|", D),
    t("                  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈", D),
])

show("S14: three-mast — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█      |    |    |", B),
    t("  █▀█  █▀▄  █▄█  █▄█     )_)  )_)  )_)", M),
    t("                         )___))___))___)", D),
    t("                        )____)____)____)", D),
    t("  v0.21.0         ______|____|____|_____", D),
    t("               ≈≈\\                   /≈≈", D),
    t("                ≈≈\\_________________ /≈≈≈", D),
])

# ══════════════════════════════════════════════════════════════════════
# NAUTICAL SYMBOLS (non-ship alternatives)
# ══════════════════════════════════════════════════════════════════════

c.print()
c.print(Rule("[bold magenta]NAUTICAL SYMBOLS[/bold magenta]", style="magenta"))

show("S15: anchor — right side (compact)", [
    t("  ▄▀█  █▀█  █▀▀  █▀█     ⚓", B),
    t("  █▀█  █▀▄  █▄█  █▄█", D),
    t("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈", D),
    t("  v0.21.0", "dim"),
])

show("S16: anchor — small ASCII", [
    t("  ▄▀█  █▀█  █▀▀  █▀█      _|_", B),
    t("  █▀█  █▀▄  █▄█  █▄█    --|--|--", M),
    t("                           |", D),
    t("  v0.21.0                 \\_/", D),
    t("                      ≈≈≈≈≈≈≈≈≈", D),
])

show("S17: compass — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█      N", B),
    t("  █▀█  █▀▄  █▄█  █▄█    ◁ ◆ ▷", M),
    t("                           S", D),
    t("  v0.21.0", "dim"),
])

show("S18: helm wheel — right side", [
    t("  ▄▀█  █▀█  █▀▀  █▀█     *-*", B),
    t("  █▀█  █▀▄  █▄█  █▄█    -|◉|-", M),
    t("                          *-*", D),
    t("  v0.21.0", "dim"),
])

show("S19: wave pattern — below text", [
    t("  ▄▀█  █▀█  █▀▀  █▀█", B),
    t("  █▀█  █▀▄  █▄█  █▄█", D),
    t("  ╰─╮╭─╮╭─╮╭─╮╭─╮╭─╮╭─╯", D),
    t("    ╰╯ ╰╯ ╰╯ ╰╯ ╰╯ ╰╯", D),
    t("  v0.21.0", "dim"),
])

show("S20: wave pattern (simple) — below text", [
    t("  ▄▀█  █▀█  █▀▀  █▀█", B),
    t("  █▀█  █▀▄  █▄█  █▄█", D),
    t("  ∼∽∼∽∼∽∼∽∼∽∼∽∼∽∼∽∼∽∼∽", D),
    t("  v0.21.0", "dim"),
])

# ── Summary ───────────────────────────────────────────────────────────

c.print()
c.print(Rule("[bold]End of showcase[/bold]", style="dim"))
c.print()
c.print("  [dim]Pick a ship style (S0-S20) — or combine: e.g. 'S6 layout but S14 ship'.[/dim]")
c.print()
