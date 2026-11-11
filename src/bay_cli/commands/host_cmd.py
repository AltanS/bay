"""``bay host upgrade``: upgrade the OS packages of one box.

The command runs ``host_upgrade.yml`` against exactly one inventory host, as
``bay deploy`` runs its playbook: the fleet sync gate first (fetch, pull,
version floor, the local run lock), then the playbook with ``-l <host>``.

It refuses to start while a Bay run is active for the box env:

* the fleet holds a fresh claim of a ``bay up`` on that box env (another
  machine, or another process here): exit 43, ``reason: env_busy``;
* the box holds the deploy lock, or an earlier upgrade unit still runs: the
  playbook stops before apt and the result says ``box_locked`` or
  ``upgrade_running``, also exit 43.

While it runs, the playbook holds the box's deploy lock, so a deploy started
meanwhile stops with a message instead of dying on the apt lock. The command
writes no receipt and no lock record: the fleet is not changed.
See ``docs/host-upgrade.md``.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated, Any

import typer

from bay_cli import console
from bay_cli.commands.output import LogOpt, log_offset, print_summary, recap_lines
from bay_cli.context import Context, context_from
from bay_cli.errors import BayError, ErrorCode

app = typer.Typer(
    help="Operate on one box: upgrade its OS packages.",
    no_args_is_help=True,
)

#: The version of the ``--json`` document.
RESULT_VERSION = 1

#: Reasons that mean "a Bay run holds the box env or the box": exit 43.
BUSY_REASONS = frozenset({"env_busy", "box_locked", "upgrade_running"})

#: Passthrough options a host upgrade refuses: the CLI picks the host, and the
#: role must run whole.
_REFUSED_PASSTHROUGH = ("-l", "--limit", "-t", "--tags", "--skip-tags")


class HostUpgradeError(BayError):
    """A refusal or failure of ``bay host upgrade`` with a machine-readable reason."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        exit_code: int = 1,
        hint: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.CONFLICT, hint=hint)
        self.reason = reason
        self._exit_code = exit_code
        self.extra = dict(extra or {})

    @property
    def exit_code(self) -> int:
        return self._exit_code


def box_host(cx: Context, env: str, box: str) -> tuple[str, list[str]]:
    """``(inventory host, every box on that host)`` for ``box`` of box env ``env``.

    The box must be in ``bay.fleet.toml`` with ``env = <env>`` and map to
    exactly one inventory host (:func:`bay_cli.scope.host_map`, the deploy's
    own placement). A box whose group holds several hosts is refused: one host
    per run.
    """
    from bay_cli import plan as planmod
    from bay_cli import scope

    boxes = planmod.load_fleet_doc(cx).get("boxes") or {}
    of_env = scope.env_boxes(boxes, env)
    if box not in boxes:
        raise BayError(
            f"no box '{box}' in bay.fleet.toml",
            code=ErrorCode.NOT_FOUND,
            hint=f"Boxes of {env}: {', '.join(of_env) or 'none'}.",
        )
    if (boxes.get(box) or {}).get("env") != env:
        raise BayError(
            f"box '{box}' is not in {env} (its env is {(boxes.get(box) or {}).get('env')!r})",
            code=ErrorCode.VALIDATION_ERROR,
            hint=f"Boxes of {env}: {', '.join(of_env) or 'none'}.",
        )
    mapping, problem = scope.host_map(cx, env)
    if mapping is None:
        raise BayError(f"cannot place box '{box}' on a host: {problem}", code=ErrorCode.CONFIG_ERROR)
    hosts = sorted(h for h, names in mapping.items() if box in names)
    if len(hosts) != 1:
        raise BayError(
            f"box '{box}' maps to {len(hosts)} inventory hosts ({', '.join(hosts) or 'none'}); "
            "a host upgrade runs on exactly one host",
            code=ErrorCode.VALIDATION_ERROR,
            hint="Give the box a group with one host in hosts/<env>.",
        )
    return hosts[0], sorted(mapping[hosts[0]])


def busy_claim(cx: Context, env: str) -> dict[str, Any] | None:
    """The fresh claim of a ``bay up`` on box env ``env``, or None.

    :func:`bay_cli.plan.foreign_claim`: a claim this machine can prove dead
    does not count. This command never claims, so every live claim counts.
    """
    from bay_cli import plan as planmod

    return planmod.foreign_claim(cx, {env}, "")


def busy_message(holder: dict[str, Any]) -> str:
    """:func:`bay_cli.plan.env_busy_message` without ``--take-over``: this command has none."""
    from bay_cli import plan as planmod

    text = planmod.env_busy_message(holder)
    return text.replace(", or pass --take-over.", ".")


