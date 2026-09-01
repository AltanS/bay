"""Scaffold generator — renders Jinja2 templates into a consumer project."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import jinja2
from ruamel.yaml import YAML

from bay_cli import console
from bay_cli.utils.secret_gen import generate_password
from bay_cli.wizard.models import SSHKey, WizardResult

# ── Template → output path mapping ──────────────────────────────────────

_TEMPLATES: dict[str, str] = {
    # group_vars/all/
    "main.yml.j2": "group_vars/all/main.yml",
    "services.yml.j2": "group_vars/all/services.yml",
    "users.yml.j2": "group_vars/all/users.yml",
    "security.yml.j2": "group_vars/all/security.yml",
    "registry.yml.j2": "group_vars/all/registry.yml",
    "vpn_access.yml.j2": "group_vars/all/vpn_access.yml",
    "access_gateway.yml.j2": "group_vars/all/access_gateway.yml",
    # group_vars/production/
    "production_main.yml.j2": "group_vars/production/main.yml",
    "production_domains.yml.j2": "group_vars/production/domains.yml",
    "production_secrets.yml.j2": "group_vars/production/secrets.yml",
    # Inventory
    "inventory.j2": "hosts/production",
    # Root files
    "ansible_cfg.j2": "ansible.cfg",
    "deploy.yml.j2": "deploy.yml",
    "provision.yml.j2": "provision.yml",
    "restore.yml.j2": "restore.yml",
    "webhook.yml.j2": "webhook.yml",
    "makefile.j2": "Makefile",
    "gitignore.j2": ".gitignore",
    "readme.md.j2": "README.md",
    # Tests
    "test_infra.sh.j2": "tests/test_infra.sh",
}


# Secret names the templates scaffold, per selected service. Every one of
# them gets a generated value — an empty secret is a failed `bay validate`
# and, for a database, a container that never comes up.
_SERVICE_SECRETS: dict[str, dict[str, int]] = {
    "postgres": {"POSTGRES_PASSWORD": 32},
    "mariadb": {"MARIADB_ROOT_PASSWORD": 32, "MARIADB_PASSWORD": 32},
    "vaultwarden": {"VAULTWARDEN_ADMIN_TOKEN": 48},
    "n8n": {"N8N_DB_POSTGRESDB_PASSWORD": 32},
    "plausible": {"PLAUSIBLE_SECRET_KEY_BASE": 64, "PLAUSIBLE_DB_PASSWORD": 32},
    "umami": {"UMAMI_APP_SECRET": 64, "UMAMI_DB_PASSWORD": 32},
}


def generated_secrets_for(selected_services: list[str]) -> dict[str, str]:
    """Mint one secret per scaffolded vault key for *selected_services*."""
    values: dict[str, str] = {}
    for service in selected_services:
        for key, length in _SERVICE_SECRETS.get(service, {}).items():
            values[key] = generate_password(length)
    return values


def _build_context(result: WizardResult) -> dict:
    """Build the Jinja2 template context from a WizardResult."""
    return {
        "project_name": result.project_name,
        "multi_region": result.multi_region,
        "server_ip": result.server_ip or "0.0.0.0",
        "regions": result.regions or [],
        "domain_base": result.domain_base,
        "letsencrypt_email": result.letsencrypt_email,
        "ssh_keys": result.ssh_keys,
        "access_gateway": result.access_gateway,
        "headscale_domain": result.headscale_domain or "",
        "vpn_enabled": result.vpn_enabled,
        "vpn_peer_ips": result.vpn_peer_ips,
        "selected_services": result.selected_services,
        "generated_secrets": generated_secrets_for(result.selected_services),
    }


_VAULT_HEADER = "$ANSIBLE_VAULT;"


def _is_vault_encrypted(path: Path) -> bool:
    """Check if a file is ansible-vault encrypted."""
    try:
        first_line = path.read_text(errors="replace").split("\n", 1)[0]
        return first_line.startswith(_VAULT_HEADER)
    except OSError:
        return False


def _log_skipped_summary(skipped: list[Path]) -> None:
    """Print a single summary line for skipped files, or nothing if none were skipped."""
    n = len(skipped)
    if n == 0:
        return
    if n <= 3:
        names = ", ".join(p.name for p in skipped)
        console.info(f"Skipped {n} existing files: {names}")
    else:
        console.info(f"Skipped {n} existing files")


def scaffold(result: WizardResult, target_dir: Path, *, force: bool = False) -> list[Path]:
    """Render all templates into *target_dir*, returning paths of created files.

    Skips existing files unless *force* is True.
    """
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("bay_cli.wizard", "templates"),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    ctx = _build_context(result)
    created: list[Path] = []
    skipped: list[Path] = []
    backed_up: list[Path] = []

    # Render the standard template set
    for template_name, output_rel in _TEMPLATES.items():
        output_path = target_dir / output_rel
        _render_one(env, template_name, ctx, output_path, created, skipped, backed_up, force=force)

    # Multi-region: render per-region group_vars/<region>/main.yml
    if result.multi_region and result.regions:
        for i, region in enumerate(result.regions):
            region_ctx = {**ctx, "region": region, "is_control_region": i == 0}
            output_path = target_dir / f"group_vars/{region.name}/main.yml"
            _render_one(env, "region_main.yml.j2", region_ctx, output_path, created, skipped, backed_up, force=force)

    created.extend(_copy_catalog_files(result.selected_services, target_dir))

    _log_skipped_summary(skipped)
    if backed_up:
        console.warning(f"Backed up {len(backed_up)} changed file(s):")
        for p in backed_up:
            rel = p.relative_to(target_dir) if p.is_relative_to(target_dir) else p
            console.console.print(f"  [dim]{rel} → {rel}.bak[/dim]")

    # Make test script executable
    test_script = target_dir / "tests/test_infra.sh"
    if test_script.exists():
        os.chmod(test_script, 0o755)

    return created


def _copy_catalog_files(selected_services: list[str], target_dir: Path) -> list[Path]:
    """Copy each selected service's catalog ``files/`` tree into the consumer.

    A service that declares ``config_files`` cannot deploy without them —
    the deploy_stack role copies every entry to the host and fails on a
    missing one. ``bin/bay service add`` has always done this; scaffolding
    did not, which is why the default (Gatus) project failed its first
    deploy. Both paths share the one helper in commands/service.py.
    """
    from bay_cli.catalog import _package_framework_root, load_catalog
    from bay_cli.commands.service import _copy_config_files

    try:
        catalog = load_catalog(_package_framework_root(), target_dir)
    except Exception as e:  # a broken catalog must not abort scaffolding
        console.warning(f"Could not read the service catalog: {e}")
        return []

    copied: list[Path] = []
    for service_id in selected_services:
        entry = catalog.get(service_id)
        if entry is None:
            continue
        for rel in _copy_config_files(entry, target_dir):
            path = target_dir / rel
            copied.append(path)
            console.success(f"created {path}")
    return copied


def fill_example_gaps(target_dir: Path, ssh_keys: list[SSHKey]) -> None:
    """Finish the ``--no-interactive`` example copy so it can actually deploy.

    ``example/`` is copied verbatim, so the two values that cannot be
    shipped in a public repo — the operator's SSH key and real secrets —
    arrive empty. Filling them here keeps the example tree free of fake
    credentials while still producing a consumer that passes validate.
    """
    _write_admin_keys(target_dir / "group_vars" / "all" / "users.yml", ssh_keys)
    for secrets_file in sorted((target_dir / "group_vars").glob("*/secrets.yml")):
        _fill_empty_secrets(secrets_file)


def _write_admin_keys(users_file: Path, ssh_keys: list[SSHKey]) -> None:
    """Put *ssh_keys* on every user in the ``ssh-access`` group."""
    if not users_file.is_file() or not ssh_keys:
        return
    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(users_file.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        return

    changed = False
    for user in data["users"]:
        if not isinstance(user, dict):
            continue
        if "ssh-access" not in (user.get("groups") or []):
            continue
        if user.get("keys"):
            continue
        user["keys"] = [key.public_key for key in ssh_keys]
        changed = True

    if changed:
        with users_file.open("w") as f:
            yaml.dump(data, f)
        console.success(f"added {len(ssh_keys)} SSH key(s) to {users_file}")


def _fill_empty_secrets(secrets_file: Path) -> None:
    """Replace every empty value under ``secrets:`` with a generated one."""
    if not secrets_file.is_file():
        return
    if _is_vault_encrypted(secrets_file):
        return
    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(secrets_file.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("secrets"), dict):
        return

    filled = 0
    for key, value in data["secrets"].items():
        if value is None or (isinstance(value, str) and not value.strip()):
            data["secrets"][key] = generate_password(32)
            filled += 1

    if filled:
        with secrets_file.open("w") as f:
            yaml.dump(data, f)
        secrets_file.chmod(0o600)
        console.success(f"generated {filled} secret(s) in {secrets_file}")


def _render_one(
    env: jinja2.Environment,
    template_name: str,
    ctx: dict,
    output_path: Path,
    created: list[Path],
    skipped: list[Path],
    backed_up: list[Path],
    *,
    force: bool = False,
) -> None:
    """Render a single template to *output_path*, appending to *created* on success.

    In force (edit) mode:
    - Vault-encrypted files are never overwritten
    - Files with identical content are left unchanged
    - Files with different content get a .bak backup before overwriting
    """
    template = env.get_template(template_name)
    content = template.render(ctx)

    if output_path.exists():
        if not force:
            skipped.append(output_path)
            return

        # Never overwrite vault-encrypted files
        if _is_vault_encrypted(output_path):
            console.info(f"skipped vault-encrypted {output_path.name}")
            skipped.append(output_path)
            return

        # Skip if content is identical
        existing_content = output_path.read_text()
        if existing_content == content:
            skipped.append(output_path)
            return

        # Content differs — back up before overwriting
        bak = output_path.with_suffix(output_path.suffix + ".bak")
        shutil.copy2(output_path, bak)
        backed_up.append(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)
    created.append(output_path)
    console.success(f"created {output_path}")


def copy_examples(bay_dir: Path, target_dir: Path, *, force: bool = False) -> None:
    """Copy example files from the framework into *target_dir*.

    This is the ``--no-interactive`` backward-compatible path — it copies
    the static example files without any parameterization.  Files that
    already exist in *target_dir* are skipped unless *force* is True.
    """
    example_dir = bay_dir / "example"
    if not example_dir.is_dir():
        console.warning(f"example directory not found: {example_dir}")
        return

    skipped: list[Path] = []

    for root, _dirs, files in os.walk(example_dir):
        root_path = Path(root)
        for fname in files:
            src = root_path / fname
            rel = src.relative_to(example_dir)
            dst = target_dir / rel

            if dst.exists() and not force:
                skipped.append(dst)
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            console.success(f"created {dst}")

    _log_skipped_summary(skipped)

    # Make test script executable if it was copied
    test_script = target_dir / "tests/test_infra.sh"
    if test_script.exists():
        os.chmod(test_script, 0o755)
