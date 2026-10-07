"""``bay doctor``: answer the questions an operator asks before a deploy.

One line per check, each ``ok``, ``warn`` or ``fail``, in this order:

* the fleet Bay picked, and why (``--fleet``, ``BAY_FLEET``, the ``fleet``
  line of a bay.toml, ``BAY_FLEET_NAME``, or the fleet directory you stand
  in), with the resolved path (a fleet may be a symlink);
* the fleet ``format``;
* the CLI version and the checkout it runs from;
* whether the vault opens (no secret is printed);
* whether each box answers (the receipt transport, a short timeout;
  ``--no-remote`` skips it);
* whether each repo project's pinned commit can be read (the checkout here or
  the repo cache, else one fetch that never prompts);
* whether the fleet clone is behind its remote;
* Bay 1 leftovers in the fleet, and plan files that are not committed;
* then the older checks: inventory, SSH, DNS, gateway, webhook.

Exit 1 when any check fails. ``--json`` prints the lines as one document.
Complements ``bay validate``, which checks the config files.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import typer
import yaml

from bay_cli import console
from bay_cli.context import (
    FLEET_ENV,
    FLEET_NAME_ENV,
    SOURCE_BAY_TOML,
    SOURCE_CWD,
    SOURCE_ENV,
    SOURCE_FLAG,
    SOURCE_FLEETS_DIR,
    Context,
    GlobalOptions,
    context_from,
)
from bay_cli.errors import BayError

DOCTOR_VERSION = 1

#: ``(argv, extra_env, cwd) -> CompletedProcess``: the box probe's runner (tests swap it).
BoxRunner = Callable[[list[str], dict[str, str], Path], "subprocess.CompletedProcess[str]"]

#: Seconds one box probe may take in all (the SSH connect timeout is 10).
BOX_PROBE_TIMEOUT = 30


_MARKS = {"ok": ("\u2713", "green"), "warn": ("!", "yellow"), "fail": ("\u2717", "red"),
          "info": ("i", "blue")}


@dataclass
class Line:
    check: str
    status: str  # ok | warn | fail | info
    detail: str


@dataclass
class Report:
    """Collects the lines; prints each at once unless the output is JSON."""

    as_json: bool = False
    lines: list[Line] = field(default_factory=list)

    def add(self, check: str, status: str, detail: str) -> None:
        self.lines.append(Line(check, status, detail))
        if self.as_json:
            return
        from rich.markup import escape

        prefix, style = _MARKS[status]
        # One line per check, never wrapped, and a detail such as
        # "[deploy.x]" is text, not markup.
        console.console.print(
            f"  [{style}]{prefix}[/{style}] {escape(f'{check:<18} {detail}')}", soft_wrap=True
        )

    @property
    def failed(self) -> int:
        return sum(1 for line in self.lines if line.status == "fail")


def doctor(
    ctx: typer.Context,
    env: str | None = typer.Argument(
        None, help="Environment to check. Default: the fleet's primary environment."
    ),
    no_remote: bool = typer.Option(
        False, "--no-remote", help="Do not ask the boxes, and do not fetch app repos."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the lines as one JSON document."),
) -> None:
    """Run pre-deploy checks: fleet, format, CLI, vault, boxes, repos, git, and more.

    Prints one line per check (ok, warn or fail): which fleet Bay picked and
    why, the fleet format, the CLI version and install path, whether the
    vault opens, whether each box answers, whether each app repo's pinned
    commit can be read, whether the fleet clone is behind its remote, Bay 1
    leftovers, plan files that are not committed, then the inventory, SSH
    connectivity, DNS resolution, the gateway configuration and (when
    configured) GitHub webhook health. Exits 1 when a check fails.
    Complements `bay validate`, which checks config files rather than
    the environment.

    Examples:

        bay doctor
        bay doctor testing --no-remote
        bay --fleet ~/fleets/prod doctor --json
    """
    was_json = console.is_json_mode()
    as_json = as_json or was_json
    console.set_json_mode(as_json)  # the webhook probe's own lines are buffered, not printed
    try:
        failed = _run(ctx, env, no_remote=no_remote, as_json=as_json)
    finally:
        console.set_json_mode(was_json)
    if failed:
        raise typer.Exit(code=1)


def _run(ctx: typer.Context, env: str | None, *, no_remote: bool, as_json: bool) -> int:
    """Run every check and print the result. Returns the number of failed checks."""
    report = Report(as_json=as_json)
    cx, why, status, detail = _picked(ctx)
    if cx is not None and not as_json:
        console.show_banner(cx, subtitle="Doctor")
    report.add("Fleet", status, detail)
    _check_cli(report)
    fleet_doc: dict[str, Any] = {}
    if cx is not None:
        fleet_doc = _check_format(cx, report)
        env = env or str(fleet_doc.get("primary_env") or "production")
        _check_vault(cx, env, report)
        if no_remote:
            report.add("Boxes", "info", "not asked (--no-remote)")
        else:
            _check_boxes(cx, fleet_doc, env, report)
        _check_repos(cx, report, fetch=not no_remote)
        _check_behind(cx, report)
        _check_leftovers(cx, report)
        _check_plans(cx, report)
        _environment_checks(cx, env, report)
    if as_json:
        console.drain_messages()
        typer.echo(
            json.dumps(
                {
                    "doctor_version": DOCTOR_VERSION,
                    "ok": report.failed == 0,
                    "env": env,
                    "fleet": None
                    if cx is None
                    else {"root": str(cx.fleet_root), "source": cx.source, "why": why},
                    "lines": [asdict(line) for line in report.lines],
                },
                indent=2,
            )
        )
    elif report.failed:
        console.console.print()
        n = report.failed
        console.error(f"{n} check{'s' if n != 1 else ''} failed. Fix the above before deploying.")
    else:
        console.console.print()
        console.success("All checks passed!")
    return report.failed


# ── The picked fleet ─────────────────────────────────────────────────────


def _picked(ctx: typer.Context) -> tuple[Context | None, str | None, str, str]:
    """``(fleet, why, status, detail)``: the fleet the other verbs would use, and why."""
    from bay_cli.commands.project_cmd import fleet_dir_above

    try:
        cx = context_from(ctx)
    except BayError as exc:
        found = fleet_dir_above(Path.cwd())
        if found is None:
            return None, None, "fail", f"{exc}" + (f"; {exc.hint}" if exc.hint else "")
        cx = Context._from_fleet_path(found, SOURCE_CWD)
    why, given = _why(ctx, cx)
    from bay_cli.fleet_line import fleet_name

    detail = f"{fleet_name(cx.fleet_root)} ({cx.fleet_root}), picked by {why}"
    if given is not None and given.expanduser().absolute() != cx.fleet_root:
        detail += f"; {given} resolves to {cx.fleet_root}"
    return cx, why, "ok", detail


def _why(ctx: typer.Context, cx: Context) -> tuple[str, Path | None]:
    """``(why this fleet, the path as it was given)`` for the Context's source."""
    from bay_cli.context import fleets_root
    from bay_cli.fleet_line import _bay_toml_identity

    obj = ctx.find_root().obj if ctx is not None else None
    if cx.source == SOURCE_FLAG:
        given = obj.fleet if isinstance(obj, GlobalOptions) and obj.fleet else None
        return f"--fleet {given or cx.fleet_root}", Path(given) if given else None
    if cx.source == SOURCE_ENV:
        value = os.environ.get(FLEET_ENV, "")
        return f"{FLEET_ENV}={value}", Path(value) if value else None
    if cx.source == SOURCE_BAY_TOML:
        name = _bay_toml_identity(Path.cwd())[1]
        here = Path.cwd()
        toml = next(
            (d / "bay.toml" for d in [here, *here.parents] if (d / "bay.toml").is_file()), None
        )
        return f'fleet = "{name}" in {toml}', fleets_root() / str(name)
    if cx.source == SOURCE_FLEETS_DIR:
        value = os.environ.get(FLEET_NAME_ENV, "")
        return f"{FLEET_NAME_ENV}={value}", fleets_root() / value
    if cx.source == SOURCE_CWD:
        return "the fleet directory you stand in", None
    return cx.source, None


