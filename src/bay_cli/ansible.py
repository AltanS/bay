"""Ansible operations for bay framework.

Ansible runs from the framework checkout (roles, playbooks, vendored
collections) against a fleet (``hosts/``, ``group_vars/``, ``.vault_pass``).
The two are different directories, and the working directory is neither, so
nothing here reads the working directory:

* A playbook run gets an inventory directory built for the run, so Ansible
  finds the fleet's ``group_vars`` next to its hosts files (``_fleet_inventory``).
  The playbook learns where the fleet is from the extra var ``bay_fleet_root``.
* Every Ansible command runs with ``uv run --directory <fleet>`` once the CLI
  has resolved the fleet (``bind_fleet``), so ad-hoc commands see the fleet as
  their working directory.
* The settings a fleet used to keep in ``ansible.cfg`` (roles path, vault
  password, SSH options) travel as environment variables (``fleet_env``).
"""

import json
import shutil
import sysconfig
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bay_cli import console, runner

#: The fleet the running CLI command works on. Set once by ``bind_fleet`` when
#: the CLI resolves its Context. A process runs one command, so one fleet.
_bound_fleet: Path | None = None


def tool(name: str) -> str:
    """The path of an Ansible command that ships with the running CLI.

    ``uv tool install`` puts only ``bay`` on PATH; ``ansible-vault`` and the
    other entry points stay in the tool's own scripts directory. A bare
    ``ansible-vault`` then fails with "not found" on a machine-wide install,
    and ``bay plan`` blocks on every project with a secret. Look next to the
    interpreter first, then on PATH.
    """
    scripts = Path(sysconfig.get_path("scripts"))
    candidate = scripts / name
    if candidate.is_file():
        return str(candidate)
    return shutil.which(name) or name


def bind_fleet(fleet_root: Path | None) -> None:
    """Make every Ansible command below run for this fleet. ``None`` unbinds."""
    global _bound_fleet
    _bound_fleet = fleet_root


def _uv_run_cmd(bay_dir: Path) -> list[str]:
    cmd = ["uv", "run", "--project", str(bay_dir)]
    if _bound_fleet is not None:
        cmd += ["--directory", str(_bound_fleet)]
    return cmd


def _collections_env(bay_dir: Path) -> dict[str, str]:
    """Environment for an Ansible command: the vendored collections, plus the fleet's settings."""
    env = {"ANSIBLE_COLLECTIONS_PATH": str(bay_dir / "vendor" / "collections")}
    if _bound_fleet is not None:
        env.update(fleet_env(bay_dir, _bound_fleet))
    return env


def fleet_env(bay_dir: Path, fleet_root: Path) -> dict[str, str]:
    """What a fleet's ansible.cfg used to say, as environment variables.

    Environment variables beat ansible.cfg, so a fleet that still has the
    old file (with ``.bay/`` paths in it) is not misled by it.
    """
    env = {
        "ANSIBLE_ROLES_PATH": f"{bay_dir / 'vendor' / 'roles'}:{bay_dir / 'roles'}",
        "ANSIBLE_INVENTORY": str(fleet_root / "hosts"),
        "ANSIBLE_FORCE_COLOR": "True",
        "ANSIBLE_SSH_ARGS": "-o ForwardAgent=yes -o ControlMaster=auto -o ControlPersist=60s",
        "ANSIBLE_PIPELINING": "True",
        "ANSIBLE_SSH_RETRIES": "1",
    }
    vault_pass = fleet_root / ".vault_pass"
    if vault_pass.is_file():
        env["ANSIBLE_VAULT_PASSWORD_FILE"] = str(vault_pass)
    return env


