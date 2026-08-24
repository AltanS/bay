"""Server management commands: list, add, remove, inspect."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import typer
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from ruamel.yaml import YAML

from bay_cli import ansible, console, paths, runner
from bay_cli.errors import BayError
from bay_cli.inventory import InventoryConfig

app = typer.Typer(help="Manage inventory servers.")


def _get_inventory(env: str) -> tuple[InventoryConfig, Path]:
    from bay_cli.paths import consumer_root, find_bay_dir

    try:
        root = consumer_root(find_bay_dir())
    except BayError:
        root = Path.cwd()

    inv_path = root / "hosts" / env
    if not inv_path.is_file():
        raise BayError.config(
            f"Inventory file not found: {inv_path}",
            hint=f"Create {inv_path} or run 'bin/bay setup'",
        )

    inv = InventoryConfig()
    inv.load(inv_path)
    return inv, root


def _check_ssh(host: str) -> bool:
    """Test SSH reachability with a short timeout."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no", host, "true"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


@app.command("list")
def list_servers(
    env: str = typer.Argument("production", help="Environment / inventory name"),
    check: bool = typer.Option(False, "--check", help="Test SSH reachability"),
) -> None:
    """List servers from the inventory.

    Examples:

        bin/bay server list
        bin/bay server list production --check
    """
    inv, root = _get_inventory(env)
    hosts = inv.list_hosts()
    children = inv.get_children_groups()

    # Determine if multi-region
    is_multi = bool(children)

    # Filter out children-section entries (those are group names, not hosts).
    # The parser tags every line inside `[production:children]` with the group
    # `production:children`, so that suffix is the only thing to exclude.
    # Excluding the *child group names* as well (eu/na/infra) also dropped the
    # real hosts, which live in exactly those groups — a multi-region inventory
    # rendered an empty table (GH bay#29).
    real_hosts = [h for h in hosts if not h["group"].endswith(":children")]

    # SSH check if requested
    reachability: dict[str, bool] = {}
    if check:
        for host in real_hosts:
            ip = host["ip"]
            reachability[ip] = _check_ssh(ip)

    if console.is_json_mode():
        entries = []
        for host in real_hosts:
            entry = {
                "name": host["name"],
                "ip": host["ip"],
                "group": host["group"],
            }
            if is_multi:
                entry["region"] = host["group"]
            if check:
                entry["reachable"] = reachability.get(host["ip"], False)
            entries.append(entry)
        console.emit_result({"servers": entries}, command="server.list")
        return

    table = Table(title="Servers")
    table.add_column("Host", style="bold")
    table.add_column("IP")
    if is_multi:
        table.add_column("Region")
    if check:
        table.add_column("Status")

    for host in real_hosts:
        row = [host["name"], host["ip"]]
        if is_multi:
            row.append(host["group"])
        if check:
            reachable = reachability.get(host["ip"], False)
            status = "[green]reachable[/green]" if reachable else "[red]unreachable[/red]"
            row.append(status)
        table.add_row(*row)

    console.console.print()
    console.console.print(table)
    console.console.print()


