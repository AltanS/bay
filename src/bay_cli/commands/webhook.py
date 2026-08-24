"""Webhook setup command."""

import re
from pathlib import Path

import typer

from bay_cli import ansible, console, guards, paths, runner
from bay_cli.config import StackConfig
from bay_cli.utils.ephemeral import show_ephemeral


def webhook(
    env: str = typer.Argument(..., help="Target environment (e.g., production)."),
    keys_only: bool = typer.Option(
        False, "--keys-only", help="Only display deploy keys, skip deployment."
    ),
) -> None:
    """Deploy webhook infrastructure and show GitHub setup instructions.

    Fetches per-service deploy keys from the server and displays them on
    an ephemeral screen. Re-run with --keys-only to view the keys again
    without redeploying anything.

    Examples:

        bin/bay webhook production
        bin/bay webhook production --keys-only
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    guards.check_bay_version(bay_dir, root)

    if not keys_only:
        console.header("Deploying webhook infrastructure")
        ansible.run_playbook(
            "webhook",
            env,
            bay_dir=bay_dir,
            tags=["git_deploy"],
        )
        console.success("Webhook infrastructure deployed")

    _show_setup_instructions(bay_dir, root, env)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _fetch_deploy_key(
    env: str, service_name: str, bay_dir: Path
) -> str | None:
    """Fetch deploy key from remote server via ansible ad-hoc."""
    key_path = f"/opt/bay/builds/{service_name}/.deploy_key.pub"
    uv_cmd = ansible._uv_run_cmd(bay_dir)
    cmd = [
        *uv_cmd,
        "ansible", env,
        "-m", "ansible.builtin.shell",
        "-a", f"cat {key_path}",
        "--become",
    ]
    try:
        result = runner.run(
            cmd,
            message=f"Fetching deploy key for {service_name}...",
            env=ansible._collections_env(bay_dir),
            check=False,
        )
        if result.returncode != 0:
            return None
        # Parse ansible output: "hostname | SUCCESS | rc=0 >>\n<key>"
        clean = _ANSI_RE.sub("", result.stdout or "")
        marker = ">>"
        idx = clean.find(marker)
        if idx == -1:
            return None
        key = clean[idx + len(marker):].strip()
        # Multi-host: take the first key (they're identical)
        if ">>" in key:
            key = key[:key.find(">>")].strip()
            # Remove any trailing hostname line
            lines = key.splitlines()
            key = lines[0].strip() if lines else key
        return key if key.startswith("ssh-") else None
    except Exception:
        return None


def _show_setup_instructions(bay_dir: Path, root: Path, env: str) -> None:
    """Fetch deploy keys, show them ephemerally, then print setup instructions."""
    config = StackConfig(root)
    services = config.get_services()
    data = config.load_services()
    webhook_config = data.get("webhook", {}) if isinstance(data, dict) else {}

    build_services = {
        name: svc for name, svc in services.items() if "build" in svc
    }

    if not build_services:
        console.info("No services with build: blocks found")
        return

    # Resolve webhook domain(s) — replace {{ domain_base }} with actual values
    webhook_domain_tpl = str(webhook_config.get("domain", "<webhook-domain>"))
    domain_bases = config.get_domain_bases()
    is_multi_region = config.is_multi_region(env)

    if is_multi_region and domain_bases and "{{" in webhook_domain_tpl:
        # Multi-region: resolve per region
        webhook_domains = {
            region: re.sub(r"\{\{\s*domain_base\s*\}\}", base, webhook_domain_tpl)
            for region, base in domain_bases.items()
        }
    else:
        # Single-region: resolve once
        domain_base = config.get_domain_base(env)
        resolved = webhook_domain_tpl
        if domain_base and "{{" in resolved:
            resolved = re.sub(r"\{\{\s*domain_base\s*\}\}", domain_base, resolved)
        webhook_domains = {"": resolved}

    # Fetch deploy keys from remote servers
    deploy_keys: dict[str, str] = {}
    for name, svc in build_services.items():
        build = svc.get("build", {})
        if build.get("token"):
            continue  # Token-based auth, no deploy key
        key = _fetch_deploy_key(env, name, bay_dir)
        if key:
            deploy_keys[name] = key

    # Show deploy keys in ephemeral screen
    if deploy_keys:
        key_content = "\n[bold]Deploy Keys[/bold]\n"
        for name, key in deploy_keys.items():
            key_content += (
                f"\n  [dim]{name}:[/dim]"
                f"\n  {key}"
                "\n"
            )
        key_content += "\n  Add each key to the repo's Settings > Deploy Keys"
        key_content += "\n  (read-only access is sufficient)\n"

        clipboard_text = "\n".join(
            f"# {name}\n{key}" for name, key in deploy_keys.items()
        )
        show_ephemeral(key_content, clipboard=clipboard_text)

    # Print setup instructions (visible in terminal history)
    console.header("GitHub Deploy Setup")
    console.console.print()

    for name, svc in build_services.items():
        build = svc.get("build", {})
        repo = build.get("repo", "<repo-url>")
        branch = build.get("branch", "main")

        console.console.print(f"  [bold]Service: {name}[/bold]")
        console.console.print(f"    Repo:   {repo}")
        console.console.print(f"    Branch: {branch}")
        console.console.print()

        needs_deploy_key = not build.get("token")
        step = 1

        if needs_deploy_key:
            if name in deploy_keys:
                console.console.print(f"    [bold]{step}.[/bold] Add deploy key to repo Settings > Deploy Keys")
                console.console.print("       (shown on previous screen — re-run with --keys-only to view again)")
            else:
                console.console.print(f"    [bold]{step}.[/bold] Add deploy key to repo Settings > Deploy Keys:")
                console.console.print(f"       Run on server: cat /opt/bay/builds/{name}/.deploy_key.pub")
            console.console.print()
            step += 1

        # Filter webhook URLs to regions where this service is active
        svc_regions = svc.get("regions", [])
        if svc_regions and len(webhook_domains) > 1:
            svc_webhook_domains = {
                r: d for r, d in webhook_domains.items() if r in svc_regions
            }
        else:
            svc_webhook_domains = webhook_domains

        console.console.print(f"    [bold]{step}.[/bold] Add webhook to repo Settings > Webhooks:")
        if len(svc_webhook_domains) > 1:
            for region, domain in svc_webhook_domains.items():
                console.console.print(f"       [{region}] URL:  https://{domain}/webhook/{name}")
        else:
            domain = next(iter(svc_webhook_domains.values()))
            console.console.print(f"       URL:          https://{domain}/webhook/{name}")
        console.console.print(f"       Secret:       (from vault — webhook.secret in services.yml)")
        console.console.print(f"       Content type: application/json")
        console.console.print(f"       Events:       Just the push event")
        console.console.print()
