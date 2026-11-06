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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from bay_cli import ansible, git, paths
from bay_cli.context import Context
from bay_reconcile.receipt import RECEIPTS_DIR, receipt_path

STATUS_VERSION = 2

#: ``(argv, extra_env, cwd) -> CompletedProcess``. Tests swap in a fake.
Runner = Callable[[list[str], dict[str, str], Path], "subprocess.CompletedProcess[str]"]

# SSH must never ask anything: `bay status` is run by scripts and agents.
# BatchMode makes a missing key or an unknown host key fail fast instead of
# prompting. Appended to the fleet's own ssh_args, not a replacement for them.
_SSH_EXTRA_ARGS = "-o BatchMode=yes"
_CONNECT_TIMEOUT = "10"


# ── Deploy side ──────────────────────────────────────────────────────────


def fleet_state(cx: Context) -> dict[str, Any]:
    """``{"root", "source", "commit", "dirty"}`` for the fleet.

    ``source`` is how Bay found the fleet. Commit and dirty are None outside git.
    """
    return {
        "root": str(cx.fleet_root),
        "source": cx.source,
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


#: ``fleet_root -> {group or host name: host names}``. Tests swap in a fake.
InventoryReader = Callable[[Path], dict[str, list[str]]]


def inventory_names(fleet_root: Path, framework_root: Path | None = None) -> dict[str, list[str]]:
    """``{name: hosts}`` for every group and every host of the fleet inventory.

    A group gives its hosts (children included), a host gives itself. The
    inventory is read as the deploy reads it: ``ansible-inventory --graph``
    runs in the framework's uv environment with the deploy's environment
    (:func:`bay_cli.ansible.fleet_env`, the vendored collections) and the same
    ``-i`` directory (:func:`bay_cli.ansible._fleet_inventory`). ``--graph``
    loads no variables, so it needs no vault password. Raises
    :class:`RuntimeError` when the inventory cannot be read.
    """
    import os

    from bay_cli.context import package_root

    bay_dir = framework_root or package_root()
    with ansible._fleet_inventory(fleet_root) as source:
        if source is None:
            return {}
        env = {
            **os.environ,
            **ansible._collections_env(bay_dir),
            **ansible.fleet_env(bay_dir, fleet_root),
            "ANSIBLE_FORCE_COLOR": "False",
            "ANSIBLE_NOCOLOR": "True",
        }
        cmd = [*ansible._uv_run_cmd(bay_dir), "ansible-inventory", "-i", str(source), "--graph"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, cwd=fleet_root, env=env, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"ansible-inventory did not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"ansible-inventory failed: {detail[-1] if detail else f'exit {proc.returncode}'}"
        )
    return parse_graph(proc.stdout)


def parse_graph(text: str) -> dict[str, list[str]]:
    """``{group or host: hosts}`` from ``ansible-inventory --graph`` output."""
    import re

    out: dict[str, set[str]] = {}
    stack: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw).rstrip()
        if not line.strip():
            continue
        depth = line.count("|")
        name = line.split("--", 1)[1] if "--" in line else line.strip()
        if name.startswith("@"):
            group = name[1:].rstrip(":")
            del stack[depth:]
            stack.append(group)
            out.setdefault(group, set())
            continue
        del stack[depth:]
        for group in stack:
            out.setdefault(group, set()).add(name)
        out.setdefault(name, set()).add(name)
    return {k: sorted(v) for k, v in out.items()}


def host_boxes(
    fleet_root: Path,
    boxes: Mapping[str, Mapping[str, Any]],
    box_env: str,
    *,
    read_inventory: InventoryReader | None = None,
    names: Mapping[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """``{inventory host: [fleet box names]}`` for the boxes of ``box_env``.

    A receipt's ``box`` is the ``inventory_hostname`` (often an address),
    while the lock and the plan name a box of ``bay.fleet.toml``. The rule is
    the deploy's own placement (``docs/layout-scenarios.md``, "Words"): the
    compiler writes the box's ``group`` and the deploy puts its containers on
    the hosts of that group. So a box with a ``group`` gets that group's hosts
    and only those. A box with no group is alone in its box env and gets every
    host of the box env. A host in the groups of two boxes runs both, so it
    maps to both. The box name places nothing. Raises when the inventory
    cannot be read.
    """
    found = names if names is not None else (read_inventory or inventory_names)(fleet_root)
    out: dict[str, list[str]] = {}
    for name, box in sorted(boxes.items()):
        if box.get("env") != box_env:
            continue
        group = box.get("group")
        hosts = set(found.get(str(group) if group else box_env, []))
        for host in sorted(hosts):
            out.setdefault(host, []).append(name)
    return out


def fetch_receipts(
    cx: Context,
    env: str,
    *,
    run: Runner | None = None,
    hosts: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """One ``cat`` per box in inventory group ``env``. Never raises for a box.

    ``hosts`` (the scope of a limited ``bay up``) adds ``-l <hosts>``: only
    those hosts are asked.
    """
    argv, extra_env = _adhoc_cat(cx, env)
    if hosts:
        argv += ["-l", ",".join(hosts)]
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


def list_commit_tags(
    cx: Context, env: str, repo: str, *, run: Runner | None = None, latest: bool = False
) -> dict[str, list[str]]:
    """``{box: [commit tags of repo]}`` for every box of ``env`` that answered.

    ``latest``: also list the ``latest`` tag when the box has it (``bay up``
    asks after a failed deploy whether a build container has any image).

    One ``docker image ls <repo>`` per box (``bay rollback --to`` asks before
    it moves anything). The default table output is parsed, because an ad-hoc
    argument is templated by Ansible and a ``--format '{{...}}'`` would not
    survive it. Raises OSError when no box answered.
    """
    import shlex

    from bay_reconcile.images import is_commit

    argv, extra_env = _adhoc_cat(cx, env)
    argv[argv.index("-a") + 1] = f"docker image ls {shlex.quote(repo)}"
    runner = run or _default_runner
    try:
        proc = runner(argv, extra_env, cx.fleet_root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(f"ansible failed: {exc}") from exc
    hosts = _parse_adhoc(proc.stdout or "")
    out: dict[str, list[str]] = {}
    for name, result in sorted((hosts or {}).items()):
        if result.get("unreachable") or result.get("failed") or result.get("rc") not in (0, None):
            continue
        tags: set[str] = set()
        for line in str(result.get("stdout") or "").splitlines()[1:]:
            cols = line.split()
            if len(cols) >= 2 and cols[0] == repo and (
                is_commit(cols[1]) or (latest and cols[1] == "latest")
            ):
                tags.add(cols[1])
        out[name] = sorted(tags)
    if not out:
        raise OSError(f"no box of {env} answered `docker image ls {repo}`")
    return out


def framework_state(cx: Context) -> dict[str, Any]:
    """``{"version", "path"}``: ``bay_version`` from version.yml, and the install."""
    return {
        "version": paths.read_installed_version(cx.framework_root),
        "path": str(cx.framework_root),
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