@contextmanager
def _fleet_inventory(fleet_root: Path | None) -> Iterator[Path | None]:
    """A directory Ansible can use as ``-i``: the fleet's hosts files and group_vars.

    Ansible loads ``group_vars/`` from the directory of an inventory source,
    and from the directory of the playbook. The playbook is in the framework
    checkout, not in the fleet, and the fleet keeps its hosts files in
    ``hosts/`` and its group_vars one level up. So the run gets a directory of
    links: one per hosts file, plus ``group_vars`` and ``host_vars``.
    """
    if fleet_root is None:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="bay-inventory-") as tmp:
        inventory = Path(tmp)
        hosts = fleet_root / "hosts"
        if hosts.is_dir():
            for entry in sorted(hosts.iterdir()):
                if entry.is_file() and not entry.name.startswith("."):
                    (inventory / entry.name).symlink_to(entry.resolve())
        for name in ("group_vars", "host_vars"):
            source = fleet_root / name
            if source.is_dir():
                (inventory / name).symlink_to(source.resolve())
        yield inventory


PROFILE_CALLBACKS = "ansible.posix.profile_tasks,ansible.posix.timer"


def _profile_env(profile: bool) -> dict[str, str]:
    """Enable the vendored ansible.posix timing callbacks when asked.

    Absent (not empty) without `--profile`, so an operator's own
    ANSIBLE_CALLBACKS_ENABLED is never overwritten by a no-op value.
    """
    if not profile:
        return {}
    return {"ANSIBLE_CALLBACKS_ENABLED": PROFILE_CALLBACKS}


def _mitogen_env(bay_dir: Path) -> dict[str, str]:
    """Enable Mitogen strategy plugin when installed in the bay venv.

    Mitogen replaces Ansible's per-task SSH+Python overhead with a persistent
    remote interpreter. Opt-in by `uv add mitogen` in the framework; opt-out
    by removing it from deps. Set BAY_NO_MITOGEN=1 to disable at runtime.
    """
    import os
    if os.environ.get("BAY_NO_MITOGEN"):
        return {}
    candidates = list(
        (bay_dir / ".venv").glob(
            "lib/python*/site-packages/ansible_mitogen/plugins/strategy"
        )
    )
    if not candidates:
        return {}
    return {
        "ANSIBLE_STRATEGY_PLUGINS": str(candidates[0]),
        "ANSIBLE_STRATEGY": "mitogen_linear",
    }


# Interpreter-only entries in <venv>/bin: their own shebangs are meaningless
# (they are the interpreter, or symlinks to it) and the activate scripts are
# shell, not Python. Everything else is a console script worth inspecting.
_VENV_NON_SCRIPTS = frozenset(
    {"activate", "activate.bat", "activate.csh", "activate.fish", "activate.nu",
     "activate.ps1", "activate_this.py", "deactivate.bat", "pydoc.bat"}
)


def _script_interpreter(script: Path) -> str | None:
    """Return the interpreter path from a script's shebang, or None."""
    try:
        with script.open("rb") as fh:
            first = fh.readline(1024)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    line = first[2:].decode("utf-8", "replace").strip()
    if not line:
        return None
    # `#!/usr/bin/env python` — the env form is relocatable, never stale.
    parts = line.split()
    interp = parts[0]
    if Path(interp).name == "env":
        return None
    return interp


def _stale_venv_reason(venv: Path, bay_dir: Path) -> str | None:
    """Explain why ``venv`` is unusable, or None if it looks fine.

    uv bakes an ABSOLUTE interpreter path into every console-script shebang.
    Moving or renaming the framework directory leaves those shebangs
    pointing at a path that no longer exists. ``uv sync`` does not repair
    them: the package set is correct, so it has nothing to do, while every
    ``uv run ansible-playbook`` dies with "No such file or directory".

    History: the first version of this check probed ``<venv>/bin/pip`` and
    returned early when it was absent. uv-created venvs do not install pip,
    so that early return fired 100% of the time and the shebang comparison
    below was unreachable for the entire life of the guard. Probe scripts
    that actually exist.
    """
    bin_dir = venv / "bin"
    if not bin_dir.is_dir():
        return None

    expected_bin = bin_dir.resolve() if bin_dir.exists() else bin_dir
    try:
        entries = sorted(bin_dir.iterdir())
    except OSError:
        return None

    for entry in entries:
        if entry.name in _VENV_NON_SCRIPTS or entry.name.startswith("python"):
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        interp = _script_interpreter(entry)
        if interp is None:
            continue
        interp_path = Path(interp)
        if not interp_path.is_absolute() or not interp_path.name.startswith("python"):
            continue
        if interp_path.parent != expected_bin and interp_path.parent != bin_dir:
            return f"{entry.name} points at {interp} — directory was moved"

    return None


