"""Interactive wizard prompt flow for project scaffolding."""

from __future__ import annotations

import ipaddress
import urllib.request
import urllib.error
from pathlib import Path
from typing import Callable

import questionary
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from bay_cli import console
from bay_cli.catalog import (
    CatalogEntry,
    _package_framework_root,
    load_catalog,
    resolve_dependencies,
)
from bay_cli.errors import BayError
from bay_cli.paths import find_bay_dir, consumer_root
from bay_cli.utils.secret_gen import generate_password
from bay_cli.wizard.models import (
    RegionConfig,
    SSHKey,
    WizardResult,
    discover_local_ssh_keys,
    parse_ssh_public_key,
    validate_domain,
    validate_ip,
    validate_project_name,
    validate_region_name,
)

# Type alias for the existing config passed in edit mode
_ExistingConfig = WizardResult | None

# ── Suggested region names for multi-region prompts ─────────────────────

_REGION_SUGGESTIONS = ["eu", "na", "ap"]

# ── Catalog loader (lazy, cached) ─────────────────────────────────────

_catalog_cache: dict[str, CatalogEntry] | None = None


def _get_catalog() -> dict[str, CatalogEntry]:
    """Load and cache the service/accessory catalog."""
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    try:
        bay_dir = find_bay_dir()
        _catalog_cache = load_catalog(bay_dir, consumer_root(bay_dir))
    except BayError:
        # Fallback: load from package location (framework dev / tests)
        fw_root = _package_framework_root()
        _catalog_cache = load_catalog(fw_root, Path.cwd())
    return _catalog_cache

# ── Questionary Helpers ────────────────────────────────────────────────


def _ask_or_interrupt(question: questionary.Question) -> object:
    """Call ``.ask()`` on a questionary Question, raising on non-TTY.

    When stdin is not a TTY, questionary returns ``None``.  This helper
    converts that into a ``KeyboardInterrupt`` so the calling code exits
    cleanly instead of proceeding with bad data.
    """
    result = question.ask()
    if result is None:
        raise KeyboardInterrupt
    return result


# ── Prompt Helpers ──────────────────────────────────────────────────────


def _prompt_validated(
    prompt_text: str,
    validator: Callable[[str], str],
    **kwargs: object,
) -> str:
    """Prompt the user repeatedly until the validator accepts the input.

    Passes **kwargs through to ``Prompt.ask()``. On validation failure,
    prints the error message and re-prompts.
    """
    while True:
        value = Prompt.ask(prompt_text, **kwargs)  # type: ignore[arg-type]
        try:
            return validator(value)
        except BayError as e:
            console.error(str(e))


def _prompt_choice(
    prompt_text: str,
    choices: list[tuple[str, str]],
    default: str | None = None,
) -> str:
    """Display an arrow-key selection list and return the selected key.

    *choices* is a list of ``(key, label)`` tuples.  Uses ``questionary.select``
    for TUI arrow-key navigation.  *default* is the key value to pre-select
    (or the first item if None).
    """
    default_key = default if default else choices[0][0]
    q_choices = [
        questionary.Choice(title=label, value=key)
        for key, label in choices
    ]
    result = _ask_or_interrupt(
        questionary.select(prompt_text, choices=q_choices, default=default_key)
    )
    return result  # type: ignore[return-value]


def _prompt_confirm(prompt_text: str, default: bool = False) -> bool:
    """Prompt for yes/no using arrow-key selection (Yes / No)."""
    choices = [
        questionary.Choice(title="Yes", value=True),
        questionary.Choice(title="No", value=False),
    ]
    result = _ask_or_interrupt(
        questionary.select(prompt_text, choices=choices, default=default)
    )
    return result  # type: ignore[return-value]


