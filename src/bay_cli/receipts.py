"""Deploy receipts: hand the fleet state to the box, read the box's record back.

Two halves of one contract (format: ``docs/deploy-receipt.md``):

* :func:`deploy_extra_vars` gives ``bay deploy`` the fleet and framework
  commits. The play passes them to ``python -m bay_reconcile.receipt``, which
  writes ``/var/lib/bay/receipts/<env>.json`` on every box.
* :func:`status_document` is ``bay status --json``: framework, fleet and one
  entry per box with the receipt read back over SSH.

Boxes are reached the way ``bay logs`` and ``bay gateway`` reach them: an
``ansible <env>`` ad-hoc call through the framework's uv environment, run
from the fleet root so its ``ansible.cfg``, inventory and SSH settings apply.
The only difference is the machine-readable ``ansible.posix.json`` stdout
callback, so one call gives one parsed result per box. Each box runs exactly
one ``cat``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from bay_cli import ansible, git, paths
from bay_cli.context import Context
from bay_reconcile.receipt import RECEIPTS_DIR, receipt_path

STATUS_VERSION = 1

#: ``(argv, extra_env, cwd) -> CompletedProcess``. Tests swap in a fake.
Runner = Callable[[list[str], dict[str, str], Path], "subprocess.CompletedProcess[str]"]

# SSH must never ask anything: `bay status` is run by scripts and agents.
# BatchMode makes a missing key or an unknown host key fail fast instead of
# prompting. Appended to the fleet's own ssh_args, not a replacement for them.
_SSH_EXTRA_ARGS = "-o BatchMode=yes"
_CONNECT_TIMEOUT = "10"


# ── Deploy side ──────────────────────────────────────────────────────────


def fleet_state(cx: Context) -> dict[str, Any]:
    """``{"root", "commit", "dirty"}`` for the fleet. Commit and dirty are None outside git."""
    return {
        "root": str(cx.fleet_root),
        "commit": git.head_commit(cx.fleet_root),
        "dirty": git.is_dirty(cx.fleet_root),
    }


def deploy_extra_vars(cx: Context) -> list[str]:
    """``-e <json>`` for ansible-playbook: the commits the receipt records.

    JSON, not ``key=value``: a ``key=value`` extra var always arrives as a
    string, so ``fleet_dirty=false`` would be the truthy string "false" and a
    missing commit would be the string "None".
    """
    fleet = fleet_state(cx)
    payload = {
        "bay_receipt_fleet_commit": fleet["commit"],
        "bay_receipt_fleet_dirty": fleet["dirty"],
        "bay_receipt_framework_commit": git.head_commit(cx.framework_root),
    }
    return ["-e", json.dumps(payload)]


# ── Status side ──────────────────────────────────────────────────────────


def list_envs(cx: Context) -> list[str]:
    """Environment names: the inventory files under ``hosts/``."""
    if not cx.hosts_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in cx.hosts_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and "." not in p.name
    )


def _default_runner(
    argv: list[str], extra_env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    import os

    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, **extra_env},
        stdin=subprocess.DEVNULL,
        timeout=120,
    )


def _adhoc_cat(cx: Context, env: str) -> tuple[list[str], dict[str, str]]:
    path = receipt_path(env, RECEIPTS_DIR)
    argv = [
        *ansible._uv_run_cmd(cx.framework_root),
        "ansible",
        env,
        "-m",
        "ansible.builtin.command",
        "-a",
        f"cat {path}",
        "-T",
        _CONNECT_TIMEOUT,
        f"--ssh-extra-args={_SSH_EXTRA_ARGS}",
    ]
    extra_env = {
        **ansible._collections_env(cx.framework_root),
        "ANSIBLE_LOAD_CALLBACK_PLUGINS": "1",
        "ANSIBLE_STDOUT_CALLBACK": "ansible.posix.json",
    }
    return argv, extra_env


def _parse_adhoc(stdout: str) -> dict[str, Mapping[str, Any]] | None:
    """Per-host results from ``ansible.posix.json`` output, or None if unparsable."""
    start = stdout.find("{")
    if start == -1:
        return None
    try:
        doc = json.loads(stdout[start:])
    except ValueError:
        return None
    hosts: dict[str, Mapping[str, Any]] = {}
    for play in doc.get("plays", []) or []:
        for task in play.get("tasks", []) or []:
            for name, result in (task.get("hosts") or {}).items():
                if isinstance(result, Mapping):
                    hosts[str(name)] = result
    return hosts


def _box_entry(env: str, box: str | None, result: Mapping[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"env": env, "box": box, "receipt": None, "error": None}
    if result.get("unreachable"):
        entry["error"] = f"unreachable: {result.get('msg') or 'no detail'}"
        return entry
    if result.get("rc") not in (0, None) or result.get("failed"):
        stderr = str(result.get("stderr") or result.get("msg") or "")
        if "No such file or directory" in stderr:
            return entry  # never deployed with receipts: no receipt, no error
        entry["error"] = f"cannot read receipt: {stderr.strip() or 'unknown error'}"
        return entry
    try:
        receipt = json.loads(str(result.get("stdout") or ""))
    except ValueError:
        entry["error"] = "receipt is not valid JSON"
        return entry
    if not isinstance(receipt, dict):
        entry["error"] = "receipt is not a JSON object"
        return entry
    entry["receipt"] = receipt
    return entry


def fetch_receipts(cx: Context, env: str, *, run: Runner | None = None) -> list[dict[str, Any]]:
    """One ``cat`` per box in inventory group ``env``. Never raises for a box."""
    argv, extra_env = _adhoc_cat(cx, env)
    runner = run or _default_runner
    try:
        proc = runner(argv, extra_env, cx.fleet_root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [{"env": env, "box": None, "receipt": None, "error": f"ansible failed: {exc}"}]

    hosts = _parse_adhoc(proc.stdout or "")
    if hosts is None:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit {proc.returncode}"
        return [{"env": env, "box": None, "receipt": None, "error": f"ansible failed: {message}"}]
    if not hosts:
        return [{"env": env, "box": None, "receipt": None, "error": "no boxes in this environment"}]
    return [_box_entry(env, name, hosts[name]) for name in sorted(hosts)]


def framework_state(cx: Context) -> dict[str, Any]:
    return {
        "pinned": paths.read_pinned_version(cx.fleet_root),
        "checkout": git.ref_name(cx.framework_root),
        "latest": git.newest_tag(cx.framework_root),
    }


def status_document(
    cx: Context,
    *,
    env: str | None = None,
    remote: bool = True,
    run: Runner | None = None,
) -> dict[str, Any]:
    """The ``bay status --json`` document (``schemas/status.schema.json``)."""
    boxes: list[dict[str, Any]] = []
    if remote:
        for name in [env] if env else list_envs(cx):
            boxes.extend(fetch_receipts(cx, name, run=run))
    return {
        "status_version": STATUS_VERSION,
        "framework": framework_state(cx),
        "fleet": fleet_state(cx),
        "boxes": boxes,
    }
