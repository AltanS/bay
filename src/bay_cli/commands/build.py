"""Build circuit breaker management — bay build status / reset."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.table import Table

from bay_cli import console, paths
from bay_cli.errors import BayError

app = typer.Typer(help="Inspect and reset the webhook build circuit breaker.")


# ---------------------------------------------------------------------------
# Vault / Telegram helpers
# ---------------------------------------------------------------------------

def _decrypt_vault(root: Path, env: str) -> dict:
    """Decrypt group_vars/<env>/secrets.yml via .vault_pass.

    Returns parsed YAML dict, or empty dict if vault unavailable.
    Mirrors the pattern in validate.py::_validate_vault_file.
    """
    vault_pass = root / ".vault_pass"
    vault_file = root / "group_vars" / env / "secrets.yml"
    if not vault_pass.exists() or not vault_file.exists():
        return {}
    try:
        proc = subprocess.run(
            [
                "ansible-vault", "decrypt",
                "--vault-password-file", str(vault_pass),
                "--output=-",
                str(vault_file),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return {}
        return yaml.safe_load(proc.stdout) or {}
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return {}


def _read_telegram_creds(root: Path, env: str) -> tuple[str, str]:
    """Read TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from vault.

    Returns (token, chat_id). Both empty strings if unavailable.
    """
    secrets = _decrypt_vault(root, env)
    token = str(secrets.get("TELEGRAM_BOT_TOKEN", "") or "")
    chat_id = str(secrets.get("TELEGRAM_CHAT_ID", "") or "")
    return token, chat_id


def _send_telegram_audit(token: str, chat_id: str, msg: str) -> None:
    """Post an audit message directly to the Telegram Bot API.

    Silent no-op if token or chat_id are empty. Does NOT depend on
    rebuild.sh internals or systemd env inheritance.
    """
    if not token or not chat_id:
        return
    try:
        subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null",
                "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "--data-urlencode", f"chat_id={chat_id}",
                "--data-urlencode", f"text={msg}",
                "--data-urlencode", "parse_mode=HTML",
            ],
            check=False,
            timeout=10,
        )
    except Exception:
        pass  # audit failure is non-fatal


# ---------------------------------------------------------------------------
# Shared ad-hoc output parser (extracted from _fetch_rig_state)
# ---------------------------------------------------------------------------

def _parse_adhoc_output(raw: str) -> str:
    """Strip ANSI codes and the ansible ad-hoc "host | STATUS | rc=N >>" prefix.

    Returns the payload text (everything after the header line).
    Mirrors the parsing pattern in ops._fetch_rig_state but extracted as a
    shared helper so both callers stay DRY.
    """
    # Strip ANSI escape codes
    clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    lines = [l.strip() for l in clean.splitlines()]
    # Drop the first "host | SUCCESS | rc=0 >>" header line if present
    if lines and re.match(r"^\S.*\|\s*(SUCCESS|CHANGED|FAILED|UNREACHABLE)\s*\|", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _parse_adhoc_json(raw: str) -> dict | None:
    """Parse JSON from ansible ad-hoc shell output.

    Returns parsed dict or None on failure.
    """
    payload = _parse_adhoc_output(raw)
    if not payload:
        return None
    # Extract first JSON object
    json_lines: list[str] = []
    in_json = False
    for line in payload.splitlines():
        if line.startswith("{"):
            in_json = True
        if in_json:
            json_lines.append(line)
        if in_json and line.startswith("}"):
            break
    if not json_lines:
        return None
    try:
        return json.loads("\n".join(json_lines))
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Multi-region host resolution
# ---------------------------------------------------------------------------

def _get_regions(bay_dir: Path) -> dict[str, str]:
    """Return {region_name: host_ip} for multi-region inventories.

    For single-region, returns {None: None} sentinel (no region column needed).
    """
    root = bay_dir.parent
    inventory = root / "hosts" / "production"
    if not inventory.exists():
        return {}

    text = inventory.read_text()
    if "[production:children]" not in text:
        return {}

    children: list[str] = []
    in_children = False
    for line in text.splitlines():
        line = line.strip()
        if line == "[production:children]":
            in_children = True
            continue
        if in_children:
            if line.startswith("["):
                break
            if line and not line.startswith("#"):
                children.append(line)

    groups: dict[str, str] = {}
    current_group: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1].split(":")[0]
            continue
        if current_group and current_group in children and line and not line.startswith("#"):
            if current_group not in groups:
                groups[current_group] = line.split()[0]

    return groups


def _is_multi_region(bay_dir: Path) -> bool:
    return bool(_get_regions(bay_dir))


# ---------------------------------------------------------------------------
# Core data fetcher  (pure, testable — no rendering)
# ---------------------------------------------------------------------------

def _fetch_single_state(
    env: str,
    stack_name: str,
    service: str,
    bay_dir: Path,
    limit: str | None,
) -> dict | None:
    """Read a single service's raw state JSON from the target host.

    Returns None if the file is missing, unreadable, or parse fails. Used by
    --keep-history to preserve the prior last_failure block across reset.
    """
    from bay_cli.commands.ops import _run_on_host

    state_file = f"/opt/{stack_name}/state/{service}.json"
    cmd = (
        f"python3 -c \""
        f"import os, json; "
        f"p='{state_file}'; "
        f"print(json.dumps(json.load(open(p))) if os.path.isfile(p) else '')\""
    )
    try:
        result = _run_on_host(
            env, cmd, bay_dir=bay_dir, capture=True, check=False, limit=limit,
        )
        raw = result.stdout.strip() if result.stdout else ""
    except Exception:
        return None
    payload = _parse_adhoc_output(raw)
    if not payload:
        return None
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None


def _fetch_build_states(
    env: str,
    stack_name: str,
    bay_dir: Path,
    limit: str | None,
) -> list[dict]:
    """Fetch CB state JSON files from the target host(s).

    Returns a list of dicts, one per service state file found.
    Each dict has keys:
        service, cb_state, consecutive_failures, opened_at,
        last_sha, last_stage, last_reason, last_at,
        last_blocked_alert_at, region (may be None), raw
    """
    from bay_cli.commands.ops import _run_on_host

    state_dir = f"/opt/{stack_name}/state"
    cmd = (
        f"python3 -c \""
        f"import os, json, sys; "
        f"d='{state_dir}'; "
        f"files=[f for f in (os.listdir(d) if os.path.isdir(d) else []) if f.endswith('.json')]; "
        f"out={{}}; "
        f"[out.update({{f[:-5]: json.loads(open(os.path.join(d,f)).read())}}) for f in files]; "
        f"print(json.dumps(out))\""
    )

    try:
        result = _run_on_host(
            env,
            cmd,
            bay_dir=bay_dir,
            capture=True,
            check=False,
            limit=limit,
        )
        raw = result.stdout.strip() if result.stdout else ""
    except Exception:
        return []

    if not raw:
        return []

    payload = _parse_adhoc_output(raw)
    if not payload:
        return []

    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(data, dict):
        return []

    states: list[dict] = []
    for svc, state in sorted(data.items()):
        if not isinstance(state, dict):
            continue
        failures = state.get("consecutive_failures", state.get("failures", 0))
        opened_at = state.get("opened_at")
        cb_open = opened_at is not None
        last_f = state.get("last_failure") or {}
        if not isinstance(last_f, dict):
            last_f = {"at": str(last_f)}
        alerts = state.get("alerts") or {}
        states.append({
            "service": svc,
            "cb_state": "OPEN" if cb_open else "closed",
            "consecutive_failures": failures,
            "opened_at": opened_at or "",
            "last_sha": (last_f.get("sha") or "")[:8],
            "last_stage": last_f.get("stage") or "",
            "last_reason": last_f.get("reason") or "",
            "last_at": last_f.get("at") or "",
            "last_blocked_alert_at": alerts.get("last_blocked_alert_at") or "",
            "region": None,  # populated by caller for multi-region
            "raw": state,
        })
    return states


# ---------------------------------------------------------------------------
# Table renderer  (display only — no I/O)
# ---------------------------------------------------------------------------

_REASON_MAX = 60


def _truncate(s: str, n: int = _REASON_MAX) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "\u2026"


def _render_status_table(
    states: list[dict],
    *,
    multi_region: bool = False,
    verbose: bool = False,
) -> Table:
    """Build a Rich Table from a list of CB state dicts."""
    cols = []
    if multi_region:
        cols.append("Region")
    cols += ["Service", "CB State", "Failures", "Opened At", "Last SHA", "Last Stage", "Last Reason", "Last At"]
    if verbose:
        cols.append("Last Blocked Alert")

    table = Table(show_header=True, header_style="bold")
    for col in cols:
        table.add_column(col)

    for s in states:
        cb_style = "bold red" if s["cb_state"] == "OPEN" else "green"
        row = []
        if multi_region:
            row.append(s.get("region") or "")
        row += [
            s["service"],
            f"[{cb_style}]{s['cb_state']}[/{cb_style}]",
            str(s["consecutive_failures"]),
            s["opened_at"] or "\u2014",
            s["last_sha"] or "\u2014",
            s["last_stage"] or "\u2014",
            _truncate(s["last_reason"]) if s["last_reason"] else "\u2014",
            s["last_at"] or "\u2014",
        ]
        if verbose:
            row.append(s["last_blocked_alert_at"] or "\u2014")
        table.add_row(*row)

    return table


# ---------------------------------------------------------------------------
# `bay build status` command
# ---------------------------------------------------------------------------

@app.command()
def status(
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: Optional[str] = typer.Option(None, "--region", "-r", help="Filter to a specific region."),
    service: Optional[str] = typer.Option(None, "--service", "-s", help="Show full state for one service."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show last_blocked_alert_at column."),
) -> None:
    """Show circuit breaker state for all services on the target host(s).

    The breaker opens after git_deploy_cb_max_failures consecutive build
    failures (default 5). While OPEN, rebuild.sh silently ignores pushes —
    the webhook still logs "triggered N services", so the webhook looks
    healthy while the service is stuck. This command is how you see it.

    Examples:

        bin/bay build status
        bin/bay build status --service myapp
        bin/bay build status --region na --verbose
    """
    from bay_cli.commands.ops import (
        _read_stack_name,
        _resolve_target_host,
        _validate_env,
    )

    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    _validate_env(env, root)

    stack_name = _read_stack_name(root)
    multi = _is_multi_region(bay_dir)
    regions = _get_regions(bay_dir) if multi else {}

    all_states: list[dict] = []

    if multi and regions:
        target_regions = {region: regions[region]} if (region and region in regions) else regions
        for rname, rhost in sorted(target_regions.items()):
            states = _fetch_build_states(env, stack_name, bay_dir, rhost)
            for s in states:
                s["region"] = rname
            all_states.extend(states)
    else:
        limit = _resolve_target_host(bay_dir, region)
        all_states = _fetch_build_states(env, stack_name, bay_dir, limit)

    if not all_states:
        console.info("No build state found")
        return

    # --service: dump full state block
    if service:
        matches = [s for s in all_states if s["service"] == service]
        if not matches:
            console.warning(f"No state found for service '{service}'")
            return
        for s in matches:
            console.console.print(json.dumps(s["raw"], indent=2))
        return

    table = _render_status_table(all_states, multi_region=multi, verbose=verbose)
    console.console.print(table)


# ---------------------------------------------------------------------------
# `bay build reset` command
# ---------------------------------------------------------------------------

@app.command()
def reset(
    service: Optional[str] = typer.Argument(None, help="Service to reset. Omit with --all to reset all."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: Optional[str] = typer.Option(None, "--region", "-r", help="Target a specific region."),
    all_services: bool = typer.Option(False, "--all", help="Reset all services on the target host(s)."),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt."),
    keep_history: bool = typer.Option(
        False,
        "--keep-history",
        help="Preserve the last_failure block (SHA/stage/timestamp) when resetting — useful for post-incident review.",
    ),
) -> None:
    """Reset the build circuit breaker after fixing the underlying issue.

    Writes a clean state file on the host and sends a Telegram audit
    message. In multi-region setups, omitting --region resets on EVERY
    region. After the reset, push again or touch the trigger file
    (/opt/<stack>/triggers/<svc>.trigger) to re-fire the build.

    Examples:

        bin/bay build reset myapp
        bin/bay build reset myapp --keep-history
        bin/bay build reset myapp --region na
        bin/bay build reset --all --force
    """
    from bay_cli.commands.ops import (
        _all_service_names,
        _read_stack_name,
        _resolve_target_host,
        _run_on_host,
        _validate_env,
        _validate_service_name,
    )

    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    _validate_env(env, root)

    if not service and not all_services:
        raise BayError("Specify a service name or pass --all")
    if service and all_services:
        raise BayError("Cannot pass both a service name and --all")

    stack_name = _read_stack_name(root)
    multi = _is_multi_region(bay_dir)
    regions = _get_regions(bay_dir) if multi else {}

    # Resolve target hosts
    if multi and regions:
        target_regions = {region: regions[region]} if (region and region in regions) else regions
    else:
        limit = _resolve_target_host(bay_dir, region)
        target_regions = {"": limit} if limit else {"": None}

    # Resolve services to reset
    if all_services:
        services_to_reset = _all_service_names(root)
    else:
        _validate_service_name(service, root)  # type: ignore[arg-type]
        services_to_reset = [service]  # type: ignore[list-item]

    if not services_to_reset:
        console.warning("No services found")
        return

    # Build host display for confirmation
    host_display_parts = []
    for rname, rhost in sorted(target_regions.items()):
        if rname:
            host_display_parts.append(f"{rname} ({rhost})")
        elif rhost:
            host_display_parts.append(rhost)
        else:
            host_display_parts.append(env)
    host_display = ", ".join(host_display_parts) if host_display_parts else env

    n = len(services_to_reset)
    svc_label = services_to_reset[0] if n == 1 else f"{n} services"

    # Confirmation prompt
    if not force and not console.is_yes_mode():
        console.console.print()
        console.console.print(
            f"  This will reset CB for [bold]{svc_label}[/bold] on [bold]{host_display}[/bold]."
        )
        console.console.print(
            "  This clears consecutive failures and allows new builds."
        )
        console.console.print(
            "  A Telegram audit message will be sent."
        )
        console.console.print()
        typer.confirm(f"Reset CB for {svc_label} on {host_display}?", abort=True)

    # Clean schema-v1 state template
    clean_state = {
        "version": 1,
        "consecutive_failures": 0,
        "opened_at": None,
        "last_failure": {
            "sha": "",
            "stage": "",
            "reason": "",
            "at": "",
        },
        "alerts": {
            "opened_sent": False,
            "last_blocked_alert_at": None,
        },
    }
    clean_json = json.dumps(clean_state)

    # Atomic write via os.rename (mirrors rebuild.sh _write_state pattern)
    write_cmd_template = (
        "python3 -c \""
        "import json, os, tempfile; "
        "d = '/opt/{stack}/state'; "
        "os.makedirs(d, exist_ok=True); "
        "dst = os.path.join(d, '{svc}.json'); "
        "fd, tmp = tempfile.mkstemp(dir=d); "
        "os.write(fd, {json_repr}); "
        "os.close(fd); "
        "os.chmod(tmp, 0o644); "
        "os.rename(tmp, dst)\""
    )

    # Read Telegram credentials once (non-fatal if unavailable)
    token, chat_id = _read_telegram_creds(root, env)

    for rname, rhost in sorted(target_regions.items()):
        for svc in services_to_reset:
            svc_state = clean_state
            if keep_history:
                existing = _fetch_single_state(env, stack_name, svc, bay_dir, rhost)
                if existing and existing.get("last_failure"):
                    svc_state = {**clean_state, "last_failure": existing["last_failure"]}
            svc_json = json.dumps(svc_state)
            cmd = write_cmd_template.format(
                stack=stack_name,
                svc=svc,
                json_repr=repr(svc_json.encode()),
            )
            try:
                _run_on_host(
                    env,
                    cmd,
                    bay_dir=bay_dir,
                    capture=True,
                    limit=rhost,
                )
            except Exception as exc:
                console.error(f"Reset failed for {svc}: {exc}")
                continue

            # Telegram audit
            host_label = f"{rname} ({rhost})" if rname else (rhost or env)
            history_note = " (last_failure preserved)" if keep_history else ""
            audit_msg = (
                f"CB reset for <b>{svc}</b> by operator on {host_label}{history_note}\n"
                f"Consecutive failures cleared. New builds are now allowed."
            )
            _send_telegram_audit(token, chat_id, audit_msg)

            console.success(f"CB reset: {svc}" + (f" [{rname}]" if rname else ""))