def _passthrough(args: list[str]) -> list[str]:
    out = [a for a in args if a != "--"]
    for arg in out:
        name = arg.split("=", 1)[0]
        if name in _REFUSED_PASSTHROUGH:
            raise BayError(
                f"{name} cannot pass through to a host upgrade",
                code=ErrorCode.VALIDATION_ERROR,
                hint="The command picks the host from --box and runs the whole role.",
            )
    return out


def _read_result(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _doc(env: str, box: str, host: str | None, boxes: list[str]) -> dict[str, Any]:
    return {
        "result_version": RESULT_VERSION,
        "ok": False,
        "env": env,
        "box": box,
        "host": host,
        "boxes_on_host": boxes,
        "dry_run": False,
        "reason": None,
        "error": None,
        "failed_task": None,
        "box_touched": False,
        "packages_upgraded": 0,
        "packages": [],
        "apt_first_rc": None,
        "apt_repair": False,
        "apt_rc": None,
        "dpkg_audit": [],
        "reboot_required": False,
        "rebooted": False,
        "reboot_note": None,
        "kernel": {"before": None, "after": None, "newest_installed": None},
        "containers": {"before": [], "after": [], "missing": [], "unhealthy": [],
                       "restart_loop": [], "restart_counts": {}, "logs": {}},
        "firewall": {"repaired": False, "why": []},
        "checks": [],
        "log": None,
    }


def merge_result(doc: dict[str, Any], result: dict[str, Any] | None, *, run_failed: bool) -> dict[str, Any]:
    """The JSON document from the playbook's result file.

    No result file (the play stopped before the role, or SSH failed): the
    run failed with ``reason: failed`` and ``box_touched`` stays as given.
    """
    out = dict(doc)
    if result is None:
        out["ok"] = False
        out["reason"] = out.get("reason") or "failed"
        out["error"] = out.get("error") or "the playbook wrote no result; read its output"
        return out
    for key in (
        "dry_run", "reason", "error", "failed_task", "packages_upgraded", "packages",
        "apt_first_rc", "apt_repair", "apt_rc", "dpkg_audit",
        "reboot_required", "rebooted", "reboot_note", "kernel", "containers", "firewall",
        "checks", "log",
    ):
        if key in result:
            out[key] = result[key]
    out["ok"] = bool(result.get("ok")) and not run_failed
    if not out["ok"] and not out.get("reason"):
        out["reason"] = "failed"
    # The role refused before it took the lock: the box was only read.
    out["box_touched"] = out.get("reason") not in BUSY_REASONS
    return out


def exit_code_for(doc: dict[str, Any]) -> int:
    if doc.get("ok"):
        return 0
    return 43 if doc.get("reason") in BUSY_REASONS else 1


def _render(doc: dict[str, Any]) -> str:
    lines = []
    head = "dry run of the host upgrade" if doc.get("dry_run") else "host upgrade"
    verdict = "ok" if doc.get("ok") else f"FAILED ({doc.get('reason')})"
    lines.append(f"{head} of box {doc['box']} ({doc.get('host')}) in {doc['env']}: {verdict}")
    if doc.get("error"):
        lines.append(f"  error: {doc['error']}")
    packages = doc.get("packages") or []
    if doc.get("dry_run"):
        lines.append(f"  packages it would upgrade: {len(packages)}")
    else:
        lines.append(f"  packages upgraded: {doc.get('packages_upgraded', 0)}")
    if packages:
        lines.append("    " + ", ".join(packages))
    if doc.get("apt_repair"):
        lines.append(
            f"  apt-get exited {doc.get('apt_first_rc')}; the repair pass exited {doc.get('apt_rc')}"
        )
    for line in doc.get("dpkg_audit") or []:
        lines.append(f"  dpkg --audit: {line}")
    kernel = doc.get("kernel") or {}
    lines.append(f"  kernel: {kernel.get('before')} -> {kernel.get('after')}")
    if doc.get("rebooted"):
        lines.append("  rebooted: yes")
    elif doc.get("reboot_note"):
        lines.append(f"  {doc['reboot_note']}")
    elif doc.get("reboot_required"):
        lines.append(
            "  a reboot is pending: run `bay host upgrade "
            f"{doc['env']} --box {doc['box']} --reboot`"
        )
    fw = doc.get("firewall") or {}
    if fw.get("repaired"):
        lines.append(f"  firewall reloaded and Docker restarted: {'; '.join(fw.get('why') or [])}")
    for check in doc.get("checks") or []:
        mark = "ok  " if check.get("ok") else "FAIL"
        lines.append(f"  {mark} {check.get('name')}: {check.get('detail')}")
    if len(doc.get("boxes_on_host") or []) > 1:
        lines.append(f"  note: the host also runs box {', '.join(b for b in doc['boxes_on_host'] if b != doc['box'])}")
    if doc.get("log"):
        lines.append(f"  apt log on the box: {doc['log']}")
    return "\n".join(lines)


_JsonOpt = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]

