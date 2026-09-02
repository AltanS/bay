"""Pre-flight doctor check — validates project health before deploying."""

from __future__ import annotations

import re
import socket
import subprocess
from pathlib import Path

import typer
import yaml

from bay_cli import console, paths


def doctor(
    env: str = typer.Argument("production", help="Target environment to check."),
) -> None:
    """Run pre-flight checks on your project before deploying.

    Checks .vault_pass, the inventory, SSH connectivity (as root, then as
    admin_user), DNS resolution of your first service domain and the
    headscale domain, gateway configuration, and (when configured) GitHub
    webhook health. Exits 1 when issues are found.
    Complements `bin/bay validate`, which checks config files rather than
    the environment.

    Examples:

        bin/bay doctor
        bin/bay doctor testing
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    console.show_banner(subtitle="Doctor")

    issues = 0

    # ── Vault password ───────────────────────────────────────────────
    vault_pass = root / ".vault_pass"
    if vault_pass.exists():
        console.success("Vault password     .vault_pass exists")
    else:
        console.error("Vault password     .vault_pass missing — create it with: echo 'your-password' > .vault_pass")
        issues += 1

    # ── Inventory ────────────────────────────────────────────────────
    inventory_file = root / "hosts" / env
    hosts = _parse_inventory(inventory_file)
    if not inventory_file.exists():
        console.error(f"Inventory          hosts/{env} not found")
        issues += 1
    elif not hosts:
        console.error(f"Inventory          hosts/{env} has no host entries")
        issues += 1
    else:
        n = len(hosts)
        console.success(f"Inventory          {n} host{'s' if n != 1 else ''} in {env}")

    # ── SSH connectivity ─────────────────────────────────────────────
    if hosts:
        host = hosts[0]
        candidates = _ssh_users(root)
        connected_as, reason = _probe_ssh(host, candidates)
        if connected_as:
            console.success(f"SSH connectivity   connected to {connected_as}@{host}")
        else:
            attempted = ", ".join(f"{u}@{host}" for u in candidates)
            console.error(f"SSH connectivity   cannot reach {attempted} — {reason}")
            issues += 1
    else:
        console.warning("SSH connectivity   skipped (no hosts in inventory)")

    # ── Load config files ────────────────────────────────────────────
    access_gw_cfg = _load_yaml(root / "group_vars" / "all" / "access_gateway.yml")
    domains_cfg = _load_yaml(root / "group_vars" / env / "domains.yml")
    vpn_cfg = _load_yaml(root / "group_vars" / "all" / "vpn_access.yml")

    gateway_type = access_gw_cfg.get("access_gateway", "none") if access_gw_cfg else "none"
    headscale_domain = access_gw_cfg.get("headscale_domain") if access_gw_cfg else None
    headscale_control_region = access_gw_cfg.get("headscale_control_region") if access_gw_cfg else None
    domain_base = domains_cfg.get("domain_base") if domains_cfg else None

    # ── DNS: a name the wildcard record actually covers ──────────────
    # Never the bare apex: the wizard tells the operator to create
    # `*.<domain_base>`, and a wildcard does not cover the apex, so a
    # correctly configured zone used to report NXDOMAIN here.
    services_file = _services_file(root, env)
    probe_domain, probe_source = _dns_probe_target(services_file, domain_base)
    if probe_domain:
        try:
            resolved = _resolve_domain(probe_domain)
        except Exception as exc:  # a crashed probe is an error, never a skip
            console.error(f"DNS: {probe_domain:<14s} check failed to run ({exc})")
            issues += 1
        else:
            if resolved:
                console.success(f"DNS: {probe_domain:<14s} resolves to {resolved} ({probe_source})")
            else:
                hint = ""
                if hosts:
                    hint = f" — create a wildcard A record: *.{domain_base} -> {hosts[0]}"
                console.error(f"DNS: {probe_domain:<14s} NXDOMAIN{hint}")
                issues += 1
    else:
        console.info("DNS: main domain   skipped (no domain_base in config)")

    # ── DNS: headscale domain ────────────────────────────────────────
    if gateway_type == "headscale" and headscale_domain:
        resolved = _resolve_domain(headscale_domain)
        if resolved:
            console.success(f"DNS: {headscale_domain:<14s} resolves to {resolved}")
        else:
            hint = ""
            control_ip = _get_control_host_ip(inventory_file, headscale_control_region)
            target_ip = control_ip or (hosts[0] if hosts else None)
            if target_ip:
                hint = f" — create A record pointing to {target_ip}"
            console.error(f"DNS: {headscale_domain:<14s} NXDOMAIN{hint}")
            issues += 1

    # ── Gateway config ───────────────────────────────────────────────
    gw_issues = _check_gateway_config(gateway_type, headscale_domain, vpn_cfg)
    if gw_issues:
        for msg in gw_issues:
            console.error(f"Gateway config     {msg}")
            issues += 1
    else:
        if gateway_type == "headscale":
            console.success(f"Gateway config     headscale configured with domain {headscale_domain}")
        elif gateway_type == "wireguard":
            console.success("Gateway config     wireguard configured")
        else:
            console.success("Gateway config     no access gateway (all services public)")

    # ── Webhook health ───────────────────────────────────────────────
    if services_file is not None:
        try:
            import yaml as _yaml
            from bay_cli.commands.validate import (
                ValidationResult,
                _probe_webhook_health,
                _validate_yaml_files,
            )

            services_data = _yaml.safe_load(services_file.read_text()) or {}
            wh_result = ValidationResult()
            parsed_files = _validate_yaml_files(root, env, wh_result)
            _probe_webhook_health(root, env, services_data, parsed_files, wh_result)
            issues += wh_result.total_issues
        except Exception as _exc:
            # A probe that cannot run is an unknown, not a pass. Counting it
            # keeps `doctor` from printing "All checks passed" after a crash.
            console.error(f"Webhook health     check failed to run ({_exc})")
            issues += 1
    else:
        console.info("Webhook health     skipped (no services.yml)")

    # ── Summary ──────────────────────────────────────────────────────
    console.console.print()
    if issues:
        console.error(f"{issues} issue{'s' if issues != 1 else ''} found. Fix the above before deploying.")
        raise typer.Exit(code=1)
    else:
        console.success("All checks passed!")


# ── Helpers ──────────────────────────────────────────────────────────────


#: Fallback when group_vars/all/main.yml does not set admin_user. Matches the
#: value the wizard scaffolds (wizard/templates/main.yml.j2).
DEFAULT_ADMIN_USER = "bay-admin"


def _ssh_users(root: Path) -> list[str]:
    """Return the SSH users to try, in order: root, then the admin account.

    A fresh server only accepts ``root``; after ``bin/bay provision`` root
    login is disabled and only ``admin_user`` works. Probing with no user at
    all (the old behaviour) authenticates as the local account name, which
    fails on both sides of that transition.
    """
    main_cfg = _load_yaml(root / "group_vars" / "all" / "main.yml") or {}
    admin_user = main_cfg.get("admin_user") or DEFAULT_ADMIN_USER
    users = ["root"]
    if isinstance(admin_user, str) and admin_user.strip() and admin_user.strip() != "root":
        users.append(admin_user.strip())
    return users


def _probe_ssh(host: str, users: list[str]) -> tuple[str | None, str]:
    """Try each user in turn. Returns (user that connected, failure reason)."""
    reason = "connection failed"
    for user in users:
        try:
            proc = subprocess.run(
                [
                    "ssh",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=5",
                    "-o", "StrictHostKeyChecking=accept-new",
                    f"{user}@{host}",
                    "true",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            reason = "timeout"
            continue
        except FileNotFoundError:
            return None, "ssh command not found"
        if proc.returncode == 0:
            return user, ""
        if proc.stderr:
            reason = proc.stderr.strip().split("\n")[0]
    return None, reason


def _services_file(root: Path, env: str) -> Path | None:
    """Locate services.yml — the wizard writes ``group_vars/all/services.yml``.

    A per-environment file is honoured only when the ``all`` one is absent.
    """
    candidates = [
        root / "group_vars" / "all" / "services.yml",
        root / "group_vars" / env / "services.yml",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _first_service_domain(services_file: Path | None) -> str | None:
    """Return the first literal domain declared in services.yml, if any.

    Jinja-templated entries (``{{ domain_base }}``) are skipped — they cannot
    be resolved without running Ansible.
    """
    if services_file is None:
        return None
    data = _load_yaml(services_file)
    if not data:
        return None
    for section in ("services", "accessories"):
        entries = data.get(section)
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            domains = entry.get("domains")
            if isinstance(domains, str):
                domains = [domains]
            if not isinstance(domains, list):
                continue
            for domain in domains:
                if not isinstance(domain, str):
                    continue
                domain = domain.strip()
                if domain and "{{" not in domain:
                    return domain
    return None


def _dns_probe_target(services_file: Path | None, domain_base: str | None) -> tuple[str | None, str]:
    """Pick the name to resolve, plus a short label saying where it came from."""
    domain = _first_service_domain(services_file)
    if domain:
        return domain, "first service domain"
    if domain_base:
        return f"status.{domain_base}", "status subdomain"
    return None, ""


def _get_control_host_ip(inventory_file: Path, control_region: str | None = None) -> str | None:
    """Return the control region host IP for multi-region, or None for single-server.

    Uses explicit control_region when provided, falls back to first child group.
    Two-pass parsing so [production:children] can appear in any position.
    """
    if not inventory_file.exists():
        return None
    text = inventory_file.read_text()
    if "[production:children]" not in text:
        return None

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

    # Prefer explicit control_region
    if control_region and control_region in groups:
        return groups[control_region]

    # Fallback: first child group
    if children and children[0] in groups:
        return groups[children[0]]
    return None


def _parse_inventory(inventory_file: Path) -> list[str]:
    """Extract hosts from an INI-format Ansible inventory file.

    Returns IP addresses or hostnames, skipping section headers, comments,
    and blank lines.
    """
    if not inventory_file.exists():
        return []

    hosts: list[str] = []
    for line in inventory_file.read_text().splitlines():
        line = line.strip()
        # Skip empty lines, comments, and section headers
        if not line or line.startswith("#") or line.startswith("["):
            continue
        # Take the first token (host may have ansible vars after it)
        host = re.split(r"\s+", line)[0]
        if host:
            hosts.append(host)
    return hosts


def _load_yaml(path: Path) -> dict | None:
    """Load a YAML file, returning None if missing or unparseable."""
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def _resolve_domain(domain: str) -> str | None:
    """Resolve a domain to its first IP address, or None on failure."""
    try:
        results = socket.getaddrinfo(domain, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if results:
            return results[0][4][0]
    except (socket.gaierror, OSError):
        pass
    return None


def _check_gateway_config(
    gateway_type: str,
    headscale_domain: str | None,
    vpn_cfg: dict | None,
) -> list[str]:
    """Validate gateway-specific configuration, returning a list of issues."""
    issues: list[str] = []

    if gateway_type == "headscale":
        if not headscale_domain:
            issues.append("headscale gateway requires headscale_domain in access_gateway.yml")

    if gateway_type in ("headscale", "wireguard"):
        allowed = vpn_cfg.get("vpn_allowed_ips", []) if vpn_cfg else []
        if not allowed:
            issues.append("vpn_allowed_ips is empty in vpn_access.yml — add trusted IPs")

    return issues
