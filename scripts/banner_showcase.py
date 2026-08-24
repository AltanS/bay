#!/usr/bin/env python3
"""Showcase banner variants for the Bay CLI.

Run: uv run python scripts/banner_showcase.py
"""

from rich.console import Console
from rich.rule import Rule
from rich.text import Text

c = Console()


def show(name: str, lines: list[str]) -> None:
    c.print()
    c.print(Rule(f"[bold]{name}[/bold]", style="dim"))
    c.print()
    for line in lines:
        c.print(line)
    c.print()


def plain(text: str, style: str = "") -> Text:
    """Create a Rich Text that won't interpret markup/escapes."""
    return Text(text, style=style)


# ══════════════════════════════════════════════════════════════════════
# SECTION 1: Style 18 — Block 2-line + waves (A letter variations)
# ══════════════════════════════════════════════════════════════════════

c.print()
c.print(Rule("[bold magenta]SECTION 1: Block 2-line — fixing the A[/bold magenta]", style="magenta"))

show("18-original (A looks like N — bad)", [
    plain("  █▀█ █▀█ █▀▀ █▀█", "bold cyan"),
    plain("  █ █ █▀▄ █▄█ █▄█", "dim cyan"),
    plain("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈  v0.21.0", "dim cyan"),
])

show("18a: A = ▄▀█ / █▀█ (slope top + crossbar)", [
    plain("  ▄▀█ █▀█ █▀▀ █▀█", "bold cyan"),
    plain("  █▀█ █▀▄ █▄█ █▄█", "dim cyan"),
    plain("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈  v0.21.0", "dim cyan"),
])

show("18b: A = ▄▀▄ / █ █ (pointed top, no crossbar)", [
    plain("  ▄▀▄ █▀█ █▀▀ █▀█", "bold cyan"),
    plain("  █ █ █▀▄ █▄█ █▄█", "dim cyan"),
    plain("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈  v0.21.0", "dim cyan"),
])

show("18c: A = ▄▀▄ / █▀█ (pointed top + crossbar)", [
    plain("  ▄▀▄ █▀█ █▀▀ █▀█", "bold cyan"),
    plain("  █▀█ █▀▄ █▄█ █▄█", "dim cyan"),
    plain("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈  v0.21.0", "dim cyan"),
])

show("18d: wider spacing — ▄▀█ variant", [
    plain("  ▄▀█  █▀█  █▀▀  █▀█", "bold cyan"),
    plain("  █▀█  █▀▄  █▄█  █▄█", "dim cyan"),
    plain("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈  v0.21.0", "dim cyan"),
])

show("18e: wider spacing — ▄▀▄ variant", [
    plain("  ▄▀▄  █▀█  █▀▀  █▀█", "bold cyan"),
    plain("  █▀█  █▀▄  █▄█  █▄█", "dim cyan"),
    plain("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈  v0.21.0", "dim cyan"),
])


# ══════════════════════════════════════════════════════════════════════
# SECTION 2: Style 19 — Block font + ship (smaller sizes)
# ══════════════════════════════════════════════════════════════════════

c.print()
c.print(Rule("[bold magenta]SECTION 2: Block font + ship — smaller sizes[/bold magenta]", style="magenta"))

show("19a: 3-line block + ship (compact)", [
    plain("  ▄▀█  █▀█  █▀▀  █▀█          |", "bold cyan"),
    plain("  █▀█  █▀▄  █▄█  █▄█         /|\\", "cyan"),
    plain("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈      / | \\", "dim cyan"),
    plain("                            /  |  \\", "dim cyan"),
    plain("  v0.21.0                \\     /", "dim"),
    plain("  Deploy → production   ~\\___/~~", "dim cyan"),
])

show("19b: 3-line block + ship (side-by-side tight)", [
    plain("  ▄▀█  █▀█  █▀▀  █▀█      |", "bold cyan"),
    plain("  █▀█  █▀▄  █▄█  █▄█     /|\\", "cyan"),
    plain("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈  / | \\", "dim cyan"),
    plain("  v0.21.0              \\   /", "dim"),
    plain("                        ~\\─/~~", "dim cyan"),
])

show("19c: 4-line chunky + ship", [
    plain("  █▀▀█  █▀▀█  █▀▀▀  █▀▀█       |", "bold cyan"),
    plain("  █▄▄█  █▄▄▀  █ ▀█  █  █      /|\\", "cyan"),
    plain("  ▀  ▀  ▀  ▀   ▀▀▀   ▀▀      / | \\", "dim cyan"),
    plain("                              \\   /", "dim cyan"),
    plain("  v0.21.0                  ~\\─/~~", "dim"),
])