# ── Fleet, CLI, vault ────────────────────────────────────────────────────


def _check_format(cx: Context, report: Report) -> dict[str, Any]:
    from bay_cli.fleet import FLEET_FILE, FLEET_FORMAT, FleetError, fleet_format, load_fleet_file

    if not (cx.fleet_root / FLEET_FILE).is_file():
        report.add(
            "Fleet format",
            "warn",
            f"no {FLEET_FILE}: not a Bay 2 fleet yet; bay import writes one",
        )
        return {}
    try:
        doc = load_fleet_file(cx.fleet_root)
    except FleetError as exc:
        report.add("Fleet format", "fail", "; ".join(exc.lines))
        return {}
    found = fleet_format(doc)
    if found < FLEET_FORMAT:
        report.add(
            "Fleet format",
            "warn",
            f"format {found}: the locks still sit beside the project folders; the next "
            "bay plan or bay compile moves them and writes format = 2",
        )
    else:
        report.add("Fleet format", "ok", f"format {found}")
    return doc


def _check_cli(report: Report) -> None:
    from bay_cli.commands.self_cmd import framework_version
    from bay_cli.context import package_root

    root = package_root()
    version = framework_version(root)
    status = "warn" if version == "unknown" else "ok"
    report.add("CLI", status, f"bay {version} at {root}")


