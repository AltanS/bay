"""Scaffold generator — renders Jinja2 templates into a consumer project."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import jinja2

from bay_cli import console
from bay_cli.wizard.models import WizardResult

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
