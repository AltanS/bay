"""Backup management commands: list, run, restore, status, check."""

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

import yaml as _yaml

from bay_cli import ansible, console as con, paths, runner
from bay_cli.errors import BayError

app = typer.Typer(help="Manage restic backups (list, run, restore, status, check).")

_console = Console()


def _get_stack_name(bay_dir: Path) -> str:
    """Read stack_name from consumer group_vars, default to 'bay'."""
    main_yml = bay_dir.parent / "group_vars" / "all" / "main.yml"
    if main_yml.exists():
        try:
            data = _yaml.safe_load(main_yml.read_text()) or {}
            return data.get("stack_name", "bay")
        except Exception:
            pass
    return "bay"


def _run_on_host(
    env: str,
    cmd: str,
    *,
    bay_dir: Path,
    capture: bool = True,
    message: str = "",
    check: bool = True,
    limit: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command on the target host(s) via ansible ad-hoc.

    ``limit`` restricts execution to a subset of the ``env`` group via
    ``--limit`` (e.g. a comma-separated host list). When None, the whole
    group is targeted (unchanged default behavior).
    """
    uv_cmd = ansible._uv_run_cmd(bay_dir)
    full_cmd = [
        *uv_cmd,
        "ansible", env,
        "-m", "ansible.builtin.shell",
        "-a", cmd,
        "--become",
    ]
    if limit:
        full_cmd += ["--limit", limit]
    return runner.run(
        full_cmd,
        capture=capture,
        message=message,
        env=ansible._collections_env(bay_dir),
        check=check,
    )


def _restic_env_source(stack_name: str = "bay") -> str:
    """Source restic.env for the given stack."""
    return f"set -a && . /opt/{stack_name}/backup/restic.env && set +a"


def _restic_repo(accessory: str) -> str:
    """Build RESTIC_REPOSITORY from env vars + accessory name."""
    return f's3:${{BACKUP_S3_ENDPOINT}}/${{BACKUP_S3_BUCKET}}/${{BACKUP_S3_PREFIX}}/${{BACKUP_HOSTNAME}}/{accessory}'


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _extract_json_from_ansible(stdout: str) -> str | None:
    """Extract JSON array from ansible ad-hoc output.

    Ansible output format: 'hostname | SUCCESS | rc=0 >>\\n<json>'
    Handles ANSI color codes in ansible output.
    """
    clean = _strip_ansi(stdout)
    marker = ">>"
    idx = clean.find(marker)
    if idx == -1:
        return None
    after = clean[idx + len(marker):].strip()
    # Find the JSON array start
    json_start = after.find("[")
    if json_start == -1:
        return None
    return after[json_start:]


def _extract_host_json_arrays(stdout: str) -> list[tuple[str, list]]:
    """Parse a per-host JSON array from each ansible ad-hoc host block.

    Multi-host ad-hoc output contains one block per host (each starting with a
    ``host | SUCCESS | rc=0 >>`` header, body lines following until the next
    header). Each host has its OWN restic repo, so a single ``json.loads`` over
    the whole stdout breaks on the second host's block ("Extra data"). This
    splits into blocks and parses each independently, skipping blocks with no
    valid JSON array (e.g. an empty repo). Robust to ANSI.
    """
    clean = _strip_ansi(stdout)
    blocks: list[tuple[str, list]] = []
    current_host: str | None = None
    buf: list[str] = []

    def _flush() -> None:
        if current_host is None:
            return
        body = "\n".join(buf)
        start = body.find("[")
        if start == -1:
            return
        try:
            parsed = json.loads(body[start:])
        except json.JSONDecodeError:
            return
        if isinstance(parsed, list):
            blocks.append((current_host, parsed))

    for line in clean.split("\n"):
        m = _ANSIBLE_HEADER_RE.match(line.strip())
        if m:
            _flush()
            current_host = m.group("host")
            buf = []
        else:
            buf.append(line)
    _flush()
    return blocks


# Stdout marker the probe echoes when the backup script exists. The probe is
# crafted to exit 0 on EVERY host (an `if` with no else), so absent hosts are
# not FAILED and runner.run stays quiet (it prints captured output on any
# non-zero ad-hoc exit, regardless of `check=False`). Presence is therefore
# keyed off this marker in the host's output block, not the rc.
_PRESENT_MARKER = "__BAY_BACKUP_PRESENT__"

# Ansible ad-hoc per-host header, e.g.:
#   "host | SUCCESS | rc=0 >>"  /  "host | FAILED | rc=2 >>"
#   "host | UNREACHABLE! => {...}"  /  "host | FAILED! => {...}"
_ANSIBLE_HEADER_RE = re.compile(
    r"^(?P<host>\S+)\s*\|\s*(?P<status>[A-Z_]+!?)\s*(?:\|\s*rc=(?P<rc>-?\d+)\s*)?(?:>>|=>)"
)


def _parse_present_hosts(stdout: str) -> list[str]:
    """Parse ansible ad-hoc output and return hosts whose block contains the marker.

    The probe exits 0 on every host, so every host reports SUCCESS and rc can
    no longer distinguish presence. Instead, the output is split into per-host
    blocks (a header line ``host | SUCCESS | rc=0 >>`` starts a block; body
    lines follow until the next header) and a host counts as present only when
    its block body contains ``_PRESENT_MARKER``. UNREACHABLE hosts emit no
    marker and are correctly treated as absent. Robust to ANSI color codes.
    """
    clean = _strip_ansi(stdout)
    hosts: list[str] = []
    current_host: str | None = None
    has_marker = False

    def _flush() -> None:
        if current_host is not None and has_marker:
            hosts.append(current_host)

    for line in clean.split("\n"):
        m = _ANSIBLE_HEADER_RE.match(line.strip())
        if m:
            _flush()
            current_host = m.group("host")
            has_marker = False
        elif _PRESENT_MARKER in line:
            has_marker = True
    _flush()
    return hosts


def _hosts_with_target(
    env: str,
    target: str,
    *,
    bay_dir: Path,
    stack: str,
) -> list[str]:
    """Return the host(s) in ``env`` that have ``backup-<target>.sh``.

    Runs a noiseless ad-hoc probe across the whole group: an ``if test -f``
    with no ``else`` so every host exits 0 (absent hosts are NOT FAILED, so
    runner.run prints nothing), echoing ``_PRESENT_MARKER`` only where the
    script exists. Presence is parsed from the marker, not the rc.
    """
    test_cmd = (
        f'if test -f /opt/{stack}/backup/backup-{target}.sh; '
        f'then echo {_PRESENT_MARKER}; fi'
    )
    result = _run_on_host(env, test_cmd, bay_dir=bay_dir, check=False)
    return _parse_present_hosts(result.stdout or "")


@app.command("list")
def list_snapshots(
    accessory: str = typer.Argument(..., help="Accessory name (e.g., postgres)."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
) -> None:
    """List backup snapshots for an accessory (newest first).

    In multi-region setups, snapshots from every host that backs up the
    accessory are merged into one list.

    Examples:

        bin/bay backup list postgres
        bin/bay backup list headscale
    """
    bay_dir = paths.find_bay_dir()

    con.header(f"Snapshots for {accessory}")

    stack = _get_stack_name(bay_dir)
    hosts = _hosts_with_target(env, accessory, bay_dir=bay_dir, stack=stack)
    if not hosts:
        con.info(f"No host has a '{accessory}' backup configured.")
        return

    cmd = (
        f'{_restic_env_source(stack)} && '
        f'export RESTIC_REPOSITORY="{_restic_repo(accessory)}" && '
        f'/usr/local/bin/restic snapshots --json --retry-lock 15m'
    )

    result = _run_on_host(
        env,
        cmd,
        bay_dir=bay_dir,
        message=f"Fetching snapshots for {accessory}...",
        limit=",".join(hosts),
    )

    # Each targeted host has its own restic repo — aggregate snapshots from
    # every host block (a multi-host target returns one block per host).
    stdout = result.stdout
    host_blocks = _extract_host_json_arrays(stdout)
    snapshots = [snap for _host, snaps in host_blocks for snap in snaps]
    if not snapshots:
        con.info("No snapshots found.")
        return
    # Sort oldest→newest so the reversed() display below shows newest first
    # across the merged multi-host set.
    snapshots.sort(key=lambda s: s.get("time", ""))

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=4)
    table.add_column("ID", width=10)
    table.add_column("Date", width=20)
    table.add_column("Size", width=10, justify="right")
    table.add_column("Tags")

    for i, snap in enumerate(reversed(snapshots), 1):
        snap_time = snap.get("time", "")[:19].replace("T", " ")
        tags = ", ".join(snap.get("tags", []))
        table.add_row(str(i), snap.get("short_id", ""), snap_time, "\u2014", tags)

    _console.print(table)


@app.command()
def run(
    accessory: Optional[str] = typer.Argument(None, help="Accessory name. If omitted, runs all."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
) -> None:
    """Trigger a backup now (one accessory, or all).

    Examples:

        bin/bay backup run postgres
        bin/bay backup run
    """
    bay_dir = paths.find_bay_dir()

    stack = _get_stack_name(bay_dir)
    if accessory:
        con.header(f"Running backup for {accessory}")
        hosts = _hosts_with_target(env, accessory, bay_dir=bay_dir, stack=stack)
        if not hosts:
            con.info(f"No host has a '{accessory}' backup configured.")
            return
        cmd = f'/opt/{stack}/backup/backup-{accessory}.sh'
        _run_on_host(env, cmd, bay_dir=bay_dir, capture=False, limit=",".join(hosts))
        con.success(f"Backup complete for {accessory}")
    else:
        con.header("Running all backups")
        # nullglob via the glob check keeps hosts with zero backup-*.sh from
        # hard-failing the whole group (mirrors status's `2>/dev/null` tolerance).
        cmd = (
            'for script in /opt/{stack}/backup/backup-*.sh; do '
            '[ -e "$script" ] || continue; '
            'echo "==> Running $script"; bash "$script"; '
            'done'
        ).format(stack=stack)
        _run_on_host(env, cmd, bay_dir=bay_dir, capture=False)
        con.success("All backups complete")


@app.command()
def restore(
    env: str = typer.Argument(..., help="Target environment (e.g., production)."),
    accessory: str = typer.Argument(..., help="Accessory name (e.g., postgres)."),
    snapshot: Optional[str] = typer.Option(None, "--snapshot", "-s", help="Snapshot ID (omit to pick interactively from the latest 10)."),
) -> None:
    """Restore an accessory from a backup snapshot (interactive).

    Streams the snapshot into the container and validates afterwards
    (postgres: table count > 0, redis: PONG). A safety snapshot tagged
    `pre-restore` is taken first. File-method targets — including the
    automatic headscale state backup — cannot be restored by this command;
    see docs/backups.md for the manual restic procedure.

    Examples:

        bin/bay backup restore production postgres
        bin/bay backup restore production postgres --snapshot ab12cd34
    """
    bay_dir = paths.find_bay_dir()
    stack = _get_stack_name(bay_dir)

    con.header(f"Restore {accessory} on {env}")

    # List available snapshots
    list_cmd = (
        f'{_restic_env_source(stack)} && '
        f'export RESTIC_REPOSITORY="{_restic_repo(accessory)}" && '
        f'/usr/local/bin/restic snapshots --json --retry-lock 15m'
    )
    result = _run_on_host(env, list_cmd, bay_dir=bay_dir, message="Fetching snapshots...")

    stdout = result.stdout
    json_str = _extract_json_from_ansible(stdout)
    if json_str is None:
        con.error("No snapshots found.")
        raise typer.Exit(1)

    try:
        snapshots = json.loads(json_str)
    except json.JSONDecodeError:
        con.error("Could not parse snapshot list from restic output.")
        raise typer.Exit(1)
    if not snapshots:
        con.error("No snapshots found.")
        raise typer.Exit(1)

    # Display snapshots
    _console.print("\nAvailable snapshots:")
    for i, snap in enumerate(reversed(snapshots[-10:]), 1):
        snap_time = snap.get("time", "")[:19].replace("T", " ")
        short_id = snap.get("short_id", "")
        _console.print(f"  {i}. {short_id}  {snap_time}")

    # Select snapshot
    if snapshot:
        selected_id = snapshot
    else:
        choice = typer.prompt("\nRestore which snapshot?", default="1")
        try:
            idx = int(choice) - 1
            selected_snap = list(reversed(snapshots[-10:]))[idx]
            selected_id = selected_snap["short_id"]
        except (ValueError, IndexError):
            selected_id = choice  # Treat as raw snapshot ID

    # Confirm
    _console.print(f"\n[bold]Restore plan:[/bold]")
    _console.print(f"  Accessory:  {accessory}")
    _console.print(f"  Snapshot:   {selected_id}")
    _console.print(f"  Target:     {env}")
    _console.print()

    if not typer.confirm("Proceed?", default=False):
        con.warning("Restore cancelled.")
        raise typer.Exit(0)

    # Run restore via ansible-playbook
    extra_args = [
        "-e", f"accessory={accessory}",
        "-e", "confirm=yes",
        "-e", f"snapshot={selected_id}",
    ]
    ansible.run_playbook(
        "restore",
        env,
        bay_dir=bay_dir,
        extra_args=extra_args,
    )
    con.success(f"Restore complete for {accessory}")


@app.command()
def status(
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
) -> None:
    """Show the backup status dashboard (last backup, snapshots, repo size).

    Examples:

        bin/bay backup status
    """
    bay_dir = paths.find_bay_dir()

    con.header("Backup Status")

    # Get list of backup scripts to discover accessories
    stack = _get_stack_name(bay_dir)
    scripts_cmd = f'ls -1 /opt/{stack}/backup/backup-*.sh 2>/dev/null | sed "s|.*/backup-||;s|\\.sh||"'
    result = _run_on_host(env, scripts_cmd, bay_dir=bay_dir, message="Discovering accessories...")

    stdout = result.stdout
    # Parse ansible output — skip lines containing the ansible status prefix
    lines = [l.strip() for l in stdout.split("\n") if l.strip() and "|" not in l[:20]]
    if not lines:
        con.info("No backup accessories configured.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Accessory", width=15)
    table.add_column("Last Backup", width=22)
    table.add_column("Snaps", width=6, justify="right")
    table.add_column("Repo Size", width=10, justify="right")

    for acc in lines:
        acc = acc.strip()
        if not acc:
            continue
        # Get latest snapshot and stats
        info_cmd = (
            f'{_restic_env_source(stack)} && '
            f'export RESTIC_REPOSITORY="{_restic_repo(acc)}" && '
            f'echo "SNAPS:$(/usr/local/bin/restic snapshots --json --retry-lock 15m 2>/dev/null | jq length)" && '
            f'echo "LATEST:$(/usr/local/bin/restic snapshots --json --retry-lock 15m 2>/dev/null | jq -r "last.time // empty")" && '
            f'echo "SIZE:$(/usr/local/bin/restic stats --json --retry-lock 15m 2>/dev/null | jq -r ".total_size // 0")"'
        )
        try:
            info_result = _run_on_host(env, info_cmd, bay_dir=bay_dir, check=False)
            info_out = info_result.stdout or ""

            snaps = "0"
            latest = "\u2014"
            size = "\u2014"

            for line in info_out.split("\n"):
                if "SNAPS:" in line:
                    val = line.split("SNAPS:")[-1].strip()
                    if val.isdigit():
                        snaps = val
                elif "LATEST:" in line:
                    val = line.split("LATEST:")[-1].strip()
                    if val and val != "null":
                        latest = val[:19].replace("T", " ")
                elif "SIZE:" in line:
                    val = line.split("SIZE:")[-1].strip()
                    if val.isdigit():
                        size_bytes = int(val)
                        if size_bytes > 1_073_741_824:
                            size = f"{size_bytes / 1_073_741_824:.1f} GB"
                        elif size_bytes > 1_048_576:
                            size = f"{size_bytes / 1_048_576:.0f} MB"
                        elif size_bytes > 1024:
                            size = f"{size_bytes / 1024:.0f} KB"
                        else:
                            size = f"{size_bytes} B"

            table.add_row(acc, latest, snaps, size)
        except Exception:
            table.add_row(acc, "[red]error[/red]", "\u2014", "\u2014")

    _console.print(table)

    # Show next scheduled timer
    # Both spellings: a host that has not yet run the rename_migration role
    # still has the pre-1.0 backup timers.
    timer_cmd = (
        "systemctl list-timers 'bay-backup@*' 'argo-backup@*' "  # legacy-argo: dual-read, remove in a future major release
        "--no-pager --plain 2>/dev/null | head -5"
    )
    try:
        timer_result = _run_on_host(env, timer_cmd, bay_dir=bay_dir, check=False)
        timer_out = timer_result.stdout or ""
        timer_lines = [
            line
            for line in timer_out.split("\n")
            if "bay-backup" in line or "argo-backup" in line  # legacy-argo: dual-read, remove in a future major release
        ]
        if timer_lines:
            _console.print(f"\n[dim]Next timer: {timer_lines[0].strip()}[/dim]")
    except Exception:
        pass


@app.command()
def check(
    accessory: str = typer.Argument(..., help="Accessory name (e.g., postgres)."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    read_data: bool = typer.Option(False, "--read-data", help="Full data verification (slow, S3 egress)."),
) -> None:
    """Verify backup repository integrity (restic check).

    Examples:

        bin/bay backup check postgres
        bin/bay backup check postgres --read-data
    """
    bay_dir = paths.find_bay_dir()

    con.header(f"Checking {accessory} repository")

    stack = _get_stack_name(bay_dir)
    hosts = _hosts_with_target(env, accessory, bay_dir=bay_dir, stack=stack)
    if not hosts:
        con.info(f"No host has a '{accessory}' backup configured.")
        return

    read_flag = "--read-data" if read_data else ""
    cmd = (
        f'{_restic_env_source(stack)} && '
        f'export RESTIC_REPOSITORY="{_restic_repo(accessory)}" && '
        f'/usr/local/bin/restic check {read_flag} --retry-lock 15m'
    )

    try:
        _run_on_host(env, cmd, bay_dir=bay_dir, capture=False, limit=",".join(hosts))
        con.success(f"Repository check passed for {accessory}")
    except BayError:
        con.error(f"Repository check FAILED for {accessory}")
        raise typer.Exit(1)