def _check_vault(cx: Context, env: str, report: Report) -> None:
    """Does the vault open? Decrypts to a pipe; no name and no value is printed."""
    from bay_cli import secrets_check

    if not cx.vault_pass.exists():
        report.add(
            "Vault",
            "fail",
            ".vault_pass is missing; put the vault password there (git ignores it)",
        )
        return
    path = secrets_check.secrets_file_for(cx, env)
    if path is None:
        report.add("Vault", "warn", f"no secrets file for {env}; nothing to open")
        return
    rel = path.relative_to(cx.fleet_root)
    if not path.read_text(errors="replace").lstrip().startswith("$ANSIBLE_VAULT"):
        report.add("Vault", "warn", f"{rel} is not encrypted; run bay vault encrypt {env}")
        return
    try:
        secrets_check.vault_names(cx, env)
    except secrets_check.SecretsUncheckable as exc:
        report.add("Vault", "fail", f"{rel} does not open: {exc}")
        return
    report.add("Vault", "ok", f"{rel} opens with .vault_pass")


# ── Boxes and repos ──────────────────────────────────────────────────────


def _quick_runner(
    argv: list[str], extra_env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, **extra_env},
        stdin=subprocess.DEVNULL,
        timeout=BOX_PROBE_TIMEOUT,
    )


def _box_envs(fleet_doc: dict[str, Any], env: str) -> list[str]:
    envs = {
        str(box.get("env"))
        for box in (fleet_doc.get("boxes") or {}).values()
        if isinstance(box, dict) and box.get("env")
    }
    return [env] if env in envs or not envs else sorted(envs)


def _check_boxes(
    cx: Context,
    fleet_doc: dict[str, Any],
    env: str,
    report: Report,
    run: BoxRunner | None = None,
) -> None:
    """Each box answers: one receipt read per box env, the way bay status reads it."""
    from bay_cli import receipts

    if not fleet_doc.get("boxes"):
        report.add("Boxes", "info", "bay.fleet.toml lists no box; none asked")
        return
    for box_env in _box_envs(fleet_doc, env):
        entries = receipts.fetch_receipts(cx, box_env, run=run or _quick_runner)
        for entry in entries:
            box = entry.get("box") or box_env
            error = entry.get("error")
            if error and str(error).startswith("cannot read receipt"):
                report.add("Box", "warn", f"{box} ({box_env}) answers, but {error}")
            elif error:
                report.add("Box", "fail", f"{box} ({box_env}): {error}")
            else:
                receipt = entry.get("receipt")
                when = (
                    f"last deploy {receipt.get('deployed_at')}"
                    if isinstance(receipt, dict)
                    else "no receipt yet"
                )
                report.add("Box", "ok", f"{box} ({box_env}) answers; {when}")


