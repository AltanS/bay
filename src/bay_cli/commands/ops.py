"""Operational commands: deploy, provision, restore, logs, restart."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.panel import Panel

from bay_cli import ansible, console, guards, paths, runner
from bay_cli.errors import BayError

# Re-export for clarity in the scrub helpers below — imported as
# `shlex_quote` locally so the intent (shell-quoting a user-supplied
# value before it crosses an ansible ad-hoc boundary) is explicit.
shlex_quote = shlex.quote


def _rescue_interspersed_args(
    ctx: typer.Context,
    *,
    rig: bool = False,
    tags: Optional[str] = None,
    skip_validate: bool = False,
    region: Optional[str] = None,
) -> tuple[bool, Optional[str], bool, Optional[str]]:
    """Rescue known flags swallowed by allow_interspersed_args=False.

    When a positional argument (env) is consumed first, Typer/Click stops
    parsing options and dumps remaining tokens into ctx.args.  This scans
    ctx.args for known flags, promotes them, and removes them so they are
    not double-passed to ansible-playbook.

    It also drops a bare `--`. Click stops option parsing at the positional
    `env`, so the separator itself survives into ctx.args and used to be
    forwarded verbatim — ansible-playbook rejects it ("unrecognized
    arguments: --") and exits 2. That broke the dry-run form printed in both
    `provision --help` and `restore --help`; only `deploy` filtered it, and
    only by luck of a local list. Stripping it here fixes all three callers at
    the one point they share.
    """
    if not ctx.args:
        return rig, tags, skip_validate, region

    remaining: list[str] = []
    i = 0
    args = ctx.args

    while i < len(args):
        token = args[i]

        if token == "--":
            # Separator, not an argument. `-e x=--` and friends are untouched:
            # only a BARE `--` is dropped.
            i += 1
        elif token == "--rig" and not rig:
            rig = True
            console.warning("--rig placed after environment — rescued")
            i += 1
        elif token == "--skip-validate" and not skip_validate:
            skip_validate = True
            console.warning("--skip-validate placed after environment — rescued")
            i += 1
        elif token in ("--tags", "-t") and tags is None and i + 1 < len(args):
            tags = args[i + 1]
            console.warning(f"{token} {tags} placed after environment — rescued")
            i += 2
        elif token.startswith("--tags=") and tags is None:
            tags = token.split("=", 1)[1]
            console.warning(f"--tags={tags} placed after environment — rescued")
            i += 1
        elif token in ("--region", "-r") and region is None and i + 1 < len(args):
            region = args[i + 1]
            console.warning(f"{token} {region} placed after environment — rescued")
            i += 2
        elif token.startswith("--region=") and region is None:
            region = token.split("=", 1)[1]
            console.warning(f"--region={region} placed after environment — rescued")
            i += 1
        else:
            remaining.append(token)
            i += 1

    ctx.args = remaining
    return rig, tags, skip_validate, region


def _validate_env(env: str, root: Path) -> None:
    """Validate that the target environment or group exists.

    Accepts either an inventory filename (hosts/production) or a group name
    within a multi-region inventory (eu, na — groups inside hosts/production).
    """
    # Direct inventory file match (e.g., "production" → hosts/production)
    if (root / "hosts" / env).exists():
        return

    # Check if env is a group inside any inventory file (multi-region)
    hosts_dir = root / "hosts"
    if hosts_dir.is_dir():
        for inv_file in sorted(hosts_dir.iterdir()):
            if not inv_file.is_file():
                continue
            try:
                content = inv_file.read_text()
            except OSError:
                continue
            if f"[{env}]" in content:
                return

    available = sorted(p.name for p in hosts_dir.iterdir() if p.is_file()) if hosts_dir.is_dir() else []
    msg = f"Unknown environment '{env}' — no inventory file at hosts/{env}"
    if available:
        msg += f"\n  Available: {', '.join(available)}"
    raise BayError(msg)


def _run_playbook(
    playbook: str,
    env: str,
    tags: Optional[str],
    extra_args: list[str],
    *,
    skip_git_health: bool = False,
    profile: bool = False,
) -> None:
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    _validate_env(env, root)
    guards.check_bay_version(bay_dir, root)
    if not skip_git_health:
        guards.check_git_health(bay_dir)

    tag_list = [t.strip() for t in tags.split(",")] if tags else None

    ansible.run_playbook(
        playbook,
        env,
        bay_dir=bay_dir,
        tags=tag_list,
        extra_args=extra_args or None,
        profile=profile,
    )

    guards.show_update_notice(bay_dir, root)


def _show_headscale_onboarding(root: Path, bay_dir: Path) -> None:
    """Print first-deploy onboarding steps for headscale gateway."""
    marker = root / ".first_deploy_done"
    if marker.exists():
        return

    # Read access gateway config
    gw_file = root / "group_vars" / "all" / "access_gateway.yml"
    if not gw_file.exists():
        return
    try:
        data = yaml.safe_load(gw_file.read_text())
    except (yaml.YAMLError, OSError):
        return
    if not isinstance(data, dict) or data.get("access_gateway") != "headscale":
        return

    hs_domain = data.get("headscale_domain", "hs.example.com")

    steps = (
        f"[bold]Headscale gateway deployed![/bold]\n"
        f"\n"
        f"  Quick-start: enroll your first device\n"
        f"\n"
        f"  1. Enroll a device:     [dim]bin/bay gateway enroll[/dim]\n"
        f"  2. On your device:      [dim]tailscale up --login-server=https://{hs_domain} --authkey=KEY[/dim]\n"
        f"\n"
        f"  Or step by step:\n"
        f"  1. Create a user:       [dim]bin/bay gateway add-user alice[/dim]\n"
        f"  2. Generate auth key:   [dim]bin/bay gateway key alice[/dim]\n"
        f"  3. On your device:      [dim]tailscale up --login-server=https://{hs_domain} --authkey=KEY[/dim]\n"
        f"\n"
        f"  Manage nodes / users / keys (no admin UI):\n"
        f"  • [dim]bin/bay gateway nodes[/dim]   [dim]bin/bay gateway users[/dim]\n"
        f"\n"
        f"  Your VPN-protected services will be accessible once your device joins the tailnet."
    )

    console.console.print()
    console.console.print(Panel(steps, title="[bold]Headscale Setup[/bold]", expand=False))
    console.console.print()

    # Create marker to prevent repeat display
    try:
        marker.touch()
    except OSError:
        pass


def _read_stack_name(root: Path) -> str:
    """Read stack_name from consumer group_vars/all/main.yml."""
    main_yml = root / "group_vars" / "all" / "main.yml"
    if not main_yml.is_file():
        raise BayError("group_vars/all/main.yml not found")
    data = yaml.safe_load(main_yml.read_text())
    name = data.get("stack_name") if isinstance(data, dict) else None
    if not name:
        raise BayError("stack_name not defined in group_vars/all/main.yml")
    return str(name)


def _read_admin_user(root: Path) -> str:
    """Read admin_user from consumer group_vars/all/main.yml.

    This is the privileged unix account the framework provisions
    (`roles/users` writes its sudoers entry from the same var), so the CLI
    must not second-guess it. `admin-shell` used to hard-code the legacy-argo
    account name,
    which meant a new consumer following `example/group_vars` — where the
    account is `bay-admin` — got an SSH session to an account that does not
    exist, with no hint as to why.

    The fallback stays the legacy-argo account rather than the example's
    `bay-admin`:
    every existing consumer predates the rename, and a fallback that changed
    the account out from under them would be a worse bug than the one being
    fixed.
    """
    main_yml = root / "group_vars" / "all" / "main.yml"
    if main_yml.is_file():
        data = yaml.safe_load(main_yml.read_text())
        if isinstance(data, dict):
            user = data.get("admin_user")
            if user:
                return str(user)
    return "argo-admin"  # legacy-argo: pre-1.0 default account on existing hosts


_RIG_CACHE_MAX_AGE = 3600  # 1 hour


def _consumer_ref(root: Path) -> str:
    """A ref that changes only when rig-relevant consumer config changes.

    Uses the last commit touching group_vars/hosts/files rather than the whole
    HEAD, so unrelated commits (docs, CI, app changes) don't force a full infra
    rerun. Over-triggers on any config change (safe — the rig is idempotent);
    never under-triggers (a stale skip is caught by the version/ref cache key)."""
    for git_args in (
        ["log", "-1", "--format=%h", "--", "group_vars", "hosts", "files"],
        ["rev-parse", "--short", "HEAD"],
    ):
        try:
            result = subprocess.run(
                ["git", *git_args], cwd=root, capture_output=True, text=True, check=True
            )
            ref = result.stdout.strip()
            if ref:
                return ref
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    return "unknown"


def _read_rig_cache(bay_dir: Path, *, version: str, consumer_ref: str) -> bool | None:
    """Cached rig decision, or None if stale/missing/computed-for-other-inputs.

    The cache is trusted only when it was computed for the SAME framework
    version and consumer_ref — so a consumer change or framework bump is a cache
    miss (forcing a fresh check), never a stale skip of a needed rig."""
    from datetime import datetime, timezone

    cache_file = bay_dir / ".rig-state-cache"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        if data.get("version") != version or data.get("consumer_ref") != consumer_ref:
            return None  # computed for different inputs — re-check
        checked_at = datetime.fromisoformat(data["checked_at"])
        age = (datetime.now(timezone.utc) - checked_at).total_seconds()
        if age > _RIG_CACHE_MAX_AGE:
            return None
        return data["rig_needed"]
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def _write_rig_cache(bay_dir: Path, rig_needed: bool, *, version: str, consumer_ref: str) -> None:
    """Write rig state to cache, stamped with the inputs it was computed for."""
    from datetime import datetime, timezone

    cache_file = bay_dir / ".rig-state-cache"
    try:
        cache_file.write_text(json.dumps({
            "rig_needed": rig_needed,
            "version": version,
            "consumer_ref": consumer_ref,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }))
    except OSError:
        pass  # non-fatal


def _invalidate_rig_cache(bay_dir: Path) -> None:
    """Drop the cache so the next deploy re-checks (after a partial/tag deploy)."""
    try:
        (bay_dir / ".rig-state-cache").unlink(missing_ok=True)
    except OSError:
        pass


def _record_rig_matched(bay_dir: Path, root: Path) -> None:
    """After a successful full/skip deploy the server matches the current inputs,
    so cache rig_needed=False — the next deploy correctly skips infra."""
    version = paths.read_installed_version(bay_dir) or "unknown"
    _write_rig_cache(bay_dir, False, version=version, consumer_ref=_consumer_ref(root))


def _check_rig_state(env: str, bay_dir: Path, root: Path) -> bool:
    """Check server rig state. Returns True if rig is needed."""
    version = paths.read_installed_version(bay_dir) or "unknown"
    consumer_ref = _consumer_ref(root)

    cached = _read_rig_cache(bay_dir, version=version, consumer_ref=consumer_ref)
    if cached is not None:
        console.info("Using cached rig state (< 1 hour old)")
        return cached

    result = _fetch_rig_state(env, bay_dir, root, current_version=version, current_ref=consumer_ref)
    _write_rig_cache(bay_dir, result, version=version, consumer_ref=consumer_ref)
    return result


def _fetch_rig_state(
    env: str, bay_dir: Path, root: Path, *, current_version: str, current_ref: str
) -> bool:
    """Fetch rig state from server via SSH. Returns True if rig is needed."""
    stack_name = _read_stack_name(root)
    rig_state_path = f"/opt/{stack_name}/.rig-state"

    # Fetch rig state from server
    try:
        result = _run_on_host(
            env,
            f"cat {rig_state_path} 2>/dev/null || echo '__MISSING__'",
            bay_dir=bay_dir,
            capture=True,
            check=False,
        )
        raw = result.stdout.strip()
    except Exception:
        return True  # Can't reach server or read state — rig needed

    if not raw or "__MISSING__" in raw:
        return True  # No state file — first deploy

    # Strip ANSI escape codes from ansible ad-hoc output
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)

    # Extract JSON from ad-hoc output (first line is "host | STATUS | rc=N >>")
    lines = [l.strip() for l in raw.splitlines()]
    json_lines = []
    in_json = False
    for line in lines:
        if line.startswith("{"):
            in_json = True
        if in_json:
            json_lines.append(line)
        if line.startswith("}"):
            break

    if not json_lines:
        return True  # No JSON found — rig needed

    try:
        state = json.loads("\n".join(json_lines))
    except (json.JSONDecodeError, ValueError):
        return True  # Corrupt state — rig needed

    # Compare framework version (computed by the caller)
    state_version = state.get("bay_version", "")
    if current_version != state_version:
        console.warning(
            f"Framework updated since last rig ({state_version} \u2192 {current_version})"
        )
        return True

    # Compare consumer config ref (rig-relevant inputs only — see _consumer_ref)
    state_ref = state.get("consumer_ref", "")
    if current_ref != state_ref:
        console.warning(
            f"Consumer config changed since last rig ({state_ref} \u2192 {current_ref})"
        )
        return True

    return False


def deploy(
    ctx: typer.Context,
    env: str = typer.Argument(..., help="Target environment (e.g., production)."),
    tags: Optional[str] = typer.Option(None, "--tags", "-t", help="Comma-separated Ansible tags."),
    region: Optional[str] = typer.Option(
        None,
        "--region",
        "-r",
        help="Target a specific region (maps to ansible --limit <host>).",
    ),
    rig: bool = typer.Option(
        False,
        "--rig",
        help="Full deploy: include infrastructure roles (nftables, traefik, monitoring, backups).",
    ),
    skip_validate: bool = typer.Option(
        False,
        "--skip-validate",
        help="Skip pre-deploy validation (emergency use only).",
    ),
    skip_healthcheck: bool = typer.Option(
        False,
        "--skip-healthcheck",
        help="Skip the post-deploy HTTPS reachability audit.",
    ),
    check_token_scope: bool = typer.Option(
        False,
        "--check-token-scope",
        help=(
            "Probe the GitHub API for each remote-strategy service with "
            "build.token set. Verifies the token has sufficient scope. "
            "Opt-in only — makes outbound HTTPS calls to api.github.com."
        ),
    ),
    profile: bool = typer.Option(
        False,
        "--profile",
        help="Print per-task timings and a total runtime (ansible.posix profile_tasks + timer).",
    ),
) -> None:
    """Deploy services to the target environment.

    Detects automatically whether infrastructure roles need to run (--rig
    forces them). Pre-deploy validation runs first; a post-deploy HTTPS
    reachability audit runs after. Extra args after `--` pass straight
    through to ansible-playbook.

    Put options BEFORE the environment argument \u2014 flags placed after it
    land in the ansible passthrough (known flags are rescued with a
    warning, unknown ones are forwarded).

    Tag-filtered deploys are partial: `--tags deploy_stack` does NOT
    refresh headscale split-DNS or the ACL policy on the control host.
    After changing VPN services, tailnet_proxies, or headscale_acl_policy,
    also deploy the headscale tag.

    Examples:

        bin/bay deploy production
        bin/bay deploy production --tags deploy_stack
        bin/bay deploy production --tags headscale
        bin/bay deploy --rig production
        bin/bay deploy production --region eu
        bin/bay deploy production -- --check --diff
        bin/bay deploy production -- -e bay_reconciler_plan_only=true
    """
    console.show_banner(subtitle=f"Deploy \u2192 {env}")
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    # Rescue flags swallowed by allow_interspersed_args=False
    rig, tags, skip_validate, region = _rescue_interspersed_args(
        ctx, rig=rig, tags=tags, skip_validate=skip_validate, region=region,
    )
    # Both of these are declared Typer options, but Click stops parsing at the
    # positional `env`, so anything placed after it lands in ctx.args instead.
    # They are filtered out of the ansible passthrough below either way — so an
    # unrescued flag is not forwarded anywhere, it is silently DROPPED. That is
    # the worst outcome for --check-token-scope in particular: the operator
    # asked for a token-scope check, saw a clean validation, and got no check.
    if "--skip-healthcheck" in (ctx.args or []):
        skip_healthcheck = True
    if "--check-token-scope" in (ctx.args or []):
        check_token_scope = True
    if "--profile" in (ctx.args or []):
        profile = True

    _validate_env(env, root)

    # ── Rig state check ──────────────────────────────────────────────
    if not rig and not tags:
        rig_needed = _check_rig_state(env, bay_dir, root)
        if rig_needed:
            console.info("Infrastructure changes detected — running full deploy with rig")
            rig = True
        else:
            console.info("Infrastructure unchanged — skipping infra roles (use --rig to force)")

    # ── Pre-deploy validation ────────────────────────────────────────
    if skip_validate:
        console.warning("Skipping pre-deploy validation (--skip-validate)")
    else:
        from bay_cli.commands.validate import run_validation

        result = run_validation(root, env, bay_dir=bay_dir, show_banner=False, check_token_scope=check_token_scope)

        if result.total_issues:
            console.console.print()
            console.error(
                f"Validation failed with {result.total_issues} "
                f"error{'s' if result.total_issues != 1 else ''} "
                f"-- fix the above or use --skip-validate to bypass"
            )
            raise typer.Exit(code=1)

        console.console.print()

    # _rig_mode: controls whether infra roles run (true when --rig or --tags)
    # _rig_write: controls whether the rig state checkpoint is updated
    #   (only on full rig deploys — tag-filtered runs are partial)
    rig_mode = rig or bool(tags)
    rig_write = rig and not bool(tags)
    extra_args = [a for a in (ctx.args or []) if a not in ("--", "--rig", "--skip-validate", "--skip-healthcheck", "--check-token-scope", "--profile")]
    extra_args = [
        "-e", f"_rig_mode={'true' if rig_mode else 'false'}",
        "-e", f"_rig_write={'true' if rig_write else 'false'}",
    ] + extra_args

    if region:
        host = _resolve_target_host(bay_dir, region)
        if host is None:
            raise BayError(f"Unknown region '{region}' for env '{env}'")
        extra_args = ["-l", host] + extra_args

    _run_playbook("deploy", env, tags, extra_args, profile=profile)
    _show_headscale_onboarding(root, bay_dir)

    # ── Post-deploy reachability audit ────────────────────────────────
    # Skip under dry-run (--check / -C) so we don't probe real URLs for
    # a playbook that never mutated state. Also skip if tags filter
    # excluded deploy_stack — there's nothing meaningful to probe.
    is_dry_run = any(a in ("--check", "-C", "--diff") for a in (ctx.args or []))
    tags_exclude_deploy = tags is not None and "deploy_stack" not in (tags or "")

    # ── Refresh rig-state cache (S9) ─────────────────────────────────────
    # The deploy succeeded (a failure would have raised). After a non-dry,
    # non-tag deploy the server's rig matches the current inputs — a rig deploy
    # just wrote .rig-state, a rig-skip deploy already matched — so record that
    # and the next deploy skips infra. A tag-filtered deploy is partial:
    # invalidate so the next run re-checks from scratch.
    if not is_dry_run:
        if tags:
            _invalidate_rig_cache(bay_dir)
        else:
            _record_rig_matched(bay_dir, root)

    if skip_healthcheck:
        console.info("Skipping post-deploy healthcheck (--skip-healthcheck).")
    elif is_dry_run:
        console.info("Skipping post-deploy healthcheck (dry-run mode).")
    elif tags_exclude_deploy:
        console.info(
            f"Skipping post-deploy healthcheck (--tags={tags} did not include deploy_stack)."
        )
    else:
        _run_post_deploy_healthcheck(env, root)


def _run_post_deploy_healthcheck(env: str, root: Path) -> None:
    """Post-deploy reachability audit. Probes every public service's
    `domains:` with HTTPS GET in parallel and reports status. Does not
    exit the deploy non-zero — the deploy itself already succeeded, and
    we don't want a transient 502 on an unrelated service to fail-out
    an otherwise-good rollout. Failures are reported loudly with a
    `docker logs` hint so the operator sees them."""
    from bay_cli.config import StackConfig
    from bay_cli.healthcheck import (
        display_label,
        readiness_note,
        run_healthcheck,
        summarize,
    )

    cfg = StackConfig(root)
    services = cfg.get_services()
    if not services:
        return

    console.console.print()
    console.header("Post-deploy Healthcheck")

    results = run_healthcheck(
        services, include_vpn=False, region_vars=cfg.resolve_region_vars(env)
    )
    if not results:
        console.info("No public services with domains — nothing to probe.")
        return

    # Pad the label column to the widest label in this run (min 40) so the
    # status badges stay aligned even when one service has a long probed URL.
    labels = [display_label(r) for r in results]
    width = max([40, *(len(s) for s in labels)])

    for r, label in zip(results, labels):
        padded = f"{label:<{width}s}"
        if r.skipped:
            console.info(f"  {padded} (skipped -- {r.skip_reason})")
        elif r.ok:
            status = f"{r.status} OK" if r.status is not None else "OK"
            console.success(f"  {padded} {status:14s} [pass]{readiness_note(r)}")
        else:
            tag = (r.error or str(r.status) or "FAIL")[:24]
            console.error(f"  {padded} {tag:14s} [FAIL]")

    summary = summarize(results)
    console.console.print()
    console.console.print(
        f"  {summary['passed']} passed, {summary['failed']} failed, "
        f"{summary['skipped']} skipped ({summary['total']} checks)"
    )

    if summary["failed"]:
        console.console.print()
        console.warning(
            f"{summary['failed']} service(s) failed post-deploy healthcheck. "
            "Deploy succeeded, but users may see outages."
        )
        for r in results:
            if r.ok or r.skipped:
                continue
            what = f"HTTP {r.status}" if r.status is not None else (r.error or "unknown")
            console.console.print(f"  [red]x[/red] {display_label(r)}  {what}")
            console.console.print(
                f"    -> Run: [cyan]ssh debugbot@<host> \"docker logs {r.service} --tail 50\"[/cyan]"
            )


def _region_extra_args(
    bay_dir: Path, region: str | None, env: str, base_args: list[str]
) -> list[str]:
    if not region:
        return list(base_args)
    host = _resolve_target_host(bay_dir, region)
    if host is None:
        raise BayError(f"Unknown region '{region}' for env '{env}'")
    return ["-l", host] + list(base_args)


def provision(
    ctx: typer.Context,
    env: str = typer.Argument(..., help="Target environment (e.g., production)."),
    tags: Optional[str] = typer.Option(None, "--tags", "-t", help="Comma-separated Ansible tags."),
    region: Optional[str] = typer.Option(
        None, "--region", "-r", help="Target a specific region (maps to ansible --limit <host>)."
    ),
    profile: bool = typer.Option(
        False,
        "--profile",
        help="Print per-task timings and a total runtime (ansible.posix profile_tasks + timer).",
    ),
) -> None:
    """Provision and harden a server (base OS, users, firewall, Docker).

    Run once for new servers, and again after changing provisioning-level
    config (vpn_allowed_ips, netplan, sshd hardening, CrowdSec). Extra args
    after `--` pass through to ansible-playbook.

    Note: the netplan tag intentionally fails when connecting over the
    tailnet \u2014 a safety assertion rejects netplan_address != ansible_host.

    Examples:

        bin/bay provision production
        bin/bay provision production --tags nftables,crowdsec
        bin/bay provision eu -- --check --diff
    """
    console.show_banner(subtitle=f"Provision \u2192 {env}")
    _, tags, _, region = _rescue_interspersed_args(ctx, tags=tags, region=region)
    bay_dir = paths.find_bay_dir()
    # `--profile` after the positional env lands in ctx.args: promote it, and
    # strip it either way so it is never forwarded to ansible-playbook.
    if "--profile" in (ctx.args or []):
        profile = True
    passthrough = [a for a in (ctx.args or []) if a != "--profile"]
    extra_args = _region_extra_args(bay_dir, region, env, passthrough)
    _run_playbook("provision", env, tags, extra_args, profile=profile)


def restore(
    ctx: typer.Context,
    env: str = typer.Argument(..., help="Target environment (e.g., production)."),
    tags: Optional[str] = typer.Option(None, "--tags", "-t", help="Comma-separated Ansible tags."),
    region: Optional[str] = typer.Option(
        None, "--region", "-r", help="Target a specific region (maps to ansible --limit <host>)."
    ),
) -> None:
    """Run the restore playbook directly (low-level).

    Prefer `bin/bay backup restore <env> <accessory>` \u2014 it lists
    snapshots, prompts for confirmation, and passes the right variables.
    This command runs the playbook as-is and expects them as extra args.

    Examples:

        bin/bay backup restore production postgres
        bin/bay restore production -- -e accessory=postgres -e confirm=yes
    """
    console.show_banner(subtitle=f"Restore \u2192 {env}")
    _, tags, _, region = _rescue_interspersed_args(ctx, tags=tags, region=region)
    bay_dir = paths.find_bay_dir()
    extra_args = _region_extra_args(bay_dir, region, env, ctx.args)
    _run_playbook("restore", env, tags, extra_args, skip_git_health=True)


# ── Runtime helpers (logs, restart) ──────────────────────────────────


def _run_on_host(
    env: str,
    cmd: str,
    *,
    bay_dir: Path,
    capture: bool = True,
    message: str = "",
    check: bool = True,
    limit: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command on the target host via ansible ad-hoc."""
    uv_cmd = ansible._uv_run_cmd(bay_dir)
    full_cmd = [
        *uv_cmd,
        "ansible", env,
        "-m", "ansible.builtin.shell",
        "-a", cmd,
        "--become",
    ]
    if limit:
        full_cmd.extend(["--limit", limit])
    return runner.run(
        full_cmd,
        capture=capture,
        message=message,
        env=ansible._collections_env(bay_dir),
        check=check,
    )


def _validate_service_name(name: str, root: Path) -> None:
    """Validate that a service or accessory exists in services.yml."""
    from bay_cli.config import StackConfig

    cfg = StackConfig(root)
    if cfg.get_service(name) is not None:
        return

    available = sorted(
        list(cfg.get_services().keys()) + list(cfg.get_accessories().keys())
    )
    hint = f"Available: {', '.join(available)}" if available else None
    raise BayError(
        f"Service '{name}' not found in services.yml",
        code=BayError.not_found("service", name).code,
        hint=hint,
    )


def _resolve_target_host(bay_dir: Path, region: str | None) -> str | None:
    """Resolve the ansible --limit host for a given region.

    For single-server setups returns None (no limit needed).
    For multi-region with --region, returns the host IP for that region.
    """
    if region is None:
        return None

    consumer_root = bay_dir.parent
    inventory = consumer_root / "hosts" / "production"
    if not inventory.exists():
        return None

    text = inventory.read_text()
    if "[production:children]" not in text:
        return None

    # Parse children groups
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

    # Map each child group to its first host
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

    return groups.get(region)


def _all_service_names(root: Path) -> list[str]:
    """Return sorted list of all service and accessory names."""
    from bay_cli.config import StackConfig

    cfg = StackConfig(root)
    return sorted(
        list(cfg.get_services().keys()) + list(cfg.get_accessories().keys())
    )


# ── logs command ─────────────────────────────────────────────────────


# Date-shaped --since (YYYY-MM-DD) implies archive search,
# which requires --path. Duration-shaped values like `1h`, `30m`, `2d`
# still forward to `docker logs --since` as originally shipped.
_DATE_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_date_shaped(since: str) -> bool:
    """Return True if --since looks like a calendar date (YYYY-MM-DD).

    Date-shaped values belong to the archive-search path and
    require --path. Anything else is treated as a docker-logs duration.
    """
    return bool(_DATE_SINCE_RE.match(since))


def _archive_path_for(stack_name: str, service: str) -> str:
    """Return the on-host archive directory for a service.

    This is the single source of truth for the path convention —
    any other code that derives it (future --scrub, docs, debugbot
    banner) must call this helper so renames propagate atomically
    (see the split-surface lesson).
    """
    return f"/opt/{stack_name}/logs/services/{service}/"


def _operator_identity() -> str:
    """Return the operator's git user.email for audit-line bookkeeping.

    Falls back to 'unknown' when git is unavailable or the email isn't
    configured. Used by `--scrub` to populate the `operator=` field in
    the `.prune-log` audit entry.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        email = result.stdout.strip()
        return email or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def _scrub_count_matches(
    env: str,
    bay_dir: Path,
    stack_name: str,
    service: str,
    pattern: str,
    limit: Optional[str],
) -> dict[str, int]:
    """Dry-run: run grep/zgrep on the host and return {filename: count}.

    Does NOT touch any file or the archiver timer. Safe to call in
    dry-run mode. Parses the one-line-per-file output emitted by the
    remote shell snippet.
    """
    log_dir = _archive_path_for(stack_name, service).rstrip("/")
    # grep returns rc=1 on zero matches; `|| echo 0` normalizes.
    snippet = (
        f"cd {log_dir} 2>/dev/null || exit 0; "
        f"for f in live.log *.log.gz; do "
        f"  [ -e \"$f\" ] || continue; "
        f"  if [ \"${{f##*.}}\" = \"gz\" ]; then "
        f"    n=$(zgrep -cE -- {shlex_quote(pattern)} \"$f\" 2>/dev/null || echo 0); "
        f"  else "
        f"    n=$(grep -cE -- {shlex_quote(pattern)} \"$f\" 2>/dev/null || echo 0); "
        f"  fi; "
        f"  printf '%s\\t%s\\n' \"$f\" \"$n\"; "
        f"done"
    )
    result = _run_on_host(env, snippet, bay_dir=bay_dir, capture=True, limit=limit, check=False)
    counts: dict[str, int] = {}
    # Ansible ad-hoc output has a header line like `host | SUCCESS | rc=0 >>`
    # before the payload — walk past it. Strip ANSI color codes first;
    # ansible colorizes stdout by default and "\x1b[0;33mlive.log\t1\x1b[0m"
    # would otherwise defeat the int() parse and silently produce an empty dict.
    ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    for raw in result.stdout.splitlines():
        line = ansi_re.sub("", raw).strip()
        if not line or "\t" not in line:
            continue
        name, _, count_str = line.partition("\t")
        try:
            counts[name.strip()] = int(count_str.strip())
        except ValueError:
            continue
    return counts


def _print_scrub_dry_run_report(
    service: str, pattern: str, archive_dir: str, counts: dict[str, int]
) -> int:
    """Print the dry-run preview. Returns total matches."""
    total = sum(counts.values())
    console.header(f"Scrub preview — {service}")
    console.info(f"  Archive: {archive_dir}")
    console.info(f"  Pattern: {pattern}")
    console.console.print()
    if not counts:
        console.info("  (no archive files found on host — nothing to scrub)")
        return 0
    # Filter to files with matches + a zero-count summary row for the rest.
    affected = [(f, n) for f, n in sorted(counts.items()) if n > 0]
    unaffected = sum(1 for n in counts.values() if n == 0)
    if affected:
        for name, n in affected:
            console.console.print(f"  [yellow]~[/yellow] {name:<30s} {n} matching line(s)")
    else:
        console.info("  (pattern did not match any lines in any file)")
    if unaffected:
        console.console.print(f"  [dim]({unaffected} file(s) scanned, no matches)[/dim]")
    console.console.print()
    if total > 0:
        console.warning(
            f"{total} line(s) would be removed. Re-run with --yes to execute."
        )
    return total


def _scrub_execute(
    env: str,
    bay_dir: Path,
    stack_name: str,
    service: str,
    pattern: str,
    operator: str,
    limit: Optional[str],
) -> None:
    """Execute scrub on the host.

    Invokes the `scrub-logs.sh` script deployed by the log_archive
    role. The script:
      - does `systemctl stop bay-logarchive@<svc>.timer` before touching live.log
      - filters matching lines out of live.log and every .log.gz
      - recomputes `.sha256` sidecars for modified archives
      - appends one `.prune-log` entry per modified file with `reason=scrub`
      - has a `trap EXIT` cleanup that restarts the archiver timer
        whether or not the scrub succeeded

    The literal strings `stop bay-logarchive`, `reason=scrub`, and the
    `trap`/`cleanup` pattern live in the deployed script — kept in sync
    with the comments here so the split-surface lesson applies (if
    the path convention changes, both sides must update together).
    """
    script = f"/opt/{stack_name}/bin/scrub-logs.sh"
    pattern_q = shlex_quote(pattern)
    operator_q = shlex_quote(operator)
    cmd = f"{script} {service} /opt/{stack_name} {pattern_q} {operator_q}"
    _run_on_host(env, cmd, bay_dir=bay_dir, capture=False, limit=limit)


def _print_archive_zcat_hint(archive_dir: str, since: str) -> None:
    """Print an educational zcat pipeline for archive search since a date.

    Dry-run only — the script does NOT execute anything. Operators
    copy-paste and refine for their own grep patterns. Uses awk's
    lexicographic timestamp compare on the first field because every
    archived line begins with an RFC 3339 UTC timestamp (ADR-002).
    """
    console.console.print(f"# Archive path: {archive_dir}")
    console.console.print(f"# To search archives since {since}, run:")
    console.console.print(f"zcat {archive_dir}*.log.gz \\")
    console.console.print(f"  | awk -v since='{since}' '$1 >= since' \\")
    console.console.print(f"  | grep 'your-pattern'")
    console.console.print(
        "# Lines in the archive are Docker-prepended RFC 3339 timestamps;"
    )
    console.console.print(
        "# field $1 is the timestamp, so `awk '$1 >= <YYYY-MM-DD>'` filters cleanly."
    )


def logs(
    service: str = typer.Argument(..., help="Service or accessory name."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output."),
    tail: int = typer.Option(100, "--tail", "-n", help="Number of lines to show from the end."),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help=(
            "Show logs since duration (e.g., 1h, 30m, 2d) via `docker logs --since`. "
            "Date-shaped values (YYYY-MM-DD) require --path and produce an archive-search hint."
        ),
    ),
    path: bool = typer.Option(
        False,
        "--path",
        "-p",
        help=(
            "Print the on-host archive directory for the service and exit "
            "(no SSH). Combine with --since YYYY-MM-DD to print a zcat "
            "pipeline dry-run."
        ),
    ),
    scrub: bool = typer.Option(
        False,
        "--scrub",
        help=(
            "GDPR erasure: remove lines matching --pattern from live.log and "
            "all rotated .log.gz archives. Requires --pattern. Prints a "
            "dry-run preview by default; add --yes to execute."
        ),
    ),
    pattern: Optional[str] = typer.Option(
        None,
        "--pattern",
        help="Regex pattern for --scrub (ERE; passed to `grep -E`).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm --scrub destructive operation. Required to actually modify files.",
    ),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: Optional[str] = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Show container logs for a service, or operate on its log archive.

    Modes:

        (default)   forward to `docker logs` on the target host
        --path      print the on-host archive directory and exit (no SSH)
        --scrub     GDPR erasure from live.log + rotated .log.gz archives;
                    prints a dry-run preview, add --yes to execute

    Only names from services.yml are accepted. Rig containers (traefik,
    headscale, bay-webhook, zot) are framework plumbing, not services —
    use `ssh debugbot@<host> "docker logs <name>"` for those, or
    `bin/bay gateway status` for headscale health.

    Date-shaped --since values (YYYY-MM-DD) address the archive and require
    --path; durations (1h, 30m, 2d) forward to `docker logs --since`.

    Examples:

        bin/bay logs myapp --tail 200
        bin/bay logs myapp -f
        bin/bay logs myapp --since 2h
        bin/bay logs myapp --path --since 2026-04-20
        bin/bay logs myapp --scrub --pattern 'user@example\\.com'
        bin/bay logs myapp --scrub --pattern 'user@example\\.com' --yes
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    _validate_env(env, root)
    _validate_service_name(service, root)

    # ── --path: archive helper mode ───────────────────────────────────
    if path:
        stack_name = _read_stack_name(root)
        archive_dir = _archive_path_for(stack_name, service)

        if since:
            _print_archive_zcat_hint(archive_dir, since)
        else:
            # Raw path, no trailing newline — composable with `cd $(...)`.
            import sys

            sys.stdout.write(archive_dir)
            sys.stdout.flush()
        return

    # ── --scrub: GDPR erasure mode ─────────────────────────────────────
    if scrub:
        if not pattern:
            console.error("--scrub requires --pattern '<regex>'")
            raise typer.Exit(code=1)

        stack_name = _read_stack_name(root)
        archive_dir = _archive_path_for(stack_name, service)
        limit = _resolve_target_host(bay_dir, region)

        # Always show the dry-run preview first. Execution is gated on --yes
        # so an operator types `--scrub ... --pattern X` to see what WOULD be
        # removed, then re-runs with `--yes` once the preview checks out.
        counts = _scrub_count_matches(env, bay_dir, stack_name, service, pattern, limit)
        total = _print_scrub_dry_run_report(service, pattern, archive_dir, counts)

        if not yes:
            # Dry-run mode ends here. Exit 0 — no files touched.
            return

        if total == 0:
            console.info("Nothing to scrub — exiting without touching the archive.")
            return

        # Execution path. The deployed scrub-logs.sh does `systemctl stop
        # bay-logarchive@<svc>.timer` before touching live.log and uses a
        # trap EXIT cleanup block to guarantee the archiver timer is
        # restarted even if the scrub errors out. Each affected file gets
        # a reason=scrub audit line in .prune-log.
        operator = _operator_identity()
        try:
            _scrub_execute(env, bay_dir, stack_name, service, pattern, operator, limit)
        finally:
            # Defense in depth: if _scrub_execute raises for any reason
            # after the host script's trap fired, issue an explicit restart
            # from the controller side too. No-ops if the timer is already
            # up. Swallow all errors — this is a best-effort cleanup.
            try:
                _run_on_host(
                    env,
                    f"systemctl start bay-logarchive@{service}.timer",
                    bay_dir=bay_dir,
                    capture=True,
                    limit=limit,
                    check=False,
                )
            except Exception:  # noqa: BLE001
                pass

        console.success(f"Scrubbed {total} line(s) from {service} archive.")
        return

    # ── date-shaped --since without --path: exit 1 with hint ─────────
    if since and _is_date_shaped(since):
        console.error(
            f"--since '{since}' looks like a date. Archive search requires --path. "
            f"Try:  bin/bay logs {service} --path --since {since}"
        )
        raise typer.Exit(code=1)

    # ── default: forward to `docker logs` on the target host ─────────
    limit = _resolve_target_host(bay_dir, region)

    parts = ["docker", "logs"]
    parts.extend(["--tail", str(tail)])
    if follow:
        parts.append("--follow")
    if since:
        parts.extend(["--since", since])
    parts.append(service)

    cmd = " ".join(parts)

    _run_on_host(
        env,
        cmd,
        bay_dir=bay_dir,
        capture=False,
        limit=limit,
    )


# ── restart command ──────────────────────────────────────────────────


def restart(
    service: Optional[list[str]] = typer.Argument(None, help="Service(s) to restart. Omit to restart all."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: Optional[str] = typer.Option(None, "--region", "-r", help="Target a specific region."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation for restart-all."),
) -> None:
    """Restart service containers without a full deploy.

    Uses `docker restart`, which reuses the EXISTING container — changed
    config, env files, images, and labels are NOT picked up. To apply
    config changes, run `bin/bay deploy <env> --tags deploy_stack`
    (recreates changed containers) instead.

    Only names from services.yml are accepted; rig containers (traefik,
    headscale, bay-webhook, zot) are not restartable here. Omitting the
    service restarts everything (with confirmation).

    Examples:

        bin/bay restart myapp
        bin/bay restart myapp worker --env production
        bin/bay restart --yes
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    _validate_env(env, root)

    limit = _resolve_target_host(bay_dir, region)

    # Resolve service list
    services = service or []

    if not services:
        # Restart all — requires confirmation
        all_names = _all_service_names(root)
        if not all_names:
            console.warning("No services found in services.yml")
            return

        if not yes and not console.is_yes_mode():
            console.header(f"Restart all services ({env})")
            for name in all_names:
                console.info(f"  {name}")
            console.console.print()
            typer.confirm("Restart all services?", abort=True)
        services = all_names
    else:
        # Validate each named service
        for name in services:
            _validate_service_name(name, root)

    console.show_banner(subtitle=f"Restart \u2192 {env}")

    # Build docker restart command (direct container, no Compose)
    parts = ["docker", "restart"]
    parts.extend(services)

    cmd = " ".join(parts)

    _run_on_host(
        env,
        cmd,
        bay_dir=bay_dir,
        capture=False,
        limit=limit,
    )

    restarted = services if services else _all_service_names(root)
    console.success(f"Restarted: {', '.join(restarted)}")


def admin_shell(
    host: str = typer.Argument(
        ...,
        help="Target host: region name (e.g. 'eu'), inventory name, or IP.",
    ),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the escalation confirmation."),
) -> None:
    """Open an SSH session as the configured admin user on the named host.

    The account comes from `admin_user` in group_vars/all/main.yml — the same
    var `roles/users` provisions the account from.

    Resolves `host` from the active inventory: a region name maps to that
    region's first host, a hostname/IP is used as-is. Replaces the
    DEVOPS_SSH_USER dance for privileged operations. For read-only debugging
    prefer `ssh debugbot@<host>`.

    Examples:

        bin/bay admin-shell eu
        bin/bay admin-shell 203.0.113.10 --yes
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)
    _validate_env(env, root)

    target_ip = _resolve_target_host(bay_dir, host)
    if target_ip is None:
        target_ip = host

    admin_user = _read_admin_user(root)

    if not yes and not console.is_yes_mode():
        console.warning(
            f"This opens a privileged session on {target_ip} as {admin_user}."
        )
        typer.confirm("Continue?", abort=True)

    cmd = ["ssh", f"{admin_user}@{target_ip}"]
    import os
    env_override = dict(os.environ)
    env_override["DEVOPS_SSH_USER"] = admin_user
    subprocess.run(cmd, env=env_override, check=False)
