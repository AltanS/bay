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

    Checks .vault_pass, the inventory, SSH connectivity, DNS resolution of
    the base and headscale domains, gateway configuration, and (when
    configured) GitHub webhook health. Exits 1 when issues are found.
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
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=5",
                    "-o", "StrictHostKeyChecking=accept-new",
                    host,
                    "true",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                console.success(f"SSH connectivity   connected to {host}")
            else:
                stderr = result.stderr.strip().split("\n")[0] if result.stderr else "connection failed"
                console.error(f"SSH connectivity   cannot reach {host} — {stderr}")
                issues += 1
        except subprocess.TimeoutExpired:
            console.error(f"SSH connectivity   timeout connecting to {host}")
            issues += 1
        except FileNotFoundError:
            console.error("SSH connectivity   ssh command not found")
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

    # ── DNS: main domain ─────────────────────────────────────────────
    if domain_base:
        resolved = _resolve_domain(domain_base)
        if resolved:
            console.success(f"DNS: {domain_base:<14s} resolves to {resolved}")
        else:
            hint = ""
            if hosts:
                hint = f" — create A record pointing to {hosts[0]}"
            console.error(f"DNS: {domain_base:<14s} NXDOMAIN{hint}")
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
    services_file = root / "group_vars" / env / "services.yml"
    if services_file.exists():
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
        except Exception as _exc:  # pragma: no cover
            console.warning(f"Webhook health     check skipped ({_exc})")
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
