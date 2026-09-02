"""Framework lifecycle commands: setup, install, update, status."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.panel import Panel

from bay_cli import ansible, console, git, paths
from bay_cli.errors import BayError

if TYPE_CHECKING:
    from bay_cli.wizard.models import WizardResult

app = typer.Typer()


_VALID_GATEWAYS = ("headscale", "wireguard", "none")


@app.command()
def setup(
    no_interactive: bool = typer.Option(
        False,
        "--no-interactive",
        help="Skip wizard, copy all example files.",
    ),
    defaults: bool = typer.Option(
        False,
        "--defaults",
        help="Use sensible defaults without prompting.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing scaffold files.",
    ),
    name: str | None = typer.Option(None, "--name", help="Project name."),
    server_ip: str | None = typer.Option(None, "--server-ip", help="Server IP or hostname."),
    domain: str | None = typer.Option(None, "--domain", help="Base domain (e.g. example.com)."),
    gateway: str | None = typer.Option(None, "--gateway", help="Access gateway: headscale, wireguard, or none."),
    headscale_domain: str | None = typer.Option(None, "--headscale-domain", help="Headscale domain."),
    services: str | None = typer.Option(None, "--services", help="Comma-separated service IDs."),
    letsencrypt_email: str | None = typer.Option(
        None, "--email", "--letsencrypt-email",
        help="Let's Encrypt contact address. Defaults to admin@<domain>.",
    ),
    multi_region: bool = typer.Option(False, "--multi-region", help="Enable multi-region mode."),
    vpn_peer_ips: str | None = typer.Option(None, "--vpn-peer-ips", help="Comma-separated WireGuard peer IPs."),
    ssh_key: list[str] = typer.Option(
        [], "--ssh-key",
        help="Admin SSH public key, e.g. 'ssh-ed25519 AAAA…'. Repeatable.",
    ),
    ssh_key_file: list[str] = typer.Option(
        [], "--ssh-key-file",
        help="Path to a .pub file for the admin account. Repeatable.",
    ),
) -> None:
    """Run the interactive setup wizard to configure your project.

    Re-running on an existing project enters edit mode: current values are
    shown as defaults, press Enter to keep them. With all required flags
    (--name, --domain, --gateway, plus --server-ip or --multi-region) the
    wizard is skipped entirely — for scripts and CI. --defaults is honoured
    with or without a TTY and requires --server-ip and --domain. Any
    non-interactive path needs an admin SSH key: --ssh-key, --ssh-key-file,
    or a key found in ~/.ssh/*.pub. The access gateway defaults to none; add
    Headscale later with --gateway headscale. --email sets the Let's Encrypt
    contact address on every path; without it, admin@<domain> is used.

    Examples:

        bin/bay setup
        bin/bay setup --defaults --server-ip 203.0.113.10 --domain example.com
        bin/bay setup --name myapp --server-ip 203.0.113.10 \\
            --domain example.com --gateway headscale \\
            --headscale-domain hs.example.com --services gatus,postgres
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    console.show_banner(subtitle="Setup Wizard")

    # Guard first, before a single file is written: the built-in defaults were
    # 0.0.0.0 and example.com, which scaffold a project that can never deploy.
    if defaults and not (server_ip and domain):
        raise BayError(
            "--defaults needs both --server-ip and --domain\n"
            "  Without them the scaffold gets 0.0.0.0 and example.com, "
            "which can never deploy.\n\n"
            "  bin/bay setup --defaults --server-ip 203.0.113.10 --domain example.com"
        )

    # Detect already-initialized project → enter edit mode
    is_existing = (root / "ansible.cfg").exists()
    if is_existing and not force and not _has_any_flag(name, server_ip, domain, gateway):
        console.info("Existing project detected — entering edit mode.")
        console.info("Current values will be shown as defaults. Press Enter to keep them.")
        console.console.print()
        force = True  # edit mode overwrites changed files

    # Create bin/bay wrapper if missing
    _ensure_bin_wrapper(root, bay_dir)

    # ── Check for CLI flag paths ─────────────────────────────────────
    flags = _SetupFlags(
        name=name, server_ip=server_ip, domain=domain, gateway=gateway,
        headscale_domain=headscale_domain, services=services,
        letsencrypt_email=letsencrypt_email, multi_region=multi_region,
        vpn_peer_ips=vpn_peer_ips, ssh_key=list(ssh_key), ssh_key_file=list(ssh_key_file),
    )

    if defaults:
        # --defaults never falls back to the example copy, TTY or not.
        from bay_cli.wizard.scaffold import scaffold

        result = _build_result_from_defaults(flags, root)
        scaffold(result, root, force=force)
    elif flags.has_all_required():
        # All required flags → build result directly, skip wizard
        result = _build_result_from_flags(flags)
        from bay_cli.wizard.scaffold import scaffold
        scaffold(result, root, force=force)
    elif flags.has_any():
        # Partial flags → pre-fill wizard
        prefill = _flags_to_prefill(flags)
        result = _scaffold_project(root, bay_dir, flags, no_interactive=no_interactive, force=force, prefill=prefill)
    else:
        # No flags → existing flow
        result = _scaffold_project(root, bay_dir, flags, no_interactive=no_interactive, force=force)

    console.console.print()
    console.success("Setup complete")
    _print_next_steps(result, root)


class _SetupFlags:
    """Parsed CLI flags for the setup command."""

    def __init__(
        self,
        *,
        name: str | None,
        server_ip: str | None,
        domain: str | None,
        gateway: str | None,
        headscale_domain: str | None,
        services: str | None,
        letsencrypt_email: str | None,
        multi_region: bool,
        vpn_peer_ips: str | None,
        ssh_key: list[str] | None = None,
        ssh_key_file: list[str] | None = None,
    ) -> None:
        self.name = name
        self.server_ip = server_ip
        self.domain = domain
        self.gateway = gateway
        self.headscale_domain = headscale_domain
        self.services = services
        self.letsencrypt_email = letsencrypt_email
        self.multi_region = multi_region
        self.vpn_peer_ips = vpn_peer_ips
        self.ssh_key = ssh_key or []
        self.ssh_key_file = ssh_key_file or []

    def has_any(self) -> bool:
        return any([self.name, self.server_ip, self.domain, self.gateway, self.services])

    def has_all_required(self) -> bool:
        if not all([self.name, self.domain, self.gateway]):
            return False
        if not self.multi_region and not self.server_ip:
            return False
        if self.gateway == "headscale" and not self.headscale_domain:
            return False
        return True

    def parse_services(self) -> list[str]:
        if not self.services:
            return ["gatus"]
        return [s.strip() for s in self.services.split(",") if s.strip()]

    def parse_vpn_peer_ips(self) -> list[str]:
        if not self.vpn_peer_ips:
            return []
        return [ip.strip() for ip in self.vpn_peer_ips.split(",") if ip.strip()]


def _has_any_flag(*values: object) -> bool:
    return any(v is not None for v in values)


def _build_result_from_flags(flags: _SetupFlags) -> WizardResult:
    """Build a WizardResult directly from CLI flags (no wizard)."""
    from bay_cli.wizard.models import (
        WizardResult,
        resolve_ssh_keys,
        validate_domain,
        validate_ip,
        validate_project_name,
    )
    from bay_cli.catalog import load_catalog, resolve_dependencies
    from bay_cli.wizard.prompts import _get_catalog

    # Validate
    project_name = validate_project_name(flags.name or "")
    domain_base = validate_domain(flags.domain or "")

    if flags.gateway not in _VALID_GATEWAYS:
        raise BayError(f"Invalid gateway '{flags.gateway}' — must be one of: {', '.join(_VALID_GATEWAYS)}")

    server_ip: str | None = None
    if not flags.multi_region:
        server_ip = validate_ip(flags.server_ip or "")

    headscale_domain: str | None = None
    if flags.gateway == "headscale":
        if not flags.headscale_domain:
            raise BayError("--headscale-domain is required when --gateway is headscale")
        headscale_domain = validate_domain(flags.headscale_domain)

    # Parse and validate services
    catalog = _get_catalog()
    service_ids = flags.parse_services()
    valid_ids = set(catalog.keys())
    for s_id in service_ids:
        if s_id not in valid_ids:
            raise BayError(f"Unknown service '{s_id}' — valid services: {', '.join(sorted(valid_ids))}")

    # Auto-add dependencies
    auto_added = resolve_dependencies(service_ids, catalog, [])
    for dep_id in auto_added:
        if dep_id not in service_ids:
            service_ids.append(dep_id)

    letsencrypt_email = flags.letsencrypt_email or f"admin@{domain_base}"
    vpn_peer_ips = flags.parse_vpn_peer_ips()

    return WizardResult(
        project_name=project_name,
        multi_region=flags.multi_region,
        server_ip=server_ip,
        domain_base=domain_base,
        letsencrypt_email=letsencrypt_email,
        ssh_keys=resolve_ssh_keys(flags.ssh_key, flags.ssh_key_file),
        access_gateway=flags.gateway,  # type: ignore[arg-type]
        headscale_domain=headscale_domain,
        vpn_peer_ips=vpn_peer_ips,
        selected_services=service_ids,
    )


def _build_result_from_defaults(flags: _SetupFlags, root: Path) -> WizardResult:
    """Build the --defaults WizardResult, with CLI flags taking precedence.

    ``--defaults`` used to be reachable only from a TTY: the non-TTY guard
    in ``_scaffold_project`` ran first and quietly copied ``example/``
    instead, so a scripted setup produced a different (and invalid) tree.
    """
    from bay_cli.catalog import resolve_dependencies
    from bay_cli.wizard.models import (
        defaults_result,
        resolve_ssh_keys,
        validate_domain,
        validate_ip,
        validate_project_name,
    )
    from bay_cli.wizard.prompts import _get_catalog

    result = defaults_result(root.name)

    if flags.name:
        result.project_name = validate_project_name(flags.name)
    if flags.server_ip:
        result.server_ip = validate_ip(flags.server_ip)
    if flags.domain:
        result.domain_base = validate_domain(flags.domain)
        # Keep the derived values on the same domain as the override.
        result.letsencrypt_email = f"admin@{result.domain_base}"
    if flags.gateway:
        if flags.gateway not in _VALID_GATEWAYS:
            raise BayError(
                f"Invalid gateway '{flags.gateway}' — must be one of: {', '.join(_VALID_GATEWAYS)}"
            )
        result.access_gateway = flags.gateway  # type: ignore[assignment]
    if flags.headscale_domain:
        result.headscale_domain = validate_domain(flags.headscale_domain)
    elif result.access_gateway == "headscale" and not result.headscale_domain:
        # Only derived when headscale is actually selected — the default
        # gateway is `none`, and that scaffold carries no headscale_domain.
        result.headscale_domain = f"hs.{result.domain_base}"
    if flags.letsencrypt_email:
        result.letsencrypt_email = flags.letsencrypt_email
    if flags.services:
        catalog = _get_catalog()
        service_ids = flags.parse_services()
        for s_id in service_ids:
            if s_id not in catalog:
                raise BayError(
                    f"Unknown service '{s_id}' — valid services: {', '.join(sorted(catalog))}"
                )
        for dep_id in resolve_dependencies(service_ids, catalog, []):
            if dep_id not in service_ids:
                service_ids.append(dep_id)
        result.selected_services = service_ids

    result.ssh_keys = resolve_ssh_keys(flags.ssh_key, flags.ssh_key_file)
    result.vpn_enabled = result.access_gateway != "none"
    if result.access_gateway == "headscale" and not result.headscale_domain:
        raise BayError("headscale_domain is required when access_gateway is headscale")
    return result


def _flags_to_prefill(flags: _SetupFlags) -> WizardResult:
    """Build a partial WizardResult from CLI flags to pre-fill the wizard."""
    from bay_cli.wizard.models import WizardResult

    gateway = flags.gateway if flags.gateway in _VALID_GATEWAYS else "none"
    headscale_domain = flags.headscale_domain
    if gateway == "headscale" and not headscale_domain and flags.domain:
        headscale_domain = f"hs.{flags.domain}"

    return WizardResult(
        project_name=flags.name or "my-project",
        multi_region=flags.multi_region,
        server_ip=flags.server_ip,
        domain_base=flags.domain or "",
        letsencrypt_email=flags.letsencrypt_email or "",
        ssh_keys=[],
        access_gateway=gateway,  # type: ignore[arg-type]
        headscale_domain=headscale_domain or "hs.example.com",
        vpn_peer_ips=flags.parse_vpn_peer_ips(),
        selected_services=flags.parse_services(),
    )


#: Single source of truth for the consumer's ``bin/bay`` wrapper. ``bootstrap.sh``
#: copies the very same file, so the two writers cannot drift apart.
WRAPPER_SOURCE = "scripts/bin-bay-wrapper.sh"


def _ensure_bin_wrapper(root: Path, bay_dir: Path) -> None:
    """Create bin/bay wrapper script if it doesn't exist."""
    bin_dir = root / "bin"
    wrapper = bin_dir / "bay"
    if wrapper.exists():
        return
    source = bay_dir / WRAPPER_SOURCE
    if not source.is_file():
        raise BayError(
            f"wrapper template missing at {source} — the framework checkout is incomplete; "
            "re-run '.bay/bootstrap.sh'"
        )
    bin_dir.mkdir(exist_ok=True)
    wrapper.write_text(source.read_text())
    wrapper.chmod(0o755)
    console.success("Created bin/bay wrapper")


def _print_next_steps(result: WizardResult | None, root: Path) -> None:
    """Print tailored next-steps guidance based on gateway selection."""
    reqs: list[str] = []
    steps: list[str] = []

    # ── Requirements ──────────────────────────────────────────────────
    if result is not None and result.access_gateway == "headscale":
        reqs.append("Tailscale client on your device ([dim]https://tailscale.com/download[/dim])")
    elif result is not None and result.access_gateway == "none":
        console.warning("No access gateway configured. All services will be publicly accessible.")

    # ── Steps ─────────────────────────────────────────────────────────
    # DNS comes first, for every gateway choice. Traefik asks Let's Encrypt
    # for certificates on the first deploy; with no record the ACME challenge
    # fails and the deploy looks broken for a reason that is not Bay's.
    steps.extend(_dns_steps(result))

    # Vault
    vault_pass = root / ".vault_pass"
    secrets_file = root / "group_vars" / "production" / "secrets.yml"
    if not vault_pass.exists():
        steps.append("Add your vault password:  [dim]echo 'your-password' > .vault_pass[/dim]")
    if secrets_file.exists():
        rel = secrets_file.relative_to(root)
        steps.append(f"Edit [bold]{rel}[/bold] and add your secrets")
        steps.append("Encrypt secrets:         [dim]bin/bay vault encrypt production[/dim]")
    else:
        steps.append("Set your secrets:        [dim]bin/bay vault edit production[/dim]")

    # Pre-flight, then deploy
    steps.append("Check your config:       [dim]bin/bay validate[/dim]")
    steps.append("Check the environment:   [dim]bin/bay doctor[/dim]")
    steps.append("Provision the server:    [dim]bin/bay provision production[/dim]")
    if result is not None and result.multi_region and result.regions and result.access_gateway == "headscale":
        control_region = result.regions[0].name
        remote_regions = [r.name for r in result.regions[1:]]
        steps.append(f"Deploy control region:   [dim]bin/bay deploy {control_region}[/dim]")
        steps.append(f"Generate API key:        [dim]bin/bay gateway apikey[/dim]")
        steps.append("Add [bold]headscale_api_key[/bold] to secrets: [dim]bin/bay vault edit production[/dim]")
        for rn in remote_regions:
            steps.append(f"Deploy remote region:    [dim]bin/bay deploy {rn}[/dim]")
    else:
        steps.append("Deploy services:         [dim]bin/bay deploy production[/dim]")

    # Gateway post-deploy
    if result is not None and result.access_gateway == "headscale":
        domain = result.headscale_domain or f"hs.{result.domain_base}"
        steps.append("Create a user:           [dim]bin/bay gateway add-user <name>[/dim]")
        steps.append("Generate pre-auth key:   [dim]bin/bay gateway key <name>[/dim]")
        steps.append(f"Enroll your device:      [dim]tailscale up --login-server=https://{domain} --authkey=<key>[/dim]")
    elif result is not None and result.access_gateway == "wireguard":
        steps.append("Configure WireGuard peers with the IPs you specified")

    # ── Render ────────────────────────────────────────────────────────
    lines: list[str] = []
    if reqs:
        lines.append("  [bold]Requirements[/bold]")
        for req in reqs:
            lines.append(f"    \u2022 {req}")
        lines.append("")
    for i, step in enumerate(steps, 1):
        lines.append(f"  {i}. {step}")

    # Doc reference for gateway setup
    if result is not None and result.access_gateway == "headscale":
        lines.append("")
        lines.append("  [dim]Full walkthrough: docs/access-gateways.md \u2192 Headscale quick start[/dim]")

    console.console.print()
    console.console.print(Panel("\n".join(lines), title="[bold]Next steps[/bold]", expand=False))
    console.console.print(f"  [dim]You're all set \u2014 happy shipping! \u2693[/dim]")
    console.console.print()


def _dns_steps(result: WizardResult | None) -> list[str]:
    """DNS records the operator must create before the first deploy.

    Printed for every gateway choice, not only Headscale: Traefik requests a
    certificate for every routed host on the first deploy, and the ACME
    challenge cannot succeed until the name resolves to the server.
    """
    generic = ["Create DNS records:      [dim]point your domain at the server IP[/dim]"]
    if result is None or not result.domain_base:
        return generic
    base = result.domain_base
    if result.multi_region and result.regions:
        target = "the region server"
    else:
        target = result.server_ip or "your server IP"
    steps = [f"Create DNS record:       [dim]*.{base} \u2192 {target}[/dim]"]
    if result.access_gateway == "headscale":
        hs = result.headscale_domain or f"hs.{base}"
        if not hs.endswith(f".{base}"):
            steps.append(f"Create DNS record:       [dim]{hs} \u2192 {target}[/dim]")
    return steps


def _scaffold_project(
    root: Path,
    bay_dir: Path,
    flags: _SetupFlags,
    *,
    no_interactive: bool,
    force: bool = False,
    prefill: WizardResult | None = None,
) -> WizardResult | None:
    """Generate project scaffold files using wizard or example copy.

    When *prefill* is provided (from CLI flags), it is used as the
    ``existing`` parameter for ``run_wizard()``.

    Returns the WizardResult when available (interactive or --defaults mode),
    or None when copying static examples.
    """
    console.header("Scaffolding project")

    if no_interactive:
        _copy_example_tree(bay_dir, root, flags, force=force)
        return None

    # Auto-detect non-TTY and fall back
    if not sys.stdin.isatty():
        console.warning("Non-interactive terminal detected, copying example files")
        _copy_example_tree(bay_dir, root, flags, force=force)
        return None

    # Interactive wizard
    try:
        from bay_cli.wizard.models import load_existing_config
        from bay_cli.wizard.prompts import run_wizard
        from bay_cli.wizard.scaffold import scaffold

        # Prefer CLI prefill, then existing config (edit mode)
        existing = prefill or (load_existing_config(root) if force else None)
        result = run_wizard(existing=existing)

        scaffold(result, root, force=force)
        return result
    except KeyboardInterrupt:
        console.console.print()
        console.warning("Wizard cancelled — no project files were created")
        console.info("Run 'bin/bay setup' to try again, or 'bin/bay setup --no-interactive' for defaults")
        raise typer.Exit(1)


def _copy_example_tree(bay_dir: Path, root: Path, flags: _SetupFlags, *, force: bool) -> None:
    """Copy ``example/`` and fill the gaps it cannot ship: keys, secrets, email."""
    from bay_cli.wizard.models import resolve_ssh_keys
    from bay_cli.wizard.scaffold import copy_examples, fill_example_gaps

    ssh_keys = resolve_ssh_keys(flags.ssh_key, flags.ssh_key_file)
    copy_examples(bay_dir, root, force=force)
    fill_example_gaps(root, ssh_keys, letsencrypt_email=_resolve_letsencrypt_email(flags))


def _resolve_letsencrypt_email(flags: _SetupFlags) -> str | None:
    """Return the ACME contact address, deriving ``admin@<domain>`` if needed.

    The example tree ships a placeholder that `bay validate` rejects, so a
    scaffold that leaves it in place produces a failing project.
    """
    if flags.letsencrypt_email:
        return flags.letsencrypt_email
    if flags.domain:
        derived = f"admin@{flags.domain.strip().lower()}"
        console.info(f"No --email given, using {derived} for Let's Encrypt")
        return derived
    console.warning(
        "No --email or --domain given — letsencrypt_email keeps its placeholder "
        "and 'bin/bay validate' will fail until you set it"
    )
    return None


@app.command()
def guide() -> None:
    """Show tailored next steps for this project's current state.

    Examples:

        bin/bay guide
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    console.show_banner(subtitle="Setup Guide")

    from bay_cli.wizard.models import load_existing_config

    result = load_existing_config(root)
    _print_next_steps(result, root)


@app.command()
def install() -> None:
    """Install the framework version pinned in .bay-version.

    Checks out the pinned tag inside .bay/, links group_vars into the
    framework, and syncs Python/Ansible dependencies. Refuses to run in
    dev-link mode (`bin/bay dev-unlink` first).

    Examples:

        bin/bay install
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    if paths.is_dev_linked(root):
        raise BayError(
            "Cannot install while in dev-link mode",
            hint="Run 'bin/bay dev-unlink' first to restore normal mode.",
        )

    console.show_banner(subtitle="Install")

    git.fetch_tags(bay_dir)

    # Determine version
    version = paths.read_pinned_version(root)
    if version is None:
        version = git.latest_tag(bay_dir)
        if not version:
            raise BayError("No tags found in framework repository")
        (root / ".bay-version").write_text(f"{version}\n")
        console.info(f"No .bay-version found, using latest: {version}")

    # Check if already at target
    current = git.describe_tags(bay_dir)
    deps_marker = bay_dir / ".deps-synced"
    deps_synced = deps_marker.exists() and deps_marker.read_text().strip() == version

    if current == version and deps_synced:
        # Still repair the links. `update` runs the *old* CLI — it checks out
        # the new version but the code doing the work is the version being
        # replaced — so anything a release adds to install/update lands one
        # bump late. `install` on an already-current checkout is the repair
        # path, and it only works if it runs before this early return.
        _ensure_skill_link(bay_dir, root)
        console.success(f"Already at {version}")
        return

    if current != version:
        git.checkout(bay_dir, version)

    # Symlink group_vars
    link = bay_dir / "group_vars"
    link.unlink(missing_ok=True)
    os.symlink("../group_vars", link)

    _ensure_skill_link(bay_dir, root)

    ansible.sync_deps(bay_dir)
    deps_marker.write_text(f"{version}\n")

    console.console.print()
    console.success(f"Install complete: {version}")


@app.command()
def update() -> None:
    """Update to the latest framework release (bumps .bay-version).

    Fetches tags, checks out the newest one, rewrites .bay-version, and
    syncs dependencies. Refuses to run in dev-link mode.

    Examples:

        bin/bay update
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    if paths.is_dev_linked(root):
        raise BayError(
            "Cannot update while in dev-link mode",
            hint="Run 'bin/bay dev-unlink' first to restore normal mode.",
        )

    console.show_banner(subtitle="Update")

    git.fetch_tags(bay_dir)

    latest = git.latest_tag(bay_dir)
    if not latest:
        raise BayError("No tags found in framework repository")

    pinned = paths.read_pinned_version(root)
    checkout = git.describe_tags(bay_dir)
    deps_marker = bay_dir / ".deps-synced"
    deps_synced = deps_marker.exists() and deps_marker.read_text().strip() == latest

    if pinned == latest and checkout == latest and deps_synced:
        console.success(f"Already at latest: {latest}")
        return

    if pinned == latest and checkout == latest:
        console.info(f"Syncing dependencies for {latest}...")
    elif pinned == latest:
        console.info(f"Syncing: checkout {checkout or 'unknown'} -> {latest}")
    else:
        console.info(f"Updating: {pinned or 'unset'} -> {latest}")

    if checkout != latest:
        git.checkout(bay_dir, latest)
    (root / ".bay-version").write_text(f"{latest}\n")

    # Symlink group_vars
    link = bay_dir / "group_vars"
    link.unlink(missing_ok=True)
    os.symlink("../group_vars", link)

    _ensure_skill_link(bay_dir, root)

    ansible.sync_deps(bay_dir)
    deps_marker.write_text(f"{latest}\n")

    console.console.print()
    console.success(f"Update complete: {latest}")


def _ensure_skill_link(framework_path: Path, root: Path) -> None:
    """Expose the framework's SKILL.md where an agent's skill router looks.

    SKILL.md lives at the framework root, inside a gitignored .bay/ — no
    router will ever find it there, which would make its frontmatter
    decorative. Linking it to .claude/skills/bay/SKILL.md makes the skill
    real, and because the link points *through* .bay/ the content stays
    pinned to the installed framework version (and follows dev-link).

    Idempotent and conservative: only ever touches a symlink it could have
    created itself. A real file at that path is the operator's own skill and
    is left alone.
    """
    source = framework_path / "SKILL.md"
    if not source.is_file():
        # Framework predates SKILL.md — nothing to link.
        return

    link = root / ".claude" / "skills" / "bay" / "SKILL.md"

    if link.is_file() and not link.is_symlink():
        console.warning(
            f"{link.relative_to(root)} is a real file — leaving it alone "
            "(run 'bin/bay --skill' for the framework's own copy)"
        )
        return

    # Relative, and deliberately NOT resolved: the link points *through*
    # .bay/, so it survives the repo moving and keeps working when .bay/ is
    # itself a dev-link to a sibling checkout.
    target = os.path.relpath(source, link.parent)

    if link.is_symlink():
        if os.readlink(link) == target:
            return
        link.unlink()

    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)
    console.info("Linked SKILL.md into .claude/skills/bay/")


def _ensure_group_vars_link(framework_path: Path, root: Path) -> None:
    """Ensure framework_path/group_vars is a healthy symlink → root/group_vars.

    Idempotent: creates the link if missing, repairs it if broken or pointing
    at the wrong target, leaves it alone if already correct.

    Raises BayError when group_vars/ in the framework dir is a real
    directory (not a symlink) — the operator must resolve that manually
    because we can't tell whether the contents are valuable.
    """
    gv_link = framework_path / "group_vars"
    gv_target = root / "group_vars"

    # Real directory in the framework path — bail rather than clobber it.
    # Note: a broken symlink reports is_dir() == False, so this only fires
    # for genuine directories.
    if gv_link.is_dir() and not gv_link.is_symlink():
        raise BayError(
            "group_vars/ in the framework directory is a real directory, not a symlink. "
            "Remove it manually or run 'bin/bay dev-unlink' first.",
        )

    if not gv_target.is_dir():
        # Consumer has no group_vars/ to link to — nothing to do.
        return

    expected = gv_target.resolve()

    if gv_link.is_symlink():
        old_raw = os.readlink(gv_link)
        try:
            current = gv_link.resolve(strict=True)
        except (OSError, RuntimeError):
            # Broken symlink or symlink loop
            current = None
        if current == expected:
            return
        gv_link.unlink()
        os.symlink(str(expected), gv_link)
        console.info(f"Repaired group_vars link (was: {old_raw})")
        return

    # No link present — create it.
    os.symlink(str(expected), gv_link)
    console.info("Linked group_vars into framework")


_DEV_LINK_CONTENT = """\
# Bay Development Link Active
#
# The .bay/ directory is symlinked to a local framework checkout
# instead of the normal pinned git clone. This means:
#
#   - Framework changes are immediately visible (no commit/tag/push cycle)
#   - .bay-version is NOT enforced — you are running whatever is in the linked repo
#   - Do NOT deploy to production in this mode
#
# To restore normal mode:
#
#   bin/bay dev-unlink
#
# This will remove the symlink, re-clone the framework at the pinned
# version from .bay-version, and delete this file.
"""


@app.command("dev-link")
def dev_link(
    path: str = typer.Argument(
        "../bay",
        help="Path to local framework checkout (default: ../bay).",
    ),
) -> None:
    """Link .bay/ to a local framework checkout for development.

    Replaces the pinned clone with a symlink so framework edits are
    visible immediately — no commit/tag/push cycle. Creates a .bay-dev
    sentinel and a warning banner while active. .bay-version is NOT
    enforced in this mode; do not deploy to production while dev-linked.

    Examples:

        bin/bay dev-link
        bin/bay dev-link ../bay
    """
    root = Path.cwd()
    bay_dir = root / ".bay"
    framework_path = Path(path)

    # Resolve framework path
    if not framework_path.is_absolute():
        framework_path = (root / framework_path).resolve()

    if not framework_path.is_dir():
        raise BayError(
            f"Framework directory not found: {framework_path}",
            hint="Pass the path to your local bay checkout.",
        )

    if not (framework_path / ".git").is_dir():
        raise BayError(
            f"Not a git repository: {framework_path}",
            hint="The framework path must be a git repository.",
        )

    already_linked = paths.is_dev_linked(root)
    if already_linked:
        console.warning("Already in dev-link mode")
        console.info(f"Linked to: {bay_dir.resolve()}")
    else:
        console.show_banner(subtitle="Dev Link")

        # Remove existing .bay/ (git clone)
        if bay_dir.is_dir():
            import shutil
            shutil.rmtree(bay_dir)
            console.info("Removed existing .bay/ clone")

        # Create symlink
        # Use relative path if possible (cleaner), fall back to absolute
        try:
            rel = os.path.relpath(framework_path, root)
            os.symlink(rel, bay_dir)
        except ValueError:
            # Windows cross-drive or other edge case
            os.symlink(str(framework_path), bay_dir)

        console.info(f"Symlinked .bay/ → {os.path.relpath(framework_path, root)}")

    # Always ensure the group_vars symlink is healthy — even when already in
    # dev-link mode. The symlink can go missing if the framework checkout is
    # cleaned, the consumer's group_vars/ is moved, or the link was never
    # created (e.g., dev-link from a pre-symlink-support version of the CLI).
    # Without it, ansible-playbook's group_vars resolution fails because
    # playbook_dir resolves to bay/ and finds no group_vars there. Symptom:
    # `Error processing keyword 'become_user': 'app_user' is undefined`.
    _ensure_group_vars_link(framework_path, root)

    if already_linked:
        return

    # Create sentinel file
    sentinel = paths.dev_link_file(root)
    sentinel.write_text(_DEV_LINK_CONTENT)
    console.info(f"Created {sentinel.name} sentinel file")

    console.console.print()
    console.warning("DEVELOPMENT MODE — framework is symlinked, not pinned")
    console.info("Run 'bin/bay dev-unlink' to restore normal mode before deploying")
    console.console.print()
    console.success("Dev link active")


DEFAULT_BAY_REPO = "https://github.com/AltanS/bay.git"

_BAY_REPO_RE = re.compile(r"^\s*BAY_REPO\s*[:?]?=\s*(\S+)", re.M)


def _consumer_bay_repo(root: Path) -> str:
    """Where this consumer clones the framework from.

    `dev-unlink` used to hard-code the clone URL, so a consumer that had set
    BAY_REPO — to a fork, to an HTTPS remote behind a proxy, or to a local
    path, which is exactly what the bootstrap test does — got the hard-coded
    repo back instead of their own. `make bay:setup` honours BAY_REPO; this
    is the one place that did not.

    The Makefile is the source of truth because that is where `make bay:setup`
    reads it from. Falling back to the public default keeps a consumer whose
    Makefile predates the var working.
    """
    makefile = root / "Makefile"
    if makefile.is_file():
        match = _BAY_REPO_RE.search(makefile.read_text())
        if match:
            return match.group(1)
    return DEFAULT_BAY_REPO


@app.command("dev-unlink")
def dev_unlink() -> None:
    """Remove the dev link and restore the pinned framework clone.

    Re-clones the framework and checks out the version in .bay-version.

    Examples:

        bin/bay dev-unlink
    """
    root = Path.cwd()
    bay_dir = root / ".bay"

    if not paths.is_dev_linked(root):
        console.info("Not in dev-link mode — nothing to do")
        return

    console.show_banner(subtitle="Dev Unlink")

    # Clean up group_vars symlink inside the framework dir before removing .bay/
    if bay_dir.is_symlink():
        framework_path = bay_dir.resolve()
        gv_link = framework_path / "group_vars"
        if gv_link.is_symlink():
            gv_link.unlink()
            console.info("Removed group_vars symlink from framework directory")
        bay_dir.unlink()
        console.info("Removed .bay/ symlink")

    # Remove sentinel
    sentinel = paths.dev_link_file(root)
    sentinel.unlink(missing_ok=True)
    console.info(f"Removed {sentinel.name}")

    # Re-clone and install pinned version
    version = paths.read_pinned_version(root)
    if not version:
        console.warning("No .bay-version found — run 'make bay:setup' to re-initialize")
        return

    console.info(f"Restoring framework at {version}...")

    from bay_cli import runner

    # Clone from wherever this consumer actually gets the framework
    repo = _consumer_bay_repo(root)
    runner.run(
        ["git", "clone", repo, str(bay_dir)],
        message=f"Cloning framework from {repo}",
    )

    # Checkout pinned version
    git.checkout(bay_dir, version)

    # Symlink group_vars
    link = bay_dir / "group_vars"
    link.unlink(missing_ok=True)
    os.symlink("../group_vars", link)

    _ensure_skill_link(bay_dir, root)

    # Sync deps
    ansible.sync_deps(bay_dir)

    console.console.print()
    console.success(f"Restored to {version}")


@app.command()
def status() -> None:
    """Show the pinned framework version, update status, and feature flags.

    Examples:

        bin/bay status
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    pinned = paths.read_pinned_version(root)
    checkout = git.current_ref(bay_dir)
    latest = git.latest_tag(bay_dir)

    console.show_banner(subtitle="Status")

    if pinned:
        console.console.print(f"  Version: [bold]{pinned}[/bold]")
    else:
        console.console.print("  Version: [dim]not set[/dim]")

    # Mismatch warning
    if pinned and checkout != pinned:
        console.warning(f"Checkout: {checkout} (MISMATCH — run 'bin/bay install')")

    # Update status
    if pinned and latest:
        if pinned == latest:
            console.success("Up to date")
        else:
            console.warning(f"Latest: {latest} — run 'bin/bay update'")

    # Feature summary
    _show_feature_summary(root)

    console.console.print()


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