show("19d: 4-line chunky + detailed ship", [
    plain("  █▀▀█  █▀▀█  █▀▀▀  █▀▀█        |    |", "bold cyan"),
    plain("  █▄▄█  █▄▄▀  █ ▀█  █  █       )_)  )_)", "cyan"),
    plain("  ▀  ▀  ▀  ▀   ▀▀▀   ▀▀      )___))___)", "dim cyan"),
    plain("                              _____|____|___", "dim cyan"),
    plain("  v0.21.0              -----\\          /---", "dim"),
    plain("                          ~~~~~~\\________/~~~~~", "dim cyan"),
])

show("19e: 2-line block + ship (most compact)", [
    plain("  ▄▀█  █▀█  █▀▀  █▀█      |", "bold cyan"),
    plain("  █▀█  █▀▄  █▄█  █▄█    _/|\\_", "dim cyan"),
    plain("                        \\   /", "dim cyan"),
    plain("  v0.21.0            ~~\\─/~~~", "dim"),
])

show("19f: 2-line block + wave only (no ship)", [
    plain("  ▄▀█  █▀█  █▀▀  █▀█", "bold cyan"),
    plain("  █▀█  █▀▄  █▄█  █▄█", "dim cyan"),
    plain("  ~\u223c~\u223c~\u223c~\u223c~\u223c~\u223c~\u223c~\u223c~\u223c~\u223c", "dim cyan"),
    plain("  v0.21.0  Deploy → production", "dim"),
])

show("19g: 2-line block + simple water line", [
    plain("  ▄▀█  █▀█  █▀▀  █▀█", "bold cyan"),
    plain("  █▀█  █▀▄  █▄█  █▄█", "dim cyan"),
    plain("  ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁", "dim cyan"),
    plain("  v0.21.0  Deploy → production", "dim"),
])


# ══════════════════════════════════════════════════════════════════════
# SECTION 3: Color variations (using 19b layout)
# ══════════════════════════════════════════════════════════════════════

c.print()
c.print(Rule("[bold magenta]SECTION 3: Color variations[/bold magenta]", style="magenta"))

# (name, bold_style, mid_style, dim_style)
palettes = [
    ("C1: cyan (current)",          "bold cyan",           "cyan",            "dim cyan"),
    ("C2: blue",                    "bold blue",           "blue",            "dim blue"),
    ("C3: bright blue",             "bold bright_blue",    "bright_blue",     "dim bright_blue"),
    ("C4: green",                   "bold green",          "green",           "dim green"),
    ("C5: magenta",                 "bold magenta",        "magenta",         "dim magenta"),
    ("C6: bright magenta",          "bold bright_magenta", "bright_magenta",  "dim bright_magenta"),
    ("C7: yellow / gold",           "bold yellow",         "yellow",          "dim yellow"),
    ("C8: red",                     "bold red",            "red",             "dim red"),
    ("C9: white / silver",          "bold white",          "white",           "dim white"),
    ("C10: cyan→blue gradient",     "bold cyan",           "blue",            "dim blue"),
    ("C11: cyan→magenta gradient",  "bold cyan",           "magenta",         "dim magenta"),
    ("C12: green→cyan gradient",    "bold green",          "cyan",            "dim cyan"),
    ("C13: yellow→red gradient",    "bold yellow",         "red",             "dim red"),
    ("C14: white→cyan gradient",    "bold white",          "cyan",            "dim cyan"),
    ("C15: bright_cyan→blue",       "bold bright_cyan",    "cyan",            "dim blue"),
]

for name, b, m, d in palettes:
    show(name, [
        plain("  ▄▀█  █▀█  █▀▀  █▀█      |", b),
        plain("  █▀█  █▀▄  █▄█  █▄█     /|\\", m),
        plain("  ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈  / | \\", d),
        plain("  v0.21.0              \\   /", "dim"),
        plain("                        ~\\─/~~", d),
    ])


# ── Summary ───────────────────────────────────────────────────────────

c.print()
c.print(Rule("[bold]End of showcase[/bold]", style="dim"))
c.print()
c.print("  [dim]Pick: letter style (18a-e), ship style (19a-g), and color (C1-C15).[/dim]")
c.print()