def _check_repos(cx: Context, report: Report, *, fetch: bool) -> None:
    """Each repo project's pinned commit is in the checkout here, the cache, or a fetch."""
    from bay_cli import gitrepo, lockfile, reposource
    from bay_cli import plan as planmod

    for name in planmod.fleet_projects(cx):
        if (cx.fleet_root / "projects" / name / "bay.toml").is_file():
            continue  # lives in the fleet: nothing to reach
        try:
            raw = lockfile.read(lockfile.lock_path(cx.fleet_root, name)) or {}
        except ValueError as exc:
            report.add("Repo", "fail", f"{name}: the lock is not valid JSON: {exc}")
            continue
        repo, commit = raw.get("repo"), raw.get("commit")
        if not repo:
            report.add("Repo", "fail", f"{name}: the lock names no repo")
            continue
        if not commit:
            report.add("Repo", "warn", f"{name}: no pinned commit yet ({repo})")
            continue
        short = str(commit)[:12]
        checkout = reposource.checkout_for(Path.cwd(), str(repo))
        if checkout is not None and gitrepo.resolve_commit(checkout, str(commit)):
            report.add("Repo", "ok", f"{name}: pin {short} is in the checkout {checkout}")
            continue
        cache = reposource.cache_path(cx.fleet_root, str(repo))
        if (cache / "HEAD").is_file() and gitrepo.resolve_commit(cache, str(commit)):
            report.add("Repo", "ok", f"{name}: pin {short} is in the repo cache")
            continue
        if not fetch:
            report.add(
                "Repo", "warn", f"{name}: pin {short} is not here yet; not fetched (--no-remote)"
            )
            continue
        cache_path, problem = reposource.ensure_cache(cx.fleet_root, str(repo), fetch=True)
        if cache_path is not None and gitrepo.resolve_commit(cache_path, str(commit)):
            report.add("Repo", "ok", f"{name}: fetched {repo}; pin {short} is there")
        elif problem:
            report.add("Repo", "fail", f"{name}: cannot reach {repo}: {problem}")
        else:
            report.add("Repo", "fail", f"{name}: pin {short} is not in {repo}; push it")


# ── Git state of the fleet ───────────────────────────────────────────────


def _check_behind(cx: Context, report: Report) -> None:
    from bay_cli import gitrepo

    if not gitrepo.is_repo(cx.fleet_root):
        report.add("Fleet clone", "warn", "the fleet is not a git repo")
        return
    behind, problem = gitrepo.behind_remote(cx.fleet_root)
    if problem:
        report.add("Fleet clone", "warn", problem)
    elif behind:
        report.add(
            "Fleet clone",
            "fail",
            f"behind its remote; run git pull in {cx.fleet_root} first (a stale clone "
            "reverts config on the next deploy)",
        )
    else:
        report.add("Fleet clone", "ok", "not behind its remote")


def _check_leftovers(cx: Context, report: Report) -> None:
    leftovers = v1_leftovers(cx.fleet_root)
    if leftovers:
        report.add(
            "v1 leftovers",
            "warn",
            f"{', '.join(leftovers)} in the fleet; Bay 2 does not use them. "
            "Remove them, and drop a shell alias bay='bin/bay'",
        )
    else:
        report.add("v1 leftovers", "ok", "none")


def _check_plans(cx: Context, report: Report) -> None:
    from bay_cli import gitrepo
    from bay_cli import plan as planmod

    if not gitrepo.is_repo(cx.fleet_root):
        return
    loose = gitrepo.uncommitted_paths(cx.fleet_root, [planmod.PLANS_DIR])
    if loose:
        shown = ", ".join(loose[:3]) + (f" and {len(loose) - 3} more" if len(loose) > 3 else "")
        report.add(
            "Plans",
            "warn",
            f"{len(loose)} plan file(s) are not committed ({shown}); bay plan saves "
            "every plan and bay up commits only the one it applies; delete the rest",
        )
    else:
        report.add("Plans", "ok", "every plan file is committed")


# ── The environment checks (inventory, SSH, DNS, gateway, webhook) ──────