@app.command(
    "upgrade",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def upgrade(
    ctx: typer.Context,
    env: Annotated[str, typer.Argument(help="The box env (the inventory group), e.g. production.")],
    box: Annotated[str, typer.Option("--box", help="The one box of bay.fleet.toml to upgrade.")],
    reboot: Annotated[
        bool,
        typer.Option("--reboot", help="Reboot when the upgrade needs it, wait for the box, then check."),
    ] = False,
    as_json: _JsonOpt = False,
    log: LogOpt = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Do not fetch the fleet first (as bay deploy).")
    ] = False,
    allow_dirty: Annotated[
        bool, typer.Option("--allow-dirty", help="Run although the fleet has uncommitted files.")
    ] = False,
    allow_older_bay: Annotated[
        bool,
        typer.Option("--allow-older-bay", help="Run although the fleet was deployed with a newer Bay."),
    ] = False,
) -> None:
    """Upgrade the OS packages of one box: apt dist-upgrade, checks, reboot on request.

    Runs on exactly one box (--box). Refuses while a bay up holds a claim on
    the box env (exit 43) or the box holds the deploy lock (exit 43). Holds
    the deploy lock while it runs, so a deploy started meanwhile stops.
    apt runs in a detached systemd unit, so a dropped SSH session does not
    stop it. When the nftables upgrade flushed the firewall, it loads the Bay
    firewall again and restarts Docker. Then it checks Docker, the containers
    that ran before, nftables, Docker's chains, CrowdSec and tailscale.

    Writes no receipt and no lock record. Extra args after `--` pass to
    ansible-playbook; `-- --check` changes nothing on the box.

    Examples:

        bay host upgrade production --box alpha
        bay host upgrade production --box alpha --reboot --json --log /tmp/upgrade.log
        bay host upgrade production --box alpha -- --check
    """
    from bay_cli.commands import ops
    from bay_cli.commands.output import routed_output

    cx = context_from(ctx)
    doc = _doc(env, box, None, [])
    offset = log_offset(log)
    try:
        with routed_output(as_json, log) as say:
            if not as_json:
                console.show_banner(cx, subtitle=f"Host upgrade → {env} / {box}")
            ops._fleet_gate(
                ctx, cx, "host upgrade", env,
                offline=offline, allow_dirty=allow_dirty, allow_older_bay=allow_older_bay,
            )
            passthrough = _passthrough(list(ctx.args or []))
            ops._validate_env(env, cx.fleet_root)
            host, on_host = box_host(cx, env, box)
            doc.update({"host": host, "boxes_on_host": on_host})
            holder = busy_claim(cx, env)
            if holder is not None:
                raise HostUpgradeError(
                    busy_message(holder),
                    reason="env_busy",
                    exit_code=43,
                    extra={"holder": holder},
                )
            doc["dry_run"] = any(a in ("--check", "-C") for a in passthrough)
            with tempfile.TemporaryDirectory(prefix="bay-host-upgrade-") as tmp:
                result_path = Path(tmp) / "result.json"
                variables = {
                    "host_upgrade_reboot": reboot,
                    "host_upgrade_result_path": str(result_path),
                    "host_upgrade_box": box,
                }
                extra = ["-l", host, "-e", json.dumps(variables), *passthrough]
                say(f"host upgrade: box {box} is host {host}")
                run_failed = False
                try:
                    ops._run_playbook(cx, "host_upgrade", env, None, extra)
                except BayError as exc:
                    run_failed = True
                    doc["error"] = str(exc)
                doc = merge_result(doc, _read_result(result_path), run_failed=run_failed)
                if run_failed and doc.get("reason") not in BUSY_REASONS:
                    doc["box_touched"] = True
    except BayError as exc:
        doc.update(
            {
                "ok": False,
                "error": str(exc),
                "reason": getattr(exc, "reason", None) or "error",
                "hint": exc.hint,
            }
        )
        doc.update(getattr(exc, "extra", {}) or {})
        if not as_json:
            if log is not None:
                # The redirect is undone: the terminal gets the summary.
                print_summary([f"bay host upgrade: FAILED ({doc['reason']})", *recap_lines(log, offset)])
            raise
        typer.echo(json.dumps(doc, indent=2))
        raise typer.Exit(exc.exit_code) from None
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
    elif log is not None:
        typer.echo("\n".join([_render(doc), *recap_lines(log, offset)]))
    else:
        typer.echo(_render(doc))
    raise typer.Exit(exit_code_for(doc))