@app.command()
def add(
    ip: str = typer.Argument(help="Server IP address"),
    region: str | None = typer.Option(None, "--region", "-r", help="Region/group name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show diff without writing"),
    env: str = typer.Option("production", "--env", "-e", help="Environment / inventory name"),
) -> None:
    """Add a server to the inventory.

    Single-server inventories replace the existing host (with
    confirmation). Multi-region inventories require --region; a missing
    region's group_vars stub is created automatically. Provision the new
    host afterwards: `bin/bay provision <env>`.

    Examples:

        bin/bay server add 203.0.113.10
        bin/bay server add 198.51.100.20 --region na
        bin/bay server add 198.51.100.20 --region na --dry-run
    """
    from rich.prompt import Confirm, Prompt

    inv, root = _get_inventory(env)
    hosts = inv.list_hosts()
    children = inv.get_children_groups()
    is_multi = bool(children)

    # ── Determine target group ───────────────────────────────────
    if is_multi:
        # Multi-region: require --region
        if not region:
            if console.is_json_mode() or console.is_yes_mode():
                raise BayError.config(
                    "--region is required in multi-region mode",
                    hint="Specify the target region with --region",
                )
            region = Prompt.ask("  Region for this server")
    else:
        if not region:
            region = env  # single-server: use the env name as group (e.g., "production")

    # ── Idempotency: check if (ip, region) already exists ────────
    for host in hosts:
        if host["ip"] == ip and host["group"] == region:
            console.success(f"Server {ip} already in '{region}'")
            if console.is_json_mode():
                console.emit_result(
                    {"added": False, "ip": ip, "region": region, "created_group_vars": False},
                    command="server.add",
                )
            return

    # ── Conflict: IP exists in a different region ────────────────
    for host in hosts:
        if host["ip"] == ip and host["group"] != region:
            raise BayError.conflict("ip", ip, host["group"])

    # ── Single-server replacement warning ────────────────────────
    # Filter out children-section entries for real host count
    children_groups = set()
    for parent, child_list in children.items():
        children_groups.update(child_list)
        children_groups.add(f"{parent}:children")
    real_hosts = [h for h in hosts if h["group"] not in children_groups]

    if not is_multi and len(real_hosts) == 1 and real_hosts[0]["ip"] != ip:
        existing_ip = real_hosts[0]["ip"]
        if not console.is_yes_mode() and not console.is_json_mode():
            console.warning(f"This will replace {existing_ip} with {ip}")
            if not Confirm.ask("  Continue?", default=False):
                raise typer.Exit(0)
        # Remove the old host
        inv.remove_host(real_hosts[0]["name"])

    # ── Add to inventory ─────────────────────────────────────────
    inv.add_host(ip, ip, region)

    # ── Auto-create region group_vars ────────────────────────────
    created_group_vars = False
    if region and region != env:
        region_dir = root / "group_vars" / region
        if not region_dir.is_dir():
            region_dir.mkdir(parents=True, exist_ok=True)
            # Read existing domain_base for stub
            from bay_cli.config import StackConfig
            cfg = StackConfig(root)
            domain_base = cfg.get_domain_base(env)
            region_domain = f"{region}.{domain_base}" if domain_base else f"{region}.example.com"
            stub = (
                f"---\n"
                f"# Region-specific config for {region}\n"
                f"domain_base: {region_domain}\n"
            )
            (region_dir / "main.yml").write_text(stub)
            created_group_vars = True
            console.info(f"Created group_vars/{region}/main.yml")

    # ── Dry-run or save ──────────────────────────────────────────
    if dry_run:
        diff_text = inv.diff()
        if not console.is_json_mode():
            if diff_text:
                console.header("Dry-run diff")
                console.console.print(diff_text)
            else:
                console.info("No changes")
    else:
        if inv.has_changes():
            inv.save()

    # ── Output ───────────────────────────────────────────────────
    if console.is_json_mode():
        console.emit_result(
            {
                "added": True,
                "ip": ip,
                "region": region,
                "created_group_vars": created_group_vars,
                "dry_run": dry_run,
            },
            command="server.add",
        )
        return

    if not dry_run:
        console.success(f"Added {ip} to '{region}'")


