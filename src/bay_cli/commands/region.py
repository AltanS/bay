"""Region management commands: add."""

from __future__ import annotations

import re
from pathlib import Path

import typer
from rich.panel import Panel

from bay_cli import console, paths
from bay_cli.errors import BayError

app = typer.Typer(help="Manage deployment regions (multi-region inventories).")

_REGION_NAME_RE = re.compile(r"^[a-z][a-z0-9]*$")
_IP_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$"
)


def _parse_existing_regions(inventory_path: Path) -> dict[str, str]:
    """Parse existing regions from a multi-region inventory.

    Returns a dict of region_name -> first_host_ip.
    Uses two-pass parsing so [production:children] can appear in any position.
    """
    if not inventory_path.exists():
        return {}

    text = inventory_path.read_text()
    if "[production:children]" not in text:
        return {}

    # Pass 1: collect children group names
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

    # Pass 2: map each child group to its first host
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


def _validate_inventory_structure(inventory_path: Path) -> bool:
    """Check that the inventory file matches the expected wizard-generated structure."""
    if not inventory_path.exists():
        return False
    content = inventory_path.read_text()
    if "[production:children]" not in content:
        return False
    return True


def _get_domain_base(root: Path) -> str:
    """Read the base domain from group_vars."""
    import yaml

    # Check production/domains.yml first, then all/main.yml
    candidates = [
        root / "group_vars" / "production" / "domains.yml",
        root / "group_vars" / "all" / "main.yml",
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                data = yaml.safe_load(candidate.read_text()) or {}
                if "domain_base" in data:
                    return data["domain_base"]
            except Exception:
                pass
    raise BayError(
        "domain_base not found in group_vars. "
        "Set domain_base in group_vars/production/domains.yml or group_vars/all/main.yml"
    )


def _get_access_gateway(root: Path) -> str:
    """Read the access gateway type from group_vars."""
    import yaml

    gw_file = root / "group_vars" / "all" / "access_gateway.yml"
    if gw_file.exists():
        try:
            data = yaml.safe_load(gw_file.read_text()) or {}
            return data.get("access_gateway", "wireguard")
        except Exception:
            pass
    return "wireguard"


def _ensure_headscale_control_region(root: Path, existing_regions: dict[str, str]) -> None:
    """Ensure headscale_control_region is set in access_gateway.yml.

    If it's missing (pre-refactor setup), add it pointing to the first existing region.
    """
    import yaml

    gw_file = root / "group_vars" / "all" / "access_gateway.yml"
    if not gw_file.exists():
        return

    data = yaml.safe_load(gw_file.read_text()) or {}
    if data.get("access_gateway") != "headscale":
        return
    if "headscale_control_region" in data:
        return

    # First existing region is the control region (wizard convention)
    if not existing_regions:
        return
    control_region = next(iter(existing_regions))

    content = gw_file.read_text()
    # Insert after headscale_domain line
    lines = content.splitlines()
    insert_idx = None
    for i, line in enumerate(lines):
        if line.startswith("headscale_domain:"):
            insert_idx = i + 1
            break

    if insert_idx is not None:
        lines.insert(insert_idx, "")
        lines.insert(insert_idx + 1, "# Which region runs the Headscale coordination server.")
        lines.insert(insert_idx + 2, "# All other regions run only the Tailscale daemon and register via API.")
        lines.insert(insert_idx + 3, f"headscale_control_region: {control_region}")
        gw_file.write_text("\n".join(lines) + "\n")
        console.success(f"added headscale_control_region: {control_region} to access_gateway.yml")


def _update_inventory(inventory_path: Path, region_name: str, server_ip: str) -> None:
    """Add a new region group to the inventory file."""
    content = inventory_path.read_text()
    lines = content.splitlines()

    # Find [production:children] and add the region name there
    new_lines: list[str] = []
    children_section = False
    inserted_group = False

    for line in lines:
        new_lines.append(line)
        stripped = line.strip()

        if stripped == "[production:children]":
            children_section = True
            continue

        if children_section and not inserted_group:
            # We're in [production:children] -- check if we've passed all entries
            if not stripped or stripped.startswith("["):
                # Insert before this line (end of children section)
                new_lines.insert(-1, region_name)
                children_section = False
                inserted_group = True

    # If children section was the last thing in the file
    if children_section and not inserted_group:
        new_lines.append(region_name)

    # Add the new group block before [production:children]
    final_lines: list[str] = []
    for line in new_lines:
        if line.strip() == "[production:children]":
            # Insert the new group before [production:children]
            final_lines.append(f"[{region_name}]")
            final_lines.append(server_ip)
            final_lines.append("")
        final_lines.append(line)

    inventory_path.write_text("\n".join(final_lines) + "\n")


def _create_region_group_vars(
    root: Path,
    region_name: str,
    domain_base: str,
) -> Path:
    """Create group_vars/<region>/main.yml."""
    region_dir = root / "group_vars" / region_name
    region_dir.mkdir(parents=True, exist_ok=True)

    main_yml = region_dir / "main.yml"

    lines = [
        "---",
        f"# Region-specific overrides for {region_name}",
        "# These override values from all/ and production/",
        "",
        f"region: {region_name}",
        f"domain_base: {region_name}.{domain_base}",
    ]

    lines.append("")  # trailing newline
    main_yml.write_text("\n".join(lines))
    return main_yml


@app.command()
def add(
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
) -> None:
    """Add a new region to an existing multi-region deployment (interactive).

    Prompts for the region name and server IP, creates
    group_vars/<region>/main.yml, and updates the inventory. Then follow
    the printed next steps: DNS records, the headscale API key in the
    vault (headscale gateways), provision, deploy.

    Examples:

        bin/bay region add
    """
    from rich.prompt import Prompt

    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    console.show_banner(subtitle="Add Region")

    inventory_path = root / "hosts" / env

    # Validate inventory structure
    if not _validate_inventory_structure(inventory_path):
        console.error(
            f"hosts/{env} is not a multi-region inventory (no [production:children] group). "
            "Use 'bin/bay setup --multi-region' to set up multi-region first."
        )
        raise typer.Exit(1)

    existing = _parse_existing_regions(inventory_path)
    domain_base = _get_domain_base(root)
    access_gateway = _get_access_gateway(root)

    # Prompt for region name
    while True:
        region_name = Prompt.ask("  Region name (e.g., eu, ap, us-west)").strip().lower()
        if not region_name:
            console.error("Region name cannot be empty")
            continue
        if not _REGION_NAME_RE.match(region_name):
            console.error(
                f"Invalid region name '{region_name}' -- use only lowercase letters and numbers, "
                "starting with a letter"
            )
            continue
        if region_name in existing:
            console.error(f"Region '{region_name}' already exists in inventory")
            continue
        break

    # Prompt for server IP
    existing_ips = set(existing.values())
    while True:
        server_ip = Prompt.ask("  Server IP address").strip()
        if not server_ip:
            console.error("Server IP cannot be empty")
            continue
        if not _IP_RE.match(server_ip):
            console.error(f"Invalid IPv4 address: {server_ip}")
            continue
        if server_ip in existing_ips:
            console.error(f"IP {server_ip} is already used by another region")
            continue
        break

    # Ensure headscale_control_region is set (for pre-refactor setups)
    if access_gateway == "headscale":
        _ensure_headscale_control_region(root, existing)

    # Create group_vars
    group_vars_path = _create_region_group_vars(root, region_name, domain_base)
    console.success(f"created {group_vars_path.relative_to(root)}")

    # Update inventory
    _update_inventory(inventory_path, region_name, server_ip)
    console.success(f"updated hosts/{env}")

    # Next-steps panel
    steps = [
        f"[bold]Region '{region_name}' added successfully.[/bold]",
        "",
        "Next steps:",
        f"  1. Create DNS records:",
        f"     [dim]*.{region_name}.{domain_base} -> {server_ip}[/dim]",
    ]

    step_num = 2
    if access_gateway == "headscale":
        steps.append(f"  {step_num}. Ensure headscale_api_key is in vault secrets:")
        steps.append(f"     [dim]bin/bay vault edit {env}[/dim]")
        step_num += 1

    steps.append(f"  {step_num}. Provision the new server:")
    steps.append(f"     [dim]bin/bay provision {region_name}[/dim]")
    step_num += 1

    steps.append(f"  {step_num}. Deploy services:")
    steps.append(f"     [dim]bin/bay deploy {region_name}[/dim]")

    console.console.print()
    console.console.print(Panel("\n".join(steps), expand=False))
    console.console.print()