def _prompt_services(existing_selected: list[str] | None = None) -> list[str]:
    """Display a checkbox picker for service/accessory selection.

    Pre-checks items from *existing_selected* (edit mode).
    Returns the list of selected service IDs.
    """
    catalog = _get_catalog()
    existing = existing_selected or []
    q_choices = []
    services = [e for e in catalog.values() if e.category == "service"]
    accessories = [e for e in catalog.values() if e.category == "accessory"]

    q_choices.append(questionary.Separator("-- Services --"))
    for entry in services:
        q_choices.append(
            questionary.Choice(
                title=entry.name,
                value=entry.id,
                checked=(entry.id in existing),
            )
        )

    q_choices.append(questionary.Separator("-- Accessories --"))
    for entry in accessories:
        q_choices.append(
            questionary.Choice(
                title=entry.name,
                value=entry.id,
                checked=(entry.id in existing),
            )
        )

    result = _ask_or_interrupt(
        questionary.checkbox("Select services to deploy (space to toggle, enter to confirm)", choices=q_choices)
    )
    return result  # type: ignore[return-value]


# ── SSH Key Helpers ─────────────────────────────────────────────────────


def _fetch_github_keys(username: str) -> list[SSHKey]:
    """Fetch public SSH keys for *username* from GitHub.

    Returns an empty list on network or HTTP errors (with a warning
    printed to the console).
    """
    url = f"https://github.com/{username}.keys"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        console.warning(f"Could not fetch keys from GitHub: {e}")
        return []

    keys: list[SSHKey] = []
    for line in body.strip().splitlines():
        line = line.strip()
        if line:
            keys.append(SSHKey(username=username, public_key=line, source="github"))
    if not keys:
        console.warning(f"No SSH keys found for GitHub user '{username}'")
    return keys


def _prompt_ssh_keys() -> list[SSHKey]:
    """Collect SSH keys via an interactive loop.

    There is no "skip" option, and the loop does not exit with an empty
    list. Provisioning disables root login and password authentication,
    so an admin account with no key locks the operator out of the server.
    Ctrl-C still aborts the whole wizard.
    """
    collected: list[SSHKey] = []

    while True:
        console.header("SSH Keys")
        if not collected:
            console.console.print(
                "  [dim]At least one key is required — provisioning disables "
                "root login and password auth.[/dim]"
            )
        choice = _prompt_choice(
            "Add an SSH key",
            [
                ("github", "Fetch from GitHub username"),
                ("paste", "Paste a public key"),
                ("local", "Use a local key from ~/.ssh/*.pub"),
            ],
        )

        if choice == "github":
            username = Prompt.ask("  GitHub username")
            keys = _fetch_github_keys(username)
            if keys:
                console.success(f"Fetched {len(keys)} key(s) for {username}")
                collected.extend(keys)

        elif choice == "paste":
            key_text = Prompt.ask("  Public key")
            try:
                key = parse_ssh_public_key(key_text)
            except BayError as e:
                console.warning(str(e))
                continue
            username = Prompt.ask("  Username label", default=key.username)
            collected.append(SSHKey(username=username, public_key=key.public_key, source="manual"))
            console.success(f"Added key for {username}")

        elif choice == "local":
            local_keys = discover_local_ssh_keys()
            if not local_keys:
                console.warning("No public keys found in ~/.ssh/*.pub")
                continue
            picked = _prompt_choice(
                "  Which key?",
                [(k.public_key, f"{k.public_key[:28]}… ({k.username})") for k in local_keys],
            )
            for k in local_keys:
                if k.public_key == picked:
                    collected.append(k)
                    console.success(f"Added key for {k.username}")
                    break

        if collected and not _prompt_confirm("  Add another SSH key?", default=False):
            break

    return collected


# ── Region Prompts ──────────────────────────────────────────────────────


def _prompt_regions() -> list[RegionConfig]:
    """Prompt for multi-region configuration and return the list."""
    console.console.print("  [dim]You can add more regions later by editing your inventory and group_vars.[/dim]")
    count_str = _ask_or_interrupt(
        questionary.text(
            "How many regions do you want to configure now?",
            default="1",
            validate=lambda val: val.isdigit() and int(val) >= 1,
        )
    )
    count = int(count_str)  # type: ignore[arg-type]

    regions: list[RegionConfig] = []
    for i in range(count):
        suggestion = _REGION_SUGGESTIONS[i] if i < len(_REGION_SUGGESTIONS) else ""
        default_name = suggestion or None

        console.console.print(f"\n  [bold]Region {i + 1}/{count}[/bold]")
        name = _prompt_validated(
            "    Region name",
            validate_region_name,
            default=default_name,  # type: ignore[arg-type]
        )
        ip = _prompt_validated("    Server IP or hostname", validate_ip)
        regions.append(RegionConfig(name=name, server_ip=ip))

    # Show summary table
    console.console.print()
    table = Table(title="Regions", show_header=True)
    table.add_column("Region", style="bold")
    table.add_column("Server")
    for r in regions:
        table.add_row(r.name, r.server_ip)
    console.console.print(table)

    return regions