def _environment_checks(cx: Context, env: str, report: Report) -> None:
    root = cx.fleet_root

    # ── Inventory ────────────────────────────────────────────────────
    inventory_file = cx.inventory(env)
    hosts = _parse_inventory(inventory_file)
    if not inventory_file.exists():
        report.add("Inventory", "fail", f"hosts/{env} not found")
    elif not hosts:
        report.add("Inventory", "fail", f"hosts/{env} has no host entries")
    else:
        n = len(hosts)
        report.add("Inventory", "ok", f"{n} host{'s' if n != 1 else ''} in {env}")

    # ── SSH connectivity ─────────────────────────────────────────────
    if hosts:
        host = hosts[0]
        candidates = _ssh_users(root, host=host, inventory_file=inventory_file)
        connected, connected_as, reason = _probe_ssh(host, candidates)
        if connected:
            who = _describe_user(connected_as, host)
            report.add("SSH connectivity", "ok", f"connected as {who}")
        else:
            attempted = ", ".join(_describe_user(u, host) for u in candidates)
            report.add("SSH connectivity", "fail", f"cannot reach {attempted} ({reason})")
    else:
        report.add("SSH connectivity", "warn", "skipped (no hosts in inventory)")

    # ── Load config files ────────────────────────────────────────────
    access_gw_cfg = _load_yaml(cx.env_file("all", "access_gateway.yml"))
    domains_cfg = _load_yaml(cx.env_file(env, "domains.yml"))
    vpn_cfg = _load_yaml(cx.env_file("all", "vpn_access.yml"))

    from bay_cli.config import access_gateway_type

    gateway_type = access_gateway_type(root, cx.framework_root)
    headscale_domain = access_gw_cfg.get("headscale_domain") if access_gw_cfg else None
    headscale_control_region = (
        access_gw_cfg.get("headscale_control_region") if access_gw_cfg else None
    )
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
            report.add("DNS", "fail", f"{probe_domain} check failed to run ({exc})")
        else:
            if resolved:
                report.add("DNS", "ok", f"{probe_domain} resolves to {resolved} ({probe_source})")
            else:
                hint = ""
                if hosts:
                    hint = f" — create a wildcard A record: *.{domain_base} -> {hosts[0]}"
                report.add("DNS", "fail", f"{probe_domain} NXDOMAIN{hint}")
    else:
        report.add("DNS", "info", "main domain skipped (no domain_base in config)")

    # ── DNS: headscale domain ────────────────────────────────────────
    if gateway_type == "headscale" and headscale_domain:
        resolved = _resolve_domain(headscale_domain)
        if resolved:
            report.add("DNS", "ok", f"{headscale_domain} resolves to {resolved}")
        else:
            hint = ""
            control_ip = _get_control_host_ip(inventory_file, headscale_control_region)
            target_ip = control_ip or (hosts[0] if hosts else None)
            if target_ip:
                hint = f" — create A record pointing to {target_ip}"
            report.add("DNS", "fail", f"{headscale_domain} NXDOMAIN{hint}")

    # ── Gateway config ───────────────────────────────────────────────
    gw_issues = _check_gateway_config(
        gateway_type, headscale_domain, vpn_cfg, _vpn_services(services_file)
    )
    if gw_issues:
        for msg in gw_issues:
            report.add("Gateway config", "fail", msg)
    elif gateway_type == "headscale":
        report.add("Gateway config", "ok", f"headscale configured with domain {headscale_domain}")
    elif gateway_type == "wireguard":
        report.add("Gateway config", "ok", "wireguard configured")
    else:
        report.add("Gateway config", "ok", "no access gateway (all services public)")

    # ── Webhook health ───────────────────────────────────────────────
    if services_file is not None:
        try:
            from bay_cli.commands.validate import (
                ValidationResult,
                _probe_webhook_health,
                _validate_yaml_files,
            )

            services_data = yaml.safe_load(services_file.read_text()) or {}
            wh_result = ValidationResult()
            parsed_files = _validate_yaml_files(root, env, wh_result)
            _probe_webhook_health(root, env, services_data, parsed_files, wh_result)
            if wh_result.total_issues:
                report.add(
                    "Webhook health", "fail", f"{wh_result.total_issues} problem(s), see above"
                )
        except Exception as _exc:
            # A probe that cannot run is an unknown, not a pass. Counting it
            # keeps `doctor` from printing "All checks passed" after a crash.
            report.add("Webhook health", "fail", f"check failed to run ({_exc})")
    else:
        report.add("Webhook health", "info", "skipped (no services.yml)")


# ── Helpers ──────────────────────────────────────────────────────────────


#: Fallback when group_vars/all/main.yml does not set admin_user. Matches the
#: value the wizard scaffolds (wizard/templates/main.yml.j2).
DEFAULT_ADMIN_USER = "bay-admin"


#: What a Bay 1 fleet held to run its own copy of Bay. Bay 2 installs the CLI once per machine.
V1_LEFTOVERS = ("bin", ".bay", ".bay-version")


def v1_leftovers(root: Path) -> list[str]:
    """The Bay 1 leftovers present in the fleet at ``root``."""
    return [name for name in V1_LEFTOVERS if (root / name).exists()]