def _purge_stale_venv(bay_dir: Path) -> None:
    """Remove .venv when its baked-in absolute paths no longer resolve.

    See :func:`_stale_venv_reason`. Removing the directory is the repair —
    the ``uv sync`` that immediately follows recreates it from the lockfile.
    """
    venv = bay_dir / ".venv"
    if not venv.is_dir():
        return
    reason = _stale_venv_reason(venv, bay_dir)
    if reason is None:
        return
    console.warning(f"Stale .venv detected ({reason}) — recreating")
    shutil.rmtree(venv, ignore_errors=True)


def uv_sync(bay_dir: Path) -> None:
    _purge_stale_venv(bay_dir)
    runner.run(
        ["uv", "sync", "--project", str(bay_dir)],
        message="Syncing Python dependencies...",
    )


def galaxy_install_roles(bay_dir: Path) -> None:
    runner.run(
        [
            *_uv_run_cmd(bay_dir),
            "ansible-galaxy", "install",
            "-r", str(bay_dir / "requirements.yml"),
            "-p", str(bay_dir / "vendor" / "roles"),
            "--force",
        ],
        message="Installing Ansible roles...",
    )


def galaxy_install_collections(bay_dir: Path) -> None:
    runner.run(
        [
            *_uv_run_cmd(bay_dir),
            "ansible-galaxy", "collection", "install",
            "-r", str(bay_dir / "requirements.yml"),
            "-p", str(bay_dir / "vendor" / "collections"),
            "--force",
        ],
        message="Installing Ansible collections...",
    )


def sync_deps(bay_dir: Path) -> None:
    """Full dependency sync: uv + Galaxy roles + Galaxy collections."""
    uv_sync(bay_dir)
    galaxy_install_roles(bay_dir)
    galaxy_install_collections(bay_dir)


def run_playbook(
    playbook: str,
    env: str,
    *,
    bay_dir: Path,
    fleet_root: Path | None = None,
    tags: list[str] | None = None,
    extra_args: list[str] | None = None,
    profile: bool = False,
) -> None:
    """Run a framework playbook against a fleet, with live output streaming.

    ``fleet_root`` is the fleet: its hosts files, group_vars and vault password
    are used, wherever the command is run from. Without it (tests), the
    framework's own ansible.cfg and inventory apply.
    """
    with _fleet_inventory(fleet_root) as inventory:
        cmd = [
            *_uv_run_cmd(bay_dir),
            "ansible-playbook", str(bay_dir / f"{playbook}.yml"),
            "-e", f"target_host={env}",
        ]
        if fleet_root is not None:
            cmd.extend(["-e", json.dumps({"bay_fleet_root": str(fleet_root)})])
        if inventory is not None:
            cmd.extend(["-i", str(inventory)])
        if tags:
            cmd.extend(["--tags", ",".join(tags)])
        if extra_args:
            cmd.extend(extra_args)

        mitogen = _mitogen_env(bay_dir)

        # One line, before the playbook starts: which connection strategy is live.
        # Mitogen prints nothing itself, so a purged venv or BAY_NO_MITOGEN=1
        # silently costs ~3 SSH execs per task with no way to notice.
        if not console.is_json_mode():
            strategy = mitogen.get("ANSIBLE_STRATEGY")
            console.info(
                f"strategy: {strategy}" if strategy
                else "strategy: linear (mitogen unavailable)"
            )

        runner.run(
            cmd,
            capture=False,
            cwd=fleet_root,
            env={
                **_collections_env(bay_dir),
                **(fleet_env(bay_dir, fleet_root) if fleet_root is not None else {}),
                **mitogen,
                **_profile_env(profile),
            },
        )


def vault_cmd(
    action: str,
    vault_file: str,
    *,
    bay_dir: Path,
) -> None:
    """Run an ansible-vault command (interactive, streams live)."""
    runner.run(
        [*_uv_run_cmd(bay_dir), "ansible-vault", action, vault_file],
        capture=False,
        env=_collections_env(bay_dir),
    )
