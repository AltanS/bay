"""Backup management commands: list, run, restore, status, check."""

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

import yaml as _yaml

from bay_cli import ansible, console as con, runner
from bay_cli.context import Context, context_from
from bay_cli.errors import BayError

app = typer.Typer(help="Manage restic backups (list, run, restore, status, check).")

_console = Console()


def _get_stack_name(cx: Context) -> str:
    """Read stack_name from consumer group_vars, default to 'bay'."""
    main_yml = cx.main_vars_file
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


def _configured_targets(cx: Context, stack: str) -> set[str]:
    """The backup target names the fleet's compiled services.yml configures.

    Accessories with a ``backup`` block, and ``<stack>_<volume>`` for every
    ``volume_backups`` entry. Which box runs each one is the resolver's job
    (``bay backup status``); this only says whether the name is configured.
    """
    try:
        data = _yaml.safe_load(cx.services_file.read_text()) or {}
    except (OSError, _yaml.YAMLError):
        return set()
    names = {
        name for name, acc in (data.get("accessories") or {}).items()
        if isinstance(acc, dict) and acc.get("backup")
    }
    names |= {f"{stack}_{vol}" for vol in (data.get("volume_backups") or {})}
    return names


@app.command("list")
def list_snapshots(
    ctx: typer.Context,
    accessory: str = typer.Argument(..., help="Accessory name (e.g., postgres)."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
) -> None:
    """List backup snapshots for an accessory (newest first).

    In multi-region setups, snapshots from every host that backs up the
    accessory are merged into one list.

    Examples:

        bay backup list postgres
        bay backup list headscale
    """
    cx = context_from(ctx)
    bay_dir = cx.framework_root

    con.header(f"Snapshots for {accessory}")

    stack = _get_stack_name(cx)
    hosts = _hosts_with_target(env, accessory, bay_dir=bay_dir, stack=stack)
    if not hosts:
        if accessory in _configured_targets(cx, stack):
            # Configured but on no box: nothing backs it up. Before 2.24.0
            # this printed an info line and exited 0.
            con.error(
                f"'{accessory}' is configured, but no host has its backup installed: "
                "nothing backs it up. Install it with 'bay up' or "
                f"'bay deploy {env} --tags backup', then check 'bay backup status'."
            )
            raise typer.Exit(1)
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
    ctx: typer.Context,
    accessory: Optional[str] = typer.Argument(None, help="Accessory name. If omitted, runs all."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
) -> None:
    """Trigger a backup now (one accessory, or all).

    Examples:

        bay backup run postgres
        bay backup run
    """
    cx = context_from(ctx)
    bay_dir = cx.framework_root

    stack = _get_stack_name(cx)
    if accessory:
        con.header(f"Running backup for {accessory}")
        hosts = _hosts_with_target(env, accessory, bay_dir=bay_dir, stack=stack)
        if not hosts:
            con.info(f"No host has a '{accessory}' backup configured.")
            return
        _run_on_host(env, _start_unit_cmd(accessory), bay_dir=bay_dir, capture=False,
                     limit=",".join(hosts))
        con.success(f"Backup complete for {accessory}")
    else:
        con.header("Running all backups")
        # nullglob via the glob check keeps hosts with zero backup-*.sh from
        # hard-failing the whole group (mirrors status's `2>/dev/null` tolerance).
        cmd = (
            'rc=0; for script in /opt/{stack}/backup/backup-*.sh; do '
            '[ -e "$script" ] || continue; '
            't=${{script##*/backup-}}; t=${{t%.sh}}; '
            'echo "==> Running $t"; '
            '{one} || rc=1; '
            'done; exit $rc'
        ).format(stack=stack, one=_start_unit_cmd('"$t"', quoted=True))
        _run_on_host(env, cmd, bay_dir=bay_dir, capture=False)
        con.success("All backups complete")


def _start_unit_cmd(target: str, *, quoted: bool = False) -> str:
    """Shell that runs one backup the way its timer does, and prints its log.

    ``systemctl start`` runs ``bay-backup@<target>.service``: as the app user,
    with the alert credentials, and waits for the oneshot to end. Before 2.24.0
    ``bay backup run`` ran the script over ``ansible --become``, so as root: a
    first run made a root-owned restic cache, and every timer run after it
    could not open the cache and read the whole repository index from S3
    again. ``quoted`` means ``target`` is already shell text (``"$t"``).
    """
    unit = f'"bay-backup@"{target}".service"' if quoted else shlex.quote(f'bay-backup@{target}.service')
    return (
        f'( systemctl start {unit}; r=$?; '
        f'journalctl --no-pager -o cat _SYSTEMD_INVOCATION_ID="$(systemctl show -p InvocationID --value {unit})"; '
        f'exit $r )'
    )


def _restore_host(target: str, wanted: str | None, hosts: list[str]) -> str:
    """The one host a restore of ``target`` runs on.

    ``hosts`` are the hosts that have ``backup-<target>.sh``. Before 2.23.2 the
    restore listed snapshots and ran restore.yml on every host of the env: a
    host without the target failed "Verify snapshot exists" and sent the
    restore.failed alert, and a target on several hosts was restored from one
    host's snapshot id on all of them.
    """
    if not hosts:
        raise BayError(
            f"No host has a '{target}' backup configured, so there is nothing to restore. "
            "Check the name with 'bay backup status'."
        )
    if wanted is not None:
        if wanted not in hosts:
            raise BayError(
                f"Host {wanted} has no '{target}' backup. Hosts that have it: {', '.join(hosts)}"
            )
        return wanted
    if len(hosts) > 1:
        raise BayError(
            f"More than one host backs up '{target}': {', '.join(hosts)}. "
            "Each host has its own snapshots. Pick one with --host <host>."
        )
    return hosts[0]


def _target_config(cx: Context, target: str, stack: str) -> dict[str, Any]:
    """What the compiled services.yml says about a restore target.

    ``method``: the backup method, from the block or the image, as the backup
    role decides. ``databases``: the databases the backup script dumps (the
    block's list, else POSTGRES_DB, then the databases of bound services).
    ``clients``: the containers a live restore stops, each as
    ``{"name", "database"}``. A project database has ``backup.clients`` (the
    other containers of the project, from the compiler): clients of every
    database of it (``""``). A shared resource has every service bound to it
    by a ``database:`` block, with the database it is bound to. The play
    stops only the clients of the restored database that run on the host.
    """
    empty: dict[str, Any] = {"method": "", "databases": [], "clients": []}
    try:
        data = _yaml.safe_load(cx.services_file.read_text()) or {}
    except (OSError, _yaml.YAMLError):
        return empty
    if target.startswith(f"{stack}_") and target[len(stack) + 1:] in (data.get("volume_backups") or {}):
        return {"method": "file", "databases": [], "clients": []}
    acc = (data.get("accessories") or {}).get(target)
    if not isinstance(acc, dict):
        return empty
    block = acc.get("backup") if isinstance(acc.get("backup"), dict) else {}
    image = str(acc.get("image", ""))
    method = str(block.get("method") or (
        "pg_dump" if "postgres" in image
        else "mysql" if ("mysql" in image or "mariadb" in image)
        else "redis" if "redis" in image else "file"
    ))
    clear = ((acc.get("env") or {}).get("clear") or {}) if isinstance(acc.get("env"), dict) else {}
    databases = [str(d) for d in (block.get("databases") or [clear.get("POSTGRES_DB", "app")])]
    clients = [{"name": str(c), "database": ""} for c in (block.get("clients") or [])]
    for name, svc in (data.get("services") or {}).items():
        db = svc.get("database") if isinstance(svc, dict) else None
        if isinstance(db, dict) and db.get("accessory") == target:
            bound = str(db.get("name") or name)
            if bound not in databases:
                databases.append(bound)
            if all(c["name"] != name for c in clients):
                clients.append({"name": str(name), "database": bound})
    if method != "pg_dump":
        databases = []
    return {"method": method, "databases": databases, "clients": clients}


def _pick_snapshot(env: str, target: str, target_host: str, *, bay_dir: Path, stack: str,
                   snapshot: str | None) -> str:
    """The snapshot id to restore: --snapshot, or a choice from the newest 10."""
    if snapshot:
        return snapshot
    list_cmd = (
        f'{_restic_env_source(stack)} && '
        f'export RESTIC_REPOSITORY="{_restic_repo(target)}" && '
        f'/usr/local/bin/restic snapshots --json --retry-lock 15m'
    )
    result = _run_on_host(
        env, list_cmd, bay_dir=bay_dir, message="Fetching snapshots...", limit=target_host
    )
    json_str = _extract_json_from_ansible(result.stdout)
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
    newest = list(reversed(snapshots[-10:]))
    _console.print("\nAvailable snapshots:")
    for i, snap in enumerate(newest, 1):
        snap_time = snap.get("time", "")[:19].replace("T", " ")
        paths = ", ".join(snap.get("paths") or [])
        _console.print(f"  {i}. {snap.get('short_id', '')}  {snap_time}  {paths}")
    choice = typer.prompt("\nRestore which snapshot?", default="1")
    try:
        return str(newest[int(choice) - 1]["short_id"])
    except (ValueError, IndexError):
        return choice  # Treat as raw snapshot ID


@app.command()
def restore(
    ctx: typer.Context,
    env: str = typer.Argument(..., help="Target environment (e.g., production)."),
    accessory: str = typer.Argument(..., help="Accessory name (e.g., postgres)."),
    snapshot: Optional[str] = typer.Option(None, "--snapshot", "-s", help="Snapshot ID (omit to pick interactively from the latest 10)."),
    host: Optional[str] = typer.Option(
        None,
        "--host",
        help="The host to restore on (an inventory name). Needed when more than one host backs up the target.",
    ),
    database: Optional[str] = typer.Option(
        None, "--database", help="pg_dump: the database to restore, when the target dumps several."
    ),
    into: Optional[str] = typer.Option(
        None,
        "--into",
        help="pg_dump: scratch (a throwaway container, the default) or live (the running database).",
    ),
    yes_stop_clients: bool = typer.Option(
        False,
        "--yes-stop-clients",
        help="With --into live: stop the containers that use the database during the restore.",
    ),
    terminate_connections: bool = typer.Option(
        False,
        "--terminate-connections",
        help="With --into live: end the other sessions of the database that are still open "
             "after the clients stopped (else the restore stops and names them).",
    ),
) -> None:
    """Restore an accessory from a backup snapshot (interactive).

    Runs only on the host that backs up the target. Each host has its own
    restic repo, so a snapshot belongs to one host: when several hosts back up
    the target (a shared resource on several boxes), name one with --host.

    A pg_dump target restores into a scratch container by default: a new
    Postgres with no network, the rows are counted, and the container is kept
    for you to read (removed by the next restore test after a day). The live
    database is never touched. --into live restores into the running
    database and always needs --yes-stop-clients: the clients of that
    database on the host are stopped first and started after (the command
    names them). A session still open after that stops the restore, unless
    --terminate-connections ends it. The restore goes into the database the
    snapshot holds; --database must match it.

    Other methods restore live, as before: a safety snapshot tagged
    `pre-restore` is taken first, and the result is checked (redis: PONG). A
    volume backup target (`<stack_name>_<volume>`) is extracted into the
    volume with its container stopped. The automatic headscale state backup
    cannot be restored by this command; see docs/backups.md.

    Examples:

        bay backup restore production postgres
        bay backup restore production shop-postgres --into live --yes-stop-clients
        bay backup restore production postgres --database blog --snapshot ab12cd34
    """
    cx = context_from(ctx)
    bay_dir = cx.framework_root
    stack = _get_stack_name(cx)
    config = _target_config(cx, accessory, stack)
    is_pg = config["method"] == "pg_dump"
    mode = into or ("scratch" if is_pg else "live")
    if mode not in ("scratch", "live"):
        raise BayError(f"--into must be scratch or live, not {mode!r}")
    if mode == "scratch" and not is_pg:
        raise BayError(
            f"'{accessory}' is not a pg_dump target ({config['method'] or 'unknown'}); "
            "only a pg_dump target restores into a scratch container. Use --into live."
        )
    # The database the restore goes into, when the CLI can know it: --database,
    # or the one database the target dumps. Else the play decides from the
    # snapshot and stops only the clients of that database.
    known_db = database or (config["databases"][0] if len(config["databases"]) == 1 else None)
    clients: list[dict[str, str]] = []
    if is_pg and mode == "live":
        clients = [c for c in config["clients"]
                   if known_db is None or c["database"] in ("", known_db)]
    names = [c["name"] for c in clients]
    if is_pg and mode == "live" and not yes_stop_clients:
        what = (f"stops these containers first and starts them after: {', '.join(names)}"
                + ("" if known_db else " (only those bound to the database the snapshot holds)")
                + ". Their apps are down for the restore"
                if names else "replaces the live database while apps may use it")
        raise BayError(
            f"A live restore of '{accessory}' {what}. "
            "Re-run with --into live --yes-stop-clients, or restore into a scratch container."
        )

    con.header(f"Restore {accessory} on {env}")

    target_host = _restore_host(
        accessory, host, _hosts_with_target(env, accessory, bay_dir=bay_dir, stack=stack)
    )
    selected_id = _pick_snapshot(
        env, accessory, target_host, bay_dir=bay_dir, stack=stack, snapshot=snapshot
    )

    _console.print("\n[bold]Restore plan:[/bold]")
    _console.print(f"  Accessory:  {accessory}")
    _console.print(f"  Snapshot:   {selected_id}")
    if database:
        _console.print(f"  Database:   {database}")
    _console.print(f"  Target:     {env}")
    _console.print(f"  Host:       {target_host}")
    _console.print(f"  Into:       {mode}")
    if names:
        _console.print(f"  Stops:      {', '.join(names)} (those that run on {target_host}; "
                       "started again after)")
    _console.print()

    if is_pg and mode == "live":
        _console.print(
            "[yellow]A live restore puts back the objects of the dump only. Objects made after "
            "the snapshot stay (a table or a column of a later migration), while a migrations "
            "table goes back to the snapshot, so the next migration can fail. Check the schema "
            "after the restore, or restore into a scratch container first.[/yellow]"
        )
        _console.print()
    question = (
        f"Stop {', '.join(names)} and restore into the live database?"
        if names else "Restore into the live database?" if mode == "live" else "Proceed?"
    )
    if not typer.confirm(question, default=False):
        con.warning("Restore cancelled.")
        raise typer.Exit(0)

    variables: dict[str, Any] = {
        "accessory": accessory,
        "confirm": "yes",
        "snapshot": selected_id,
        "restore_into": mode,
        "restore_stop_clients": clients,
        "restore_terminate_connections": bool(terminate_connections and mode == "live"),
    }
    if database:
        variables["database"] = database
    if mode == "scratch":
        variables["restore_keep"] = True
    # fleet_root: the run reads the fleet's hosts and group_vars. Without it
    # (before 2.23.2) Ansible found no group_vars and stopped at the first task
    # (`'app_user' is undefined`), so no restore could run.
    ansible.run_playbook(
        "restore",
        env,
        bay_dir=bay_dir,
        fleet_root=cx.fleet_root,
        extra_args=["-e", json.dumps(variables), "-l", target_host],
    )
    con.success(f"Restore complete for {accessory}")


@app.command("restore-test")
def restore_test(
    ctx: typer.Context,
    env: str = typer.Argument(..., help="Target environment (e.g., production)."),
    accessory: str = typer.Argument(..., help="A pg_dump target (e.g., shop-postgres)."),
    snapshot: Optional[str] = typer.Option(None, "--snapshot", "-s", help="Snapshot ID (default: the newest)."),
    host: Optional[str] = typer.Option(None, "--host", help="The host, when several back up the target."),
    database: Optional[str] = typer.Option(None, "--database", help="The database, when the target dumps several."),
    as_json: bool = typer.Option(False, "--json", help="Print the report as one JSON document."),
) -> None:
    """Prove a pg_dump backup restores: restore it into a scratch container and count.

    On the box: a new Postgres with no network and no port, the snapshot
    restored into it, an exact row count per table, compared with the live
    planner estimates; then the container is removed, also on failure. No
    prompt and no pre-restore backup: the live database is never touched.
    Only counts leave the box. It reads the whole snapshot from S3 once.

    Exit 0 when the restore worked and holds tables, else 1.

    Examples:

        bay backup restore-test production shop-postgres
    """
    import tempfile

    cx = context_from(ctx)
    bay_dir = cx.framework_root
    stack = _get_stack_name(cx)
    as_json = as_json or con.is_json_mode()
    config = _target_config(cx, accessory, stack)
    if config["method"] != "pg_dump":
        raise BayError(
            f"'{accessory}' is not a pg_dump target ({config['method'] or 'not in services.yml'}); "
            "a restore test needs one."
        )
    if not as_json:
        con.header(f"Restore test of {accessory} on {env}")
    target_host = _restore_host(
        accessory, host, _hosts_with_target(env, accessory, bay_dir=bay_dir, stack=stack)
    )
    with tempfile.TemporaryDirectory(prefix="bay-restore-test-") as tmp:
        out = Path(tmp) / "report.json"
        variables: dict[str, Any] = {
            "accessory": accessory,
            "snapshot": snapshot or "latest",
            "restore_into": "scratch",
            "restore_keep": False,
            # A failure sends backup.restore_test_failed, not restore.failed.
            "restore_test": True,
            "restore_test_out": str(out),
        }
        if database:
            variables["database"] = database
        result = ansible.run_playbook(
            "restore",
            env,
            bay_dir=bay_dir,
            fleet_root=cx.fleet_root,
            extra_args=["-e", json.dumps(variables), "-l", target_host],
            capture=as_json,
            check=False,
        )
        report = json.loads(out.read_text()) if out.exists() else None
    ok = result.returncode == 0 and report is not None and int(report.get("tables", 0)) > 0
    if as_json:
        print(json.dumps({"ok": ok, "report": report}, indent=2))
        raise typer.Exit(0 if ok else 1)
    if report is not None:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Table")
        table.add_column("Rows (scratch)", justify="right")
        table.add_column("Rows (live estimate)", justify="right")
        live = report.get("live_estimates") or {}
        for name, rows in sorted((report.get("counts") or {}).items()):
            table.add_row(name, str(rows), str(live.get(name, "-")))
        _console.print(table)
        _console.print(
            f"Snapshot {report.get('snapshot')} ({report.get('snapshot_time', '')[:19]}), "
            f"{report.get('tables')} tables, {report.get('rows')} rows, {report.get('seconds')} s."
        )
        if report.get("empty_but_live_has_rows"):
            con.warning("Empty in the snapshot but not in the live estimates: "
                        + ", ".join(report["empty_but_live_has_rows"]))
    if not ok:
        con.error(f"Restore test FAILED for {accessory}")
        raise typer.Exit(1)
    con.success(f"Restore test passed for {accessory}")


_STATE_STYLE = {
    "ok": "green",
    "new": "cyan",
    "no_record": "yellow",
    "missing": "bold red",
    "stale": "bold red",
    "no_answer": "yellow",
    "extra": "dim",
    "disabled": "dim",
}


@app.command()
def status(
    ctx: typer.Context,
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Also ask restic for the snapshots of every target (one S3 list per target).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print one JSON document."),
) -> None:
    """Show which backup targets each box should have, and whether they run.

    The expected targets come from the fleet config, with the same rules as
    the backup role, so a target that was never installed shows as missing.
    Each box reports its scripts, timers and the time of the last good run.
    No S3 call unless --deep.

    Exit codes: 0 all fine; 1 a configured target has no script or no
    running timer; 2 a target had no good run within its max_age_hours;
    3 a box did not answer. When several apply, the lowest non-zero wins.

    Examples:

        bay backup status
        bay backup status --env production --deep --json
    """
    import tempfile

    from bay_cli import backup_status as bs

    cx = context_from(ctx)
    bay_dir = cx.framework_root
    as_json = as_json or con.is_json_mode()
    if not as_json:
        con.header("Backup Status")

    with tempfile.TemporaryDirectory(prefix="bay-backup-status-") as tmp:
        out = Path(tmp) / "status.json"
        result = ansible.run_playbook(
            "backup-status",
            env,
            bay_dir=bay_dir,
            fleet_root=cx.fleet_root,
            extra_args=[
                "-e", json.dumps({
                    "backup_status_out": str(out),
                    "backup_status_deep": deep,
                }),
            ],
            capture=True,
            check=False,
            # No spinner in JSON mode: stdout holds only the document.
            message="" if as_json else "Reading the backup state of every box...",
        )
        if not out.exists():
            # The play wrote nothing: it failed before the last task (a vault
            # or inventory problem), or no box answered. Show why.
            tail = "\n".join(
                _strip_ansi((result.stdout or "") + (result.stderr or "")).strip().splitlines()[-15:]
            )
            if not as_json:
                _console.print(tail, markup=False, highlight=False)
            doc = {"hosts": [f"{env} (no box answered)"], "results": {}}
        else:
            doc = json.loads(out.read_text())
    rows, code = bs.evaluate(doc)

    if as_json:
        print(json.dumps({
            "env": env,
            "deep": deep,
            "exit_code": code,
            "rows": [r.to_dict() for r in rows],
        }, indent=2))
        raise typer.Exit(code)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Host")
    table.add_column("Target")
    table.add_column("State")
    table.add_column("Last good run")
    table.add_column("Next run")
    if deep:
        table.add_column("Snapshots", justify="right")
        table.add_column("Latest snapshot")
    for r in rows:
        style = _STATE_STYLE.get(r.state, "")
        state = f"[{style}]{r.state}[/{style}]" if style else r.state
        if r.detail:
            state += f" ({r.detail})"
        cells = [r.host, r.target, state, bs.when(r.last_ok), r.next_run or "-"]
        if deep:
            cells += [
                "-" if r.snapshots is None else str(r.snapshots),
                r.deep_error or (r.latest_snapshot or "-")[:19].replace("T", " "),
            ]
        table.add_row(*cells)
    _console.print(table)
    if code == bs.MISSING:
        con.error("A configured target is not installed. Install it with 'bay up' or 'bay deploy <env> --tags backup'.")
    elif code == bs.STALE:
        con.error("A target had no good run within its max_age_hours. See journalctl -u bay-backup@<target> on the box.")
    elif code == bs.NO_ANSWER:
        con.warning("A box did not answer, so its targets are unknown.")
    raise typer.Exit(code)


@app.command()
def check(
    ctx: typer.Context,
    accessory: str = typer.Argument(..., help="Accessory name (e.g., postgres)."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    read_data: bool = typer.Option(False, "--read-data", help="Full data verification (slow, S3 egress)."),
) -> None:
    """Verify backup repository integrity (restic check).

    Examples:

        bay backup check postgres
        bay backup check postgres --read-data
    """
    cx = context_from(ctx)
    bay_dir = cx.framework_root

    con.header(f"Checking {accessory} repository")

    stack = _get_stack_name(cx)
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
