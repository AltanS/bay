#!/usr/bin/env python3
"""Regenerate the generated blocks in SKILL.md.

SKILL.md is the single-file orientation document for an agent (or a human)
dropped into a Bay consumer repo: what Bay is, the rules that are not
negotiable, the whole CLI surface, and where the deep docs live.

Two of its sections are compiled, because a hand-written copy of the command
tree is a second source of truth and will drift:

  * the command reference — walked out of the Typer app itself
  * the documentation map — parsed from the curated tables in docs/README.md

Everything outside the BEGIN/END markers is hand-written and preserved.
`make docs-skill` rewrites it; a test asserts regenerating produces no diff.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import click
import typer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DOC = ROOT / "SKILL.md"
DOCS_README = ROOT / "docs" / "README.md"

CLI_BEGIN = "<!-- BEGIN GENERATED CLI REFERENCE -->"
CLI_END = "<!-- END GENERATED CLI REFERENCE -->"
DOCS_BEGIN = "<!-- BEGIN GENERATED DOC MAP -->"
DOCS_END = "<!-- END GENERATED DOC MAP -->"

DEFAULT_PANEL = "Other"

# Commands deliberately kept out of the orientation doc. A walker has no
# editorial stance; this is where we supply one. Transitional and one-shot
# migration tools are noise to an agent asking "what can I do here?" — they
# are still discoverable in `--help`.
SKIP_COMMANDS = {
    "gateway migrate-namespace",  # one-shot migration
}

# Docs the map omits. Superseded research is worse than absent in an
# orientation doc — an agent has no way to weigh "historical" against the
# rest. Anything under docs/ that is neither emitted nor listed here is a
# hard error, so a new doc cannot be silently dropped.
SKIP_DOCS = {
    "docs/README.md",  # the source of the map
    "docs/external-tailscale-research.md",  # superseded M38 research
    "docs/external-tailscale-implementation-plan.md",  # never built
    "docs/rename-map.md",  # legacy-argo: M108 rename working doc, dies when the transition shim is finally removed (a future major release, not v1.1)
}


def _summary(cmd: click.Command) -> str:
    """First sentence of the command's help, collapsed to one line.

    Source comments ride along in docstrings (`legacy-argo:` tags, milestone
    notes) and this text ships to consumers and, eventually, to the public
    repo — so a trailing ` # ...` is stripped rather than published.
    """
    text = (cmd.help or cmd.short_help or "").strip()
    if not text:
        return ""
    first = text.split("\n\n", 1)[0]
    first = re.sub(r"\s+#\s.*$", "", first, flags=re.DOTALL)
    return " ".join(first.split())


def _signature(cmd: click.Command) -> str:
    """Positional arguments as `<env>`-style placeholders."""
    parts = []
    for param in cmd.params:
        if not isinstance(param, click.Argument):
            continue
        name = param.name or ""
        if param.nargs == -1:
            parts.append(f"[{name}...]")
        elif param.required:
            parts.append(f"<{name}>")
        else:
            parts.append(f"[{name}]")
    return " ".join(parts)


def _panel(cmd: click.Command) -> str:
    return getattr(cmd, "rich_help_panel", None) or DEFAULT_PANEL


def _walk(group: click.Group, prefix: str = "") -> list[tuple[str, click.Command, str]]:
    """Flatten a click group into (path, command, panel) triples, depth-first."""
    rows: list[tuple[str, click.Command, str]] = []
    ctx = click.Context(group)
    for name in sorted(group.list_commands(ctx)):
        cmd = group.get_command(ctx, name)
        if cmd is None or cmd.hidden:
            continue
        path = f"{prefix}{name}"
        if path in SKIP_COMMANDS:
            continue
        if isinstance(cmd, click.Group):
            panel = _panel(cmd)
            rows.append((path, cmd, panel))
            for sub_path, sub_cmd, _ in _walk(cmd, prefix=f"{path} "):
                rows.append((sub_path, sub_cmd, panel))
        else:
            rows.append((path, cmd, _panel(cmd)))
    return rows


def render_cli() -> str:
    """One line per command: the inventory, not the manual.

    Flags are deliberately omitted. `bin/bay <cmd> --help` is live and always
    correct, so a compiled copy of it buys nothing but context; what --help
    cannot tell an agent is which commands *exist*. The handful of flags that
    change what a command means (--rig, --skip-validate, ...) are called out
    by hand in SKILL.md instead.
    """
    from bay_cli.cli import app

    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)

    rows = _walk(root)
    panels: dict[str, list[tuple[str, click.Command]]] = {}
    for path, cmd, panel in rows:
        panels.setdefault(panel, []).append((path, cmd))

    order = [
        "Framework",
        "Operations",
        "Stack Manager",
        "Vault",
        "Backup",
        "Utilities",
    ]
    names = [p for p in order if p in panels] + sorted(set(panels) - set(order))

    out: list[str] = []
    for panel in names:
        out.append(f"### {panel}\n")
        for path, cmd in panels[panel]:
            sig = _signature(cmd)
            invocation = f"bin/bay {path}{' ' + sig if sig else ''}"
            summary = _summary(cmd)
            out.append(f"- `{invocation}`{' — ' + summary if summary else ''}")
        out.append("")
    return "\n".join(out).rstrip()


_ROW = re.compile(r"^\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*(.*?)\s*\|\s*$")


def render_doc_map() -> str:
    """Re-emit the curated docs/README.md tables as a flat, section-grouped list.

    Fails loud, twice over. A row this parser cannot read (a bolded link, a
    trailing annotation, an escaped pipe) used to be skipped silently: the
    doc vanished from the map, regeneration succeeded and the drift test
    stayed green. So an unreadable link row is an error, and every file under
    docs/ must be either emitted or explicitly skipped.
    """
    section = ""
    out: list[str] = []
    seen: set[str] = set()
    for lineno, line in enumerate(DOCS_README.read_text().splitlines(), 1):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if not line.startswith("|"):
            continue
        match = _ROW.match(line)
        if not match:
            if "](" in line:
                raise SystemExit(
                    f"{DOCS_README}:{lineno}: table row has a link this parser "
                    f"cannot read — fix the row or teach _ROW about it:\n  {line}"
                )
            continue
        _label, href, what = match.groups()
        # docs/README.md links are relative to docs/; SKILL.md quotes paths
        # from the framework root so they can be opened from a consumer.
        href = href[3:] if href.startswith("../") else f"docs/{href}"
        seen.add(href)
        if href in SKIP_DOCS:
            continue
        if section:
            out.append(f"\n**{section}**\n")
            section = ""
        out.append(f"- `{href}` — {what}")

    on_disk = {
        str(p.relative_to(ROOT))
        for p in (ROOT / "docs").rglob("*.md")
    }
    missing = sorted(on_disk - seen - SKIP_DOCS)
    if missing:
        raise SystemExit(
            "docs present on disk but absent from the docs/README.md index:\n  "
            + "\n  ".join(missing)
            + f"\nAdd them to {DOCS_README.relative_to(ROOT)} or to SKIP_DOCS "
            "in this script."
        )
    return "\n".join(out).strip()


def _replace(text: str, begin: str, end: str, body: str) -> str:
    if begin not in text or end not in text:
        raise SystemExit(f"markers not found in {DOC}: {begin}")
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{begin}\n\n{body}\n\n{end}{tail}"


def main(argv: list[str] | None = None) -> int:
    check = "--check" in (argv if argv is not None else sys.argv[1:])

    current = DOC.read_text()
    text = _replace(current, CLI_BEGIN, CLI_END, render_cli())
    text = _replace(text, DOCS_BEGIN, DOCS_END, render_doc_map())

    if check:
        # Compare in memory — never touch the file. The drift test uses this,
        # so a verification run cannot leave a half-written SKILL.md behind.
        if text != current:
            print(
                f"{DOC.relative_to(ROOT)} is stale — run `make docs-skill`",
                file=sys.stderr,
            )
            return 1
        print(f"{DOC.relative_to(ROOT)} is up to date")
        return 0

    DOC.write_text(text)
    print(f"regenerated {DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