@app.command()
def remove(
    ip: str = typer.Argument(help="Server IP address to remove"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show diff without writing"),
    env: str = typer.Option("production", "--env", "-e", help="Environment / inventory name"),
) -> None:
    """Remove a server from the inventory.

    Does NOT deprovision the machine — services keep running on it until
    you shut it down or wipe it yourself.

    Examples:

        bin/bay server remove 198.51.100.20
        bin/bay server remove 198.51.100.20 --dry-run
    """
    from rich.prompt import Confirm

    inv, root = _get_inventory(env)
    hosts = inv.list_hosts()

    # ── Idempotency: find host(s) matching this IP ───────────────
    matching = [h for h in hosts if h["ip"] == ip]
    if not matching:
        console.success(f"Server {ip} not in inventory, nothing to remove")
        if console.is_json_mode():
            console.emit_result(
                {"removed": False, "ip": ip, "warnings": [], "dry_run": dry_run},
                command="server.remove",
            )
        return

    # ── Warning ──────────────────────────────────────────────────
    warnings = [
        "Removing a server from inventory does NOT deprovision it. "
        "Services will keep running. Manually shut down or wipe the server if needed.",
    ]

    # ── Confirmation ─────────────────────────────────────────────
    if not console.is_yes_mode() and not console.is_json_mode() and not dry_run:
        for w in warnings:
            console.warning(w)
        if not Confirm.ask(f"Remove server {ip}?", default=False):
            return

    # ── Remove host(s) by name ───────────────────────────────────
    for host in matching:
        inv.remove_host(host["name"])

    # ── Dry-run ──────────────────────────────────────────────────
    if dry_run:
        diff_text = inv.diff()
        if console.is_json_mode():
            console.emit_result(
                {"removed": False, "ip": ip, "warnings": warnings, "dry_run": True},
                command="server.remove",
            )
        else:
            for w in warnings:
                console.warning(w)
            if diff_text:
                console.header("Dry-run diff")
                console.console.print(diff_text)
            else:
                console.info("No changes")
        return

    # ── Save ─────────────────────────────────────────────────────
    inv.save()

    # ── Output ───────────────────────────────────────────────────
    if console.is_json_mode():
        console.emit_result(
            {"removed": True, "ip": ip, "warnings": warnings, "dry_run": False},
            command="server.remove",
        )
        return

    console.success(f"Removed {ip} from inventory")
    for w in warnings:
        console.warning(w)


# ── Inspect ──────────────────────────────────────────────────────────────


_ANSI_RE_PATTERN = r"\x1b\[[0-9;]*m"


def _extract_ansible_output(stdout: str) -> str:
    """Extract command output from ansible ad-hoc response."""
    import re

    clean = re.sub(_ANSI_RE_PATTERN, "", stdout)
    marker = ">>"
    idx = clean.find(marker)
    if idx == -1:
        return clean
    return clean[idx + len(marker) :].strip()


def _ssh_collect(
    env: str, cmd: str, *, bay_dir: Path, message: str = "", limit: str | None = None
) -> str:
    """Run a command on the target host via ansible ad-hoc (no sudo)."""
    uv_cmd = ansible._uv_run_cmd(bay_dir)
    full_cmd = [
        *uv_cmd,
        "ansible",
        env,
        "-m",
        "ansible.builtin.command",
        "-a",
        cmd,
    ]
    if limit:
        full_cmd.extend(["--limit", limit])
    result = runner.run(
        full_cmd,
        capture=True,
        message=message,
        env=ansible._collections_env(bay_dir),
    )
    return _extract_ansible_output(result.stdout or "")


def _detect_network(
    env: str, interface: str, *, bay_dir: Path, limit: str | None = None
) -> dict[str, Any]:
    """SSH to a host and detect network configuration.

    Returns a dict with keys: mac, address, prefix_length, gateway, dns_servers.
    """
    # Collect link info (MAC)
    link_raw = _ssh_collect(
        env,
        f"ip -j link show {interface}",
        bay_dir=bay_dir,
        message=f"Detecting interface {interface}...",
        limit=limit,
    )
    link_data = json.loads(link_raw)
    if not link_data:
        raise BayError(
            f"Interface '{interface}' not found on host",
            hint=f"Try --interface with a different name. Run 'ip link' on the host to list interfaces.",
        )
    mac = link_data[0].get("address", "")

    # Collect address info (IP + prefix)
    addr_raw = _ssh_collect(
        env,
        f"ip -j addr show {interface}",
        bay_dir=bay_dir,
        message="Detecting IP address...",
        limit=limit,
    )
    addr_data = json.loads(addr_raw)
    address = ""
    prefix_length = 32
    for iface in addr_data:
        for info in iface.get("addr_info", []):
            if info.get("family") == "inet":
                address = info.get("local", "")
                prefix_length = info.get("prefixlen", 32)
                break
        if address:
            break

    # Collect default route (gateway)
    route_raw = _ssh_collect(
        env,
        "ip -j route show default",
        bay_dir=bay_dir,
        message="Detecting gateway...",
        limit=limit,
    )
    route_data = json.loads(route_raw)
    gateway = route_data[0].get("gateway", "") if route_data else ""

    # Collect DNS servers
    dns_raw = _ssh_collect(
        env,
        "cat /etc/resolv.conf",
        bay_dir=bay_dir,
        message="Detecting DNS servers...",
        limit=limit,
    )
    dns_servers: list[str] = []
    for line in dns_raw.splitlines():
        line = line.strip()
        if line.startswith("nameserver "):
            dns_servers.append(line.split()[1])

    return {
        "mac": mac,
        "address": address,
        "prefix_length": prefix_length,
        "gateway": gateway,
        "dns_servers": dns_servers[:2],
    }


def _load_network_yml(root: Path, group: str) -> dict[str, Any] | None:
    """Load existing network.yml for a group, or None if not found."""
    yaml = YAML()
    for candidate in [
        root / "group_vars" / group / "network.yml",
        root / "group_vars" / "all" / "network.yml",
    ]:
        if candidate.is_file():
            with candidate.open() as fh:
                data = yaml.load(fh)
            if isinstance(data, dict):
                return data
    return None


def _format_yaml_block(
    host_name: str, ip: str, detected: dict[str, Any], paste_path: str
) -> str:
    """Format detected network config as a ready-to-paste YAML block."""
    dns_lines = "\n".join(f'    - "{s}"' for s in detected["dns_servers"])
    return (
        f"---\n"
        f"# Network configuration — per-host static IP (replaces DHCP).\n"
        f"# Detected from {host_name} ({ip})\n"
        f"\n"
        f"netplan_enabled: true\n"
        f'netplan_address: "{detected["address"]}"\n'
        f'netplan_mac: "{detected["mac"]}"\n'
        f'netplan_gateway: "{detected["gateway"]}"\n'
        f"netplan_prefix_length: {detected['prefix_length']}\n"
        f"netplan_dns_servers:\n"
        f"{dns_lines}\n"
    )


def _check_drift(
    detected: dict[str, Any], existing: dict[str, Any]
) -> list[tuple[str, str, str]]:
    """Compare detected values against existing network.yml.

    Returns list of (field, existing_value, detected_value) for mismatches.
    """
    drifts: list[tuple[str, str, str]] = []
    field_map = {
        "netplan_mac": "mac",
        "netplan_address": "address",
        "netplan_gateway": "gateway",
        "netplan_prefix_length": "prefix_length",
    }
    for yml_key, det_key in field_map.items():
        existing_val = existing.get(yml_key)
        detected_val = detected[det_key]
        if existing_val is not None and str(existing_val) != str(detected_val):
            drifts.append((yml_key, str(existing_val), str(detected_val)))
    return drifts


@app.command()
def inspect(
    env: str = typer.Argument("production", help="Environment / inventory name"),
    interface: str = typer.Option("eth0", "--interface", "-i", help="Network interface"),
    region: str | None = typer.Option(None, "--region", "-r", help="Restrict to a specific region"),
) -> None:
    """Inspect live network configuration from servers via SSH.

    Detects MAC address, IP, gateway, prefix length, and DNS servers.
    Compares against existing network.yml, flags drift, and prints a
    ready-to-paste YAML block for new hosts (then run
    `bin/bay provision <env> --tags netplan`).

    Examples:

        bin/bay server inspect
        bin/bay server inspect production --region eu
        bin/bay server inspect production --interface ens3
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)
    inv, _ = _get_inventory(env)

    hosts = inv.list_hosts()
    children = inv.get_children_groups()

    # Filter out children-section entries
    children_groups: set[str] = set()
    for parent, child_list in children.items():
        children_groups.update(child_list)
        children_groups.add(f"{parent}:children")
    real_hosts = [h for h in hosts if h["group"] not in children_groups]

    # Filter by region if specified
    if region:
        real_hosts = [h for h in real_hosts if h["group"] == region]
        if not real_hosts:
            raise BayError(
                f"No hosts found in region '{region}'",
                hint=f"Available regions: {', '.join(sorted({h['group'] for h in hosts if h['group'] not in children_groups}))}",
            )

    is_multi_host = len(real_hosts) > 1
    results: list[dict[str, Any]] = []

    for host in real_hosts:
        host_name = host["name"]
        host_ip = host["ip"]
        host_group = host["group"]

        console.header(f"Inspecting {host_name} ({host_ip})")

        # Use --limit to target specific host
        limit = host_ip if len(real_hosts) > 1 or region else None

        detected = _detect_network(
            env, interface, bay_dir=bay_dir, limit=limit
        )

        # Determine paste path
        if is_multi_host:
            paste_path = f"host_vars/{host_name}/network.yml"
        else:
            paste_path = f"group_vars/{host_group}/network.yml"

        # Check for drift against existing config
        existing = _load_network_yml(root, host_group)
        drifts = _check_drift(detected, existing) if existing else []

        yaml_block = _format_yaml_block(host_name, host_ip, detected, paste_path)

        results.append({
            "host": host_name,
            "ip": host_ip,
            "group": host_group,
            "detected": detected,
            "paste_path": paste_path,
            "drifts": drifts,
            "has_existing": existing is not None,
        })

        # ── Rich output ────────────────────────────────────────────
        if existing and not drifts:
            console.success(f"network.yml is up to date — no drift detected")
        elif drifts:
            drift_table = Table(title="Drift Detected", border_style="yellow")
            drift_table.add_column("Field", style="bold")
            drift_table.add_column("network.yml", style="red")
            drift_table.add_column("Live server", style="green")
            for field, old, new in drifts:
                drift_table.add_row(field, old, new)
            console.console.print()
            console.console.print(drift_table)
            console.warning(f"Update {paste_path} with the live values above")

        if not existing:
            console.console.print()
            console.console.print(
                Panel(
                    Syntax(yaml_block, "yaml", theme="monokai"),
                    title=f"[bold]Paste into: {paste_path}[/bold]",
                    border_style="green",
                    expand=False,
                )
            )
            console.info(f"Then run: bay provision {env} --tags netplan")
        elif drifts:
            console.console.print()
            console.console.print(
                Panel(
                    Syntax(yaml_block, "yaml", theme="monokai"),
                    title=f"[bold]Updated config for: {paste_path}[/bold]",
                    border_style="yellow",
                    expand=False,
                )
            )

        # Topology hint
        if detected["prefix_length"] == 32:
            console.info(
                "Hetzner /32 topology detected — scope:link route will be added automatically"
            )

    # ── Multi-host guidance ─────────────────────────────────────────
    if is_multi_host:
        console.console.print()
        console.warning(
            "Multiple hosts detected — use host_vars/<hostname>/network.yml "
            "for per-host values (MACs and IPs differ per machine)"
        )

    # ── JSON output ─────────────────────────────────────────────────
    if console.is_json_mode():
        console.emit_result(
            {"hosts": results},
            command="server.inspect",
        )