# ── Summary Display ─────────────────────────────────────────────────────


def _show_summary(result: WizardResult) -> None:
    """Render a summary panel of all collected values."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value")

    table.add_row("Project", result.project_name)
    table.add_row("Mode", "multi-region" if result.multi_region else "single-server")

    if result.multi_region and result.regions:
        parts = []
        for i, r in enumerate(result.regions):
            label = f"{r.name} ({r.server_ip})"
            if i == 0 and result.access_gateway == "headscale":
                label += " [bold cyan]primary[/bold cyan]"
            parts.append(label)
        table.add_row("Regions", ", ".join(parts))
    elif result.server_ip:
        table.add_row("Server", result.server_ip)

    table.add_row("Domain", result.domain_base)
    table.add_row("Let's Encrypt", result.letsencrypt_email)

    if result.ssh_keys:
        keys_str = ", ".join(
            f"{k.username} ({k.source})" for k in result.ssh_keys
        )
        table.add_row("SSH keys", keys_str)
    else:
        table.add_row("SSH keys", "[dim]none[/dim]")

    if result.access_gateway == "headscale":
        table.add_row("Access Gateway", f"headscale ({result.headscale_domain})")
    elif result.access_gateway == "wireguard":
        if result.vpn_peer_ips:
            peers = ", ".join(result.vpn_peer_ips)
            table.add_row("Access Gateway", f"wireguard ({len(result.vpn_peer_ips)} peers: {peers})")
        else:
            table.add_row("Access Gateway", "wireguard (no peers yet)")
    else:
        table.add_row("Access Gateway", "[dim]none (all services public)[/dim]")

    if result.selected_services:
        catalog = _get_catalog()
        svc_labels = [
            catalog[s_id].name if s_id in catalog else s_id
            for s_id in result.selected_services
        ]
        table.add_row("Services", ", ".join(svc_labels))
    else:
        table.add_row("Services", "[dim]none[/dim]")

    console.console.print()
    console.console.print(Panel(table, title="[bold]Summary[/bold]", expand=False))
    console.console.print()


# ── Main Wizard Entry Point ─────────────────────────────────────────────


def run_wizard(existing: _ExistingConfig = None) -> WizardResult:
    """Run the interactive onboarding wizard and return collected values.

    When *existing* is provided, pre-fills prompts with current values
    (edit/resume mode).

    Prompts the user through project name, server configuration, domain,
    SSL, SSH keys, and service preset selection.  Displays a summary for
    confirmation before returning the result.

    Raises ``BayError`` if the user declines the final confirmation.
    """
    edit_mode = existing is not None
    if edit_mode:
        console.console.print("  [dim]Press Enter to keep current values, or type new values to change.[/dim]")
        console.console.print()

    # ── 1. Project name ─────────────────────────────────────────────
    console.header("Project Name")
    default_name = existing.project_name if existing else Path.cwd().name.lower()
    project_name = _prompt_validated(
        "  Project name",
        validate_project_name,
        default=default_name,
    )

    # ── 2. Multi-region? ────────────────────────────────────────────
    console.header("Deployment Mode")
    multi_region = _prompt_confirm(
        "  Multi-region deployment?",
        default=existing.multi_region if existing else False,
    )

    # ── 3. Server address (branching) ───────────────────────────────
    server_ip: str | None = None
    regions: list[RegionConfig] | None = None

    if multi_region:
        console.header("Region Configuration")
        regions = _prompt_regions()
    else:
        console.header("Server")
        server_ip = _prompt_validated(
            "  Server IP or hostname",
            validate_ip,
            **({"default": existing.server_ip} if existing and existing.server_ip else {}),
        )

    # ── 4. Domain base ──────────────────────────────────────────────
    console.header("Domain")
    console.console.print("  [dim]The base domain is used to generate subdomains for your services.[/dim]")
    if multi_region:
        region_names = [r.name for r in (regions or [])]
        example_region = region_names[0] if region_names else "eu"
        console.console.print(f"  [dim]Each region gets a prefix, e.g. for example.com:[/dim]")
        console.console.print(f"  [dim]  - Status page: status.{example_region}.example.com[/dim]")
        console.console.print(f"  [dim]  - Your app:    app.{example_region}.example.com[/dim]")
        console.console.print()
        if regions:
            for r in regions:
                console.console.print(f"  [dim]DNS: *.{r.name}.example.com -> {r.server_ip}[/dim]")
        console.console.print()
        console.console.print("  [dim]Enter the root domain — region prefixes are added automatically.[/dim]")
    else:
        console.console.print("  [dim]For example, if your base domain is example.com:[/dim]")
        console.console.print("  [dim]  - Status page: status.example.com[/dim]")
        console.console.print("  [dim]  - Your app:    app.example.com[/dim]")
        console.console.print()
        if server_ip:
            console.console.print(f"  [dim]DNS: Create a wildcard A record: *.example.com -> {server_ip}[/dim]")
        else:
            console.console.print("  [dim]DNS: Create a wildcard A record: *.yourdomain.com -> your-server-ip[/dim]")
    console.console.print()
    default_domain = existing.domain_base if existing and existing.domain_base else None
    domain_base = _prompt_validated(
        "  Base domain (e.g., example.com)",
        validate_domain,
        **({"default": default_domain} if default_domain else {}),
    )

    # ── 5. Let's Encrypt email ──────────────────────────────────────
    console.header("SSL Certificate")
    default_email = existing.letsencrypt_email if existing and existing.letsencrypt_email else f"admin@{domain_base}"
    letsencrypt_email = Prompt.ask("  Let's Encrypt email", default=default_email)

    # ── 6. SSH keys ─────────────────────────────────────────────────
    ssh_keys = _prompt_ssh_keys()

    # ── 7. Access gateway ────────────────────────────────────────────
    console.header("Access Gateway")
    console.console.print("  [dim]Services with access: vpn are restricted to VPN clients only.[/dim]")
    console.console.print("  [dim]Choose how VPN clients connect to your server:[/dim]")
    console.console.print()
    gw_choices = [
        ("none", "None (Recommended to start) -- no VPN, all services public"),
        ("headscale", "Headscale -- self-hosted Tailscale, auto device enrollment"),
        ("wireguard", "WireGuard -- manual peer configuration"),
    ]
    console.console.print(
        "  [dim]Headscale adds a DNS record, a Tailscale client and four "
        "post-deploy steps.[/dim]"
    )
    console.console.print(
        "  [dim]You can add it later by re-running: bin/bay setup --gateway headscale[/dim]"
    )
    console.console.print()
    gw_default = "none"
    if existing:
        gw_default = existing.access_gateway
    access_gateway = _prompt_choice("  Access gateway", gw_choices, default=gw_default)

    headscale_domain: str | None = None
    vpn_peer_ips: list[str] = []

    if access_gateway == "headscale":
        # Multi-region: ask which region is the primary (runs headscale server)
        if multi_region and regions and len(regions) > 1:
            console.console.print()
            console.console.print("  [dim]One region must host the Headscale coordination server (primary).[/dim]")
            console.console.print("  [dim]Other regions connect to it as remote nodes.[/dim]")
            console.console.print()
            region_choices = [(r.name, f"{r.name} ({r.server_ip})") for r in regions]
            primary_name = _prompt_choice("  Primary Headscale region", region_choices, default=regions[0].name)
            # Reorder so primary is first (scaffold uses index 0 as control)
            primary_idx = next(i for i, r in enumerate(regions) if r.name == primary_name)
            if primary_idx != 0:
                regions.insert(0, regions.pop(primary_idx))
                console.success(f"Primary region: {primary_name}")

        if multi_region and regions:
            # Derive from primary region's domain (region is already at index 0)
            default_hs_domain = f"hs.{regions[0].name}.{domain_base}"
        elif existing and existing.headscale_domain:
            default_hs_domain = existing.headscale_domain
        else:
            default_hs_domain = f"hs.{domain_base}"
        headscale_domain = _prompt_validated(
            "  Headscale domain",
            validate_domain,
            default=default_hs_domain,
        )

    elif access_gateway == "wireguard":
        console.console.print("  [dim]Enter WireGuard peer IPs (empty line to finish):[/dim]")
        while True:
            ip_input = Prompt.ask("    IP", default="")
            if not ip_input:
                break
            try:
                ipaddress.ip_address(ip_input.strip())
                vpn_peer_ips.append(ip_input.strip())
            except ValueError:
                console.error(f"Invalid IP address: {ip_input}")
        if vpn_peer_ips:
            console.success(f"Added {len(vpn_peer_ips)} VPN peer IP(s)")
        else:
            console.info("No IPs added now. You can add them later in group_vars/all/vpn_access.yml")

    # ── 8. Service selection ─────────────────────────────────────────
    console.header("Services")

    existing_services = existing.selected_services if existing else None
    selected_services = _prompt_services(existing_services)

    # Auto-select required accessories
    catalog = _get_catalog()
    auto_added = resolve_dependencies(selected_services, catalog, [])
    for dep_id in auto_added:
        if dep_id not in selected_services:
            selected_services.append(dep_id)

    if selected_services:
        labels = [
            catalog[s_id].name if s_id in catalog else s_id
            for s_id in selected_services
        ]
        console.success(f"Selected: {', '.join(labels)}")
        if auto_added:
            dep_labels = [
                catalog[s_id].name if s_id in catalog else s_id
                for s_id in auto_added
            ]
            console.info(f"Auto-added dependencies: {', '.join(dep_labels)}")
    else:
        console.info("No services selected -- blank services.yml will be generated")

    # ── 9. Vault password ────────────────────────────────────────────
    vault_pass_file = Path.cwd() / ".vault_pass"
    if not vault_pass_file.exists():
        console.header("Vault Password")
        console.console.print("  [dim]Ansible Vault encrypts your secrets (database passwords, API keys).[/dim]")
        console.console.print("  [dim]The vault password is stored locally in .vault_pass (gitignored).[/dim]")
        console.console.print()
        choice = _prompt_choice(
            "  Vault password",
            [
                ("generate", "Generate a strong password (Recommended)"),
                ("manual", "Enter manually"),
                ("skip", "Skip (create later)"),
            ],
        )
        if choice == "generate":
            vault_pass = generate_password(32)
            vault_pass_file.write_text(vault_pass + "\n")
            vault_pass_file.chmod(0o600)
            console.success("Created .vault_pass with generated password")
            console.console.print(f"  [dim]Password: {vault_pass}[/dim]")
            console.console.print("  [dim]Store this somewhere safe -- you'll need it to decrypt secrets.[/dim]")
        elif choice == "manual":
            vault_pass = Prompt.ask("  Enter vault password", password=True)
            if vault_pass:
                vault_pass_file.write_text(vault_pass + "\n")
                vault_pass_file.chmod(0o600)
                console.success("Created .vault_pass")
            else:
                console.info("Skipped -- create it later: echo 'your-password' > .vault_pass")
        else:
            console.info("Skipped -- create it later: echo 'your-password' > .vault_pass")
        console.console.print()

    # ── 10. Summary & confirmation ──────────────────────────────────
    result = WizardResult(
        project_name=project_name,
        multi_region=multi_region,
        server_ip=server_ip,
        regions=regions,
        domain_base=domain_base,
        letsencrypt_email=letsencrypt_email,
        ssh_keys=ssh_keys,
        access_gateway=access_gateway,
        headscale_domain=headscale_domain,
        vpn_peer_ips=vpn_peer_ips,
        selected_services=selected_services,
    )

    _show_summary(result)

    if not _prompt_confirm("  Generate project scaffold?", default=True):
        raise BayError("Scaffold generation cancelled")

    return result
