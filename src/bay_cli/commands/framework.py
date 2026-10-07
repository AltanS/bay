"""Framework status: the installed version, the fleet, and the feature summary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from bay_cli import console
from bay_cli.context import context_from


def status(
    ctx: typer.Context,
    as_json: bool = typer.Option(
        False,
        "--json",
        help=(
            "Print one JSON document: framework, fleet, and the deploy receipt "
            "of every box (read over SSH). Schema: docs/deploy-receipt.md."
        ),
    ),
    env: Optional[str] = typer.Option(
        None,
        "--env",
        "-e",
        help=(
            "With --json: read the receipts of this environment only. Without "
            "--json: also print the receipt of each box of this environment."
        ),
    ),
    no_remote: bool = typer.Option(
        False, "--no-remote", help="Do not contact any box (with --json, boxes is empty)."
    ),
) -> None:
    """Show the installed Bay version, the fleet it works on, and the feature flags.

    With --json, print a stable JSON document instead (status_version 2):
    the Bay version, where the fleet is and how Bay found it, the fleet
    commit, and what each box last deployed, from the receipt the box wrote.
    Never prompts: SSH runs in batch mode, and a box that cannot be read
    gets an error string.

    Examples:

        bay status
        bay status --json
        bay status --json --env production
        bay status --json --no-remote
        bay status --env production
    """
    cx = context_from(ctx)

    if as_json or console.is_json_mode():
        from bay_cli.receipts import status_document

        doc = status_document(cx, env=env, remote=not no_remote)
        if as_json:
            print(json.dumps(doc, indent=2))
        else:
            console.emit_result(doc, command="status")
        return

    from bay_cli.receipts import framework_state, fleet_state

    framework = framework_state(cx)
    fleet = fleet_state(cx)

    console.show_banner(cx, subtitle="Status")

    version = framework["version"]
    console.console.print(f"  Version: [bold]{version}[/bold]" if version else "  Version: [dim]unknown[/dim]")
    console.console.print(f"  Install: [dim]{framework['path']}[/dim]")
    console.console.print(f"  Fleet:   [bold]{fleet['root']}[/bold]  [dim]({fleet['source']})[/dim]")

    # Feature summary
    _show_feature_summary(cx.fleet_root)

    if env and not no_remote:
        from bay_cli.receipts import fetch_receipts

        _show_receipts(env, fetch_receipts(cx, env))

    console.console.print()


def receipt_line(entry: dict) -> str:
    """One plain line for a box of ``bay status --env``: what its receipt says.

    The route count shows only for a receipt that lists routes (the ingress
    box). A receipt written before 2.3 has no ``routes`` key.
    """
    box = entry.get("box") or "?"
    if entry.get("error"):
        return f"{box}: {entry['error']}"
    receipt = entry.get("receipt")
    if not isinstance(receipt, dict):
        return f"{box}: no receipt"
    containers = receipt.get("containers") or []
    parts = [
        str(receipt.get("result") or "unknown"),
        str(receipt.get("deployed_at") or "?"),
        f"{len(containers)} container{'' if len(containers) == 1 else 's'}",
    ]
    routes = receipt.get("routes")
    if isinstance(routes, list) and routes:
        parts.append(f"{len(routes)} route{'' if len(routes) == 1 else 's'}")
    return f"{box}: " + ", ".join(parts)


def _show_receipts(env: str, entries: list[dict]) -> None:
    console.header(f"Receipts ({env})")
    for entry in entries:
        console.console.print(f"  {receipt_line(entry)}", markup=False, highlight=False)


# ── Feature summary for status command ────────────────────────────────

# (variable_name, display_label, framework_default)
_FEATURE_FLAGS: list[tuple[str, str, bool]] = [
    ("backup_enabled", "Backups", False),
    ("crowdsec_enabled", "CrowdSec IDS/IPS", True),
    ("watchtower_enabled", "Watchtower", True),
    ("sshd_hardening_enabled", "SSH Hardening", True),
    ("docker_monitor_enabled", "Docker Monitor", True),
    ("debug_agent_enabled", "Debug Agent", False),
]


def _show_feature_summary(root: Path) -> None:
    """Parse group_vars and show a feature overview table."""
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    # Collect all values from group_vars (non-vault files only)
    overrides: dict[str, bool] = {}
    yaml = YAML()
    yaml.preserve_quotes = True

    group_vars = root / "group_vars"
    if not group_vars.is_dir():
        return

    for subdir in sorted(group_vars.iterdir()):
        if not subdir.is_dir():
            continue
        for f in sorted(subdir.iterdir()):
            if f.suffix not in (".yml", ".yaml") or not f.is_file():
                continue
            try:
                first_line = f.read_text(errors="replace").split("\n", 1)[0]
                if first_line.strip().startswith("$ANSIBLE_VAULT"):
                    continue  # skip encrypted files
                with f.open() as fh:
                    data = yaml.load(fh)
                if not isinstance(data, dict):
                    continue
                for var_name, _, _ in _FEATURE_FLAGS:
                    if var_name in data:
                        overrides[var_name] = bool(data[var_name])
            except (OSError, YAMLError):
                continue

    console.header("Features")

    from rich.table import Table

    table = Table(show_header=True, show_edge=False, pad_edge=False, padding=(0, 2))
    table.add_column("Feature", style="bold")
    table.add_column("Status")
    table.add_column("Source", style="dim")

    for var_name, label, default in _FEATURE_FLAGS:
        if var_name in overrides:
            value = overrides[var_name]
            source = "group_vars"
        else:
            value = default
            source = "default"

        status_str = (
            "[green]enabled[/green]" if value else "[dim]disabled[/dim]"
        )
        table.add_row(label, status_str, source)

    console.console.print(table)
