"""Ansible operations for bay framework."""

import shutil

from pathlib import Path

from bay_cli import console, runner


def _uv_run_cmd(bay_dir: Path) -> list[str]:
    return ["uv", "run", "--project", str(bay_dir)]


def _collections_env(bay_dir: Path) -> dict[str, str]:
    return {"ANSIBLE_COLLECTIONS_PATH": str(bay_dir / "vendor" / "collections")}


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
    inspected = 0
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
        inspected += 1
        if interp_path.parent != expected_bin and interp_path.parent != bin_dir:
            return f"{entry.name} points at {interp} — directory was moved"

    if inspected == 0:
        # No console script could be read (a stripped or half-built venv).
        # Fall back to pyvenv.cfg's `prompt`, which uv writes from the
        # project name: a venv built before the 1.0 rename still says the
        # pre-1.0 project name while the framework dir is now `.bay`.
        cfg = venv / "pyvenv.cfg"
        try:
            text = cfg.read_text()
        except OSError:
            return None
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip() != "prompt":
                continue
            prompt = value.strip()
            if prompt in ("argo", ".argo") and bay_dir.name != prompt:  # legacy-argo: pre-1.0 project name
                return f"pyvenv.cfg prompt is {prompt!r} — venv predates the 1.0 rename"
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
    tags: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> None:
    """Run an ansible-playbook with live output streaming."""
    cmd = [
        *_uv_run_cmd(bay_dir),
        "ansible-playbook", f"{playbook}.yml",
        "-e", f"target_host={env}",
    ]
    if tags:
        cmd.extend(["--tags", ",".join(tags)])
    if extra_args:
        cmd.extend(extra_args)

    runner.run(
        cmd,
        capture=False,
        env={**_collections_env(bay_dir), **_mitogen_env(bay_dir)},
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
    )
