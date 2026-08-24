#!/usr/bin/env python3
"""Regenerate the alert table in docs/alerting.md from alerts/registry.yml.

A hand-maintained copy of the registry in prose is a second source of truth,
and it will drift. The table between the markers below is generated; `make
docs-alerts` rewrites it and CI asserts regenerating produces no diff.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "alerts" / "registry.yml"
DOC = ROOT / "docs" / "alerting.md"

BEGIN = "<!-- BEGIN GENERATED ALERT TABLE -->"
END = "<!-- END GENERATED ALERT TABLE -->"


def render_table() -> str:
    registry = YAML(typ="safe").load(REGISTRY.open())
    rows = [
        "| Alert ID | Level | Default | Source | Summary |",
        "|---|---|---|---|---|",
    ]
    for alert_id in sorted(registry):
        entry = registry[alert_id] or {}
        # The Default column is why this table is generated rather than written:
        # "does this alert reach me out of the box" is the first question an
        # operator asks, and it is a registry fact, not prose.
        state = "on" if entry.get("enabled_by_default", True) else "**off**"
        rows.append(
            f"| `{alert_id}` | `{entry.get('level', '')}` | {state} | "
            f"`{entry.get('source', '')}` | {entry.get('summary', '')} |"
        )
    return "\n".join(rows)


def main() -> int:
    text = DOC.read_text()
    if BEGIN not in text or END not in text:
        print(f"markers not found in {DOC}", file=sys.stderr)
        return 1
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    DOC.write_text(f"{head}{BEGIN}\n\n{render_table()}\n\n{END}{tail}")
    print(f"regenerated {len(render_table().splitlines()) - 2} alert rows in {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