def _inventory_ansible_user(inventory_file: Path | None, host: str) -> str | None:
    """Return the ``ansible_user`` the inventory sets for *host*, if any.

    Ansible is the tool that actually connects to the server, so its own
    answer comes first. Three places are read, most specific first:

    1. the host line itself (``1.2.3.4 ansible_user=ops``),
    2. a ``[<group>:vars]`` block for a group the host belongs to,
    3. the ``[all:vars]`` block.

    This is a best-effort INI reader, not an inventory parser. Anything it
    cannot understand simply yields None, and the caller falls back.
    """
    if inventory_file is None or not inventory_file.exists():
        return None

    var_re = re.compile(r"\bansible_user\s*=\s*(\S+)")
    section = ""
    host_groups: list[str] = []
    group_vars: dict[str, str] = {}

    for raw in inventory_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]").strip()
            continue
        if section.endswith(":vars"):
            key, sep, value = line.partition("=")
            if sep and key.strip() == "ansible_user" and value.strip():
                group_vars[section[: -len(":vars")]] = value.strip()
            continue
        if section.endswith(":children"):
            continue
        if re.split(r"\s+", line)[0] != host:
            continue
        host_groups.append(section)
        match = var_re.search(line)
        if match:
            return match.group(1)

    for group in host_groups:
        if group in group_vars:
            return group_vars[group]
    return group_vars.get("all")


def _ssh_users(
    root: Path,
    host: str | None = None,
    inventory_file: Path | None = None,
) -> list[str | None]:
    """Return the SSH users to try, most likely first.

    Order: the inventory's ``ansible_user`` for *host*, then ``admin_user``
    from group_vars, then no user at all (the ssh default, which honours
    ``~/.ssh/config``), then ``root``.

    ``root`` is last on purpose. Every hardened host has root login off, so
    trying it first spends a guaranteed failed authentication against an
    sshd that CrowdSec is watching. ``None`` means "no user in the target",
    which lets a matching ``Host`` block in ``~/.ssh/config`` decide.
    """
    main_cfg = _load_yaml(root / "group_vars" / "all" / "main.yml") or {}
    admin_user = main_cfg.get("admin_user") or DEFAULT_ADMIN_USER

    users: list[str | None] = []
    if host:
        inventory_user = _inventory_ansible_user(inventory_file, host)
        if inventory_user:
            users.append(inventory_user)
    if isinstance(admin_user, str) and admin_user.strip():
        users.append(admin_user.strip())
    users.append(None)  # the ssh default user
    users.append("root")

    ordered: list[str | None] = []
    for user in users:
        if user is None and None in ordered:
            continue
        if user is not None and (not user or user in ordered):
            continue
        ordered.append(user)
    return ordered


def _describe_user(user: str | None, host: str) -> str:
    """Human-readable form of one probe target."""
    return f"{user}@{host}" if user else f"{host} (ssh default user)"


def _probe_ssh(host: str, users: list[str | None]) -> tuple[bool, str | None, str]:
    """Try each user in turn.

    Returns (connected, the user that connected, failure reason). The user
    is None for the no-user target, so the boolean carries success, not the
    user value.
    """
    reason = "connection failed"
    for user in users:
        target = f"{user}@{host}" if user else host
        try:
            proc = subprocess.run(
                [
                    "ssh",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=5",
                    "-o", "StrictHostKeyChecking=accept-new",
                    target,
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
            return False, None, "ssh command not found"
        if proc.returncode == 0:
            return True, user, ""
        if proc.stderr:
            reason = proc.stderr.strip().split("\n")[0]
    return False, None, reason


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
    vpn_services: list[str] | None = None,
) -> list[str]:
    """Validate gateway-specific configuration, returning a list of issues.

    ``vpn_services`` are the services with ``access: vpn``; None when unknown.
    With wireguard, ``vpn_allowed_ips`` is the only source of the vpn-only
    allowlist, but it matters only to a vpn service: a fleet that serves
    everything public (the role default gateway, no access_gateway.yml) needs
    no list.
    """
    issues: list[str] = []

    if gateway_type == "headscale":
        if not headscale_domain:
            issues.append("headscale gateway requires headscale_domain in access_gateway.yml")

    if gateway_type in ("headscale", "wireguard"):
        allowed = vpn_cfg.get("vpn_allowed_ips", []) if vpn_cfg else []
        needed = gateway_type == "headscale" or vpn_services is None or bool(vpn_services)
        if not allowed and needed:
            issues.append("vpn_allowed_ips is empty in vpn_access.yml — add trusted IPs")

    return issues


def _vpn_services(services_file: Path | None) -> list[str]:
    """Names of the services with ``access: vpn`` in the services file; [] without one."""
    if services_file is None:
        return []
    try:
        data = yaml.safe_load(services_file.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return []
    return sorted(
        str(name)
        for name, svc in services.items()
        if isinstance(svc, dict) and svc.get("access") == "vpn"
    )
