"""Data models and validators for the onboarding wizard."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from bay_cli.errors import BayError

# ── Validators ───────────────────────────────────────────────────────────

_PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_REGION_NAME_RE = re.compile(r"^[a-z][a-z0-9]*$")
_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$")


def validate_project_name(name: str) -> str:
    """Validate project name (DNS-safe: lowercase alphanumeric + hyphens)."""
    name = name.strip()
    if not name:
        raise BayError("Project name cannot be empty")
    if len(name) > 63:
        raise BayError("Project name must be 63 characters or fewer")
    if not _PROJECT_NAME_RE.match(name):
        raise BayError(
            f"Invalid project name '{name}' — must start with a letter, "
            "use only lowercase letters, numbers, and hyphens"
        )
    return name


def validate_ip(value: str) -> str:
    """Validate IPv4/IPv6 address or hostname."""
    value = value.strip()
    if not value:
        raise BayError("Server address cannot be empty")
    # Try as IP address first
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    # Fall back to hostname validation
    if _HOSTNAME_RE.match(value) and "." in value:
        return value
    raise BayError(
        f"Invalid address '{value}' — provide an IPv4/IPv6 address or hostname"
    )


def validate_domain(domain: str) -> str:
    """Validate domain name (at least one dot, no protocol prefix)."""
    domain = domain.strip().lower()
    if not domain:
        raise BayError("Domain cannot be empty")
    if "://" in domain:
        raise BayError(f"Domain should not include protocol — use '{domain.split('://', 1)[1]}'")
    if " " in domain:
        raise BayError("Domain cannot contain spaces")
    if "." not in domain:
        raise BayError(f"Invalid domain '{domain}' — must contain at least one dot (e.g., example.com)")
    return domain


def validate_region_name(name: str) -> str:
    """Validate region name (lowercase alpha + numbers, starts with letter)."""
    name = name.strip().lower()
    if not name:
        raise BayError("Region name cannot be empty")
    if not _REGION_NAME_RE.match(name):
        raise BayError(
            f"Invalid region name '{name}' — use only lowercase letters and numbers, "
            "starting with a letter (e.g., eu, na, ap)"
        )
    return name


# ── Data Models ──────────────────────────────────────────────────────────


@dataclass
class SSHKey:
    """An SSH public key collected during the wizard."""

    username: str
    public_key: str
    source: Literal["github", "manual"]


_SSH_KEY_PREFIXES = ("ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-")


def parse_ssh_public_key(text: str, *, fallback_username: str = "admin") -> SSHKey:
    """Parse one OpenSSH public key line into an :class:`SSHKey`.

    Raises BayError when the line is not a public key — a private key
    pasted here would be both useless and a leak.
    """
    line = text.strip()
    if not line or not line.startswith(_SSH_KEY_PREFIXES):
        raise BayError(
            f"Not an SSH public key: {line[:40]!r} — expected a line starting "
            "with ssh-ed25519, ssh-rsa or ecdsa-… (the contents of a .pub file)"
        )
    parts = line.split()
    comment = parts[2] if len(parts) > 2 else ""
    username = comment.split("@", 1)[0] if comment else fallback_username
    return SSHKey(username=username or fallback_username, public_key=line, source="manual")


def read_ssh_key_file(path: Path) -> list[SSHKey]:
    """Read every public key from *path* (a ``.pub`` file)."""
    try:
        text = path.read_text()
    except OSError as e:
        raise BayError(f"Cannot read SSH key file {path}: {e}") from e
    keys = [
        parse_ssh_public_key(line, fallback_username=path.stem)
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not keys:
        raise BayError(f"No SSH public key found in {path}")
    return keys


def discover_local_ssh_keys() -> list[SSHKey]:
    """Return the public keys found in ``~/.ssh/*.pub``, ignoring bad ones."""
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.is_dir():
        return []
    found: list[SSHKey] = []
    for pub in sorted(ssh_dir.glob("*.pub")):
        try:
            found.extend(read_ssh_key_file(pub))
        except BayError:
            continue
    return found


def resolve_ssh_keys(
    inline_keys: list[str] | None = None,
    key_files: list[str] | None = None,
) -> list[SSHKey]:
    """Resolve the admin account's SSH keys for a non-interactive setup.

    Order: ``--ssh-key`` values, then ``--ssh-key-file`` files, then
    ``~/.ssh/*.pub``. Refuses to return an empty list: provisioning
    disables root login and password authentication, so an admin account
    with no key locks you out of the server you just paid for.
    """
    keys: list[SSHKey] = []
    for raw in inline_keys or []:
        keys.append(parse_ssh_public_key(raw))
    for raw_path in key_files or []:
        keys.extend(read_ssh_key_file(Path(raw_path).expanduser()))
    if keys:
        return keys

    keys = discover_local_ssh_keys()
    if keys:
        return keys

    raise BayError(
        "No SSH public key found for the admin account.\n"
        "  Provisioning disables root login and password authentication, so a "
        "keyless admin locks you out of your own server.\n"
        "  Pass one with --ssh-key 'ssh-ed25519 AAAA…' or "
        "--ssh-key-file ~/.ssh/id_ed25519.pub,\n"
        "  or create a key first: ssh-keygen -t ed25519"
    )


@dataclass
class RegionConfig:
    """A deployment region with name and server address."""

    name: str
    server_ip: str


@dataclass
class WizardResult:
    """All values collected by the onboarding wizard."""

    project_name: str
    multi_region: bool
    # Single-server fields
    server_ip: str | None = None
    # Multi-region fields
    regions: list[RegionConfig] | None = None
    # Common fields
    domain_base: str = ""
    letsencrypt_email: str = ""
    ssh_keys: list[SSHKey] = field(default_factory=list)
    # Access gateway
    access_gateway: Literal["headscale", "wireguard", "none"] = "headscale"
    headscale_domain: str | None = None
    # Legacy VPN fields (kept for template compat, derived from access_gateway)
    vpn_enabled: bool = False
    vpn_peer_ips: list[str] = field(default_factory=list)
    selected_services: list[str] = field(default_factory=lambda: ["gatus"])

    def __post_init__(self) -> None:
        """Derive vpn_enabled and validate gateway-specific fields."""
        self.vpn_enabled = self.access_gateway != "none"
        if self.access_gateway == "headscale" and not self.headscale_domain:
            raise BayError("headscale_domain is required when access_gateway is headscale")


def load_existing_config(root: Path) -> WizardResult | None:
    """Load an existing scaffolded project's config into a WizardResult.

    Returns None if the project doesn't appear to be scaffolded.
    """
    main_file = root / "group_vars" / "all" / "main.yml"
    if not main_file.exists():
        return None

    try:
        main_data = yaml.safe_load(main_file.read_text()) or {}
    except (yaml.YAMLError, OSError):
        return None

    project_name = main_data.get("stack_name", root.name)

    # Detect multi-region from inventory
    inventory = root / "hosts" / "production"
    multi_region = False
    server_ip: str | None = None
    regions: list[RegionConfig] | None = None

    if inventory.exists():
        inv_text = inventory.read_text()
        if "[production:children]" in inv_text:
            multi_region = True
            regions = _parse_regions_from_inventory(inv_text)
        else:
            for line in inv_text.splitlines():
                line = line.strip()
                if line and not line.startswith("[") and not line.startswith("#"):
                    server_ip = line.split()[0]
                    break

    # Domain
    domains_file = root / "group_vars" / "production" / "domains.yml"
    domain_base = ""
    letsencrypt_email = ""
    if domains_file.exists():
        try:
            dom_data = yaml.safe_load(domains_file.read_text()) or {}
            domain_base = dom_data.get("domain_base", "")
            letsencrypt_email = dom_data.get("letsencrypt_email", "")
        except (yaml.YAMLError, OSError):
            pass

    # Access gateway
    gw_file = root / "group_vars" / "all" / "access_gateway.yml"
    access_gateway: Literal["headscale", "wireguard", "none"] = "none"
    headscale_domain: str | None = None
    if gw_file.exists():
        try:
            gw_data = yaml.safe_load(gw_file.read_text()) or {}
            gw_val = gw_data.get("access_gateway", "none")
            if gw_val in ("headscale", "wireguard", "none"):
                access_gateway = gw_val
            headscale_domain = gw_data.get("headscale_domain")
        except (yaml.YAMLError, OSError):
            pass

    # VPN peer IPs
    vpn_file = root / "group_vars" / "all" / "vpn_access.yml"
    vpn_peer_ips: list[str] = []
    if vpn_file.exists() and access_gateway == "wireguard":
        try:
            vpn_data = yaml.safe_load(vpn_file.read_text()) or {}
            ips = vpn_data.get("vpn_allowed_ips", [])
            if isinstance(ips, list):
                vpn_peer_ips = [str(ip) for ip in ips]
        except (yaml.YAMLError, OSError):
            pass

    # Services
    svc_file = root / "group_vars" / "all" / "services.yml"
    selected_services: list[str] = []
    if svc_file.exists():
        try:
            svc_data = yaml.safe_load(svc_file.read_text()) or {}
            services = svc_data.get("services")
            if isinstance(services, dict) and services:
                selected_services.extend(services.keys())
            accessories = svc_data.get("accessories")
            if isinstance(accessories, dict) and accessories:
                selected_services.extend(accessories.keys())
        except (yaml.YAMLError, OSError):
            pass

    return WizardResult(
        project_name=project_name,
        multi_region=multi_region,
        server_ip=server_ip,
        regions=regions,
        domain_base=domain_base,
        letsencrypt_email=letsencrypt_email,
        ssh_keys=[],
        access_gateway=access_gateway,
        headscale_domain=headscale_domain,
        vpn_peer_ips=vpn_peer_ips,
        selected_services=selected_services,
    )


def _parse_regions_from_inventory(text: str) -> list[RegionConfig]:
    """Parse region names and IPs from a multi-region INI inventory."""
    regions: list[RegionConfig] = []
    current_group: str | None = None
    children_groups: list[str] = []

    # First pass: find the children groups
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
                children_groups.append(line)

    # Second pass: get IPs for each child group
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1].split(":")[0]
            continue
        if current_group in children_groups and line and not line.startswith("#"):
            ip = line.split()[0]
            regions.append(RegionConfig(name=current_group, server_ip=ip))

    return regions


def defaults_result(directory_name: str) -> WizardResult:
    """Return a WizardResult with sensible defaults for --defaults mode."""
    # Sanitize directory name to a valid project name
    name = re.sub(r"[^a-z0-9-]", "-", directory_name.lower()).strip("-") or "my-project"
    if not name[0].isalpha():
        name = f"p-{name}"

    return WizardResult(
        project_name=name,
        multi_region=False,
        server_ip="0.0.0.0",
        domain_base="example.com",
        letsencrypt_email="admin@example.com",
        ssh_keys=[],
        # `none` keeps the first deploy to one DNS record and no client
        # install. Add a gateway later: bin/bay setup --gateway headscale
        access_gateway="none",
        headscale_domain=None,
        vpn_peer_ips=[],
        selected_services=["gatus"],
    )
