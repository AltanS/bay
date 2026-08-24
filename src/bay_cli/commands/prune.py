"""Operator-facing disk prune for build servers.

Reclaims disk space on a target env without requiring operators to SSH and
run docker commands by hand. By default restricts to the `build_server` host
— where remote-strategy builds accumulate old image layers. Pass
`--target all` to run on every host in the env.

Design notes:
  * `--volumes` is deliberately not exposed. `docker system prune --volumes`
    would delete unused Docker volumes, and on demo that includes the
    postgres data volume whenever the DB is stopped for any reason. The
    underlying ansible ad-hoc call runs `docker system prune -af` (without
    `--volumes`) for exactly this reason.
  * `--dry-run` is strictly read-only: it prints `docker system df` output
    and does NOT prune anything.

Exit codes:
  0 — success
  1 — ansible/SSH failure, or build_server could not be resolved
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import typer

from bay_cli import ansible, console, paths, runner
from bay_cli.errors import BayError

# Mirrors `argo_buildx_builder` in roles/{git_deploy,cronjobs}/defaults/main.yml.  # legacy-argo: live host artifact name, migrate separately
# Ad-hoc shell has no access to Ansible vars, so the name is repeated here;
# tests/test_builder_prune.py pins it equal to the role defaults.
_BUILDER_NAME = "argo-builder"  # legacy-argo: live host artifact name, migrate separately
_KEEP_STORAGE = "2G"

# `docker builder prune` only reaches the DEFAULT builder. git_deploy builds
# with `_BUILDER_NAME` (docker-container driver), whose cache is held in its
# own Docker volume — untouched by `docker builder prune` and unremovable by
# `docker system prune --volumes` while the buildkit container holds it open.
# Pruning only the default builder reclaimed nothing from it, which is how a
# host reached 41 GB of stale cache (GH#34). Prefer the script the cronjobs
# role installs; fall back to an inline sweep on hosts that don't have it
# (non-rig hosts never run the cronjobs role).
#
# `--keep-storage` was renamed `--reserved-space`; the fallback tries the new
# spelling first — an unsupported flag errors out without pruning anything, so
# retrying with the old one is safe.
# buildx builder registrations are PER-USER, and these commands run under
# `--become` (root). git_deploy creates the argo builder as the app user, so  # legacy-argo: live host artifact name, migrate separately
# root's `docker buildx inspect argo-builder` reports "no builder found" even  # legacy-argo: live host artifact name, migrate separately
# though the buildkit container is running and its cache volume is on disk.
# Naming the builder from a root shell is therefore not enough — probe root
# first, then the users below. `app_user` is a consumer var not available to
# ad-hoc shell, so the candidates are the conventional names.
_BUILDER_USERS = "bay argo"  # legacy-argo: pre-1.0 app_user on existing hosts

# Emits `$owner` = the user whose buildx registry knows $b ("" for root), or
# returns non-zero. Shared by the prune and the dry run so they never disagree
# about whether a builder is reachable.
_RESOLVE_OWNER = (
    "resolve_owner() { "
    '  for u in "" ' + _BUILDER_USERS + "; do "
    '    if [ -n "$u" ] && ! id "$u" >/dev/null 2>&1; then continue; fi; '
    '    if [ -z "$u" ]; then '
    '      docker buildx inspect "$1" >/dev/null 2>&1 && { owner=""; return 0; }; '
    "    else "
    '      runuser -u "$u" -- docker buildx inspect "$1" >/dev/null 2>&1 && { owner="$u"; return 0; }; '
    "    fi; "
    "  done; "
    "  return 1; "
    "}; "
    "as_owner() { "
    '  if [ -z "$owner" ]; then "$@"; else runuser -u "$owner" -- "$@"; fi; '
    "}; "
)

# `docker builder prune` only reaches the DEFAULT builder. git_deploy builds
# with `_BUILDER_NAME` (docker-container driver), whose cache is held in its
# own Docker volume — untouched by `docker builder prune` and unremovable by
# `docker system prune --volumes` while the buildkit container holds it open.
# Pruning only the default builder reclaimed nothing from it, which is how a
# host reached 41 GB of stale cache (GH#34). Prefer the script the cronjobs
# role installs; fall back to an inline sweep on hosts that don't have it
# (non-rig hosts never run the cronjobs role).
#
# `--keep-storage` was renamed `--reserved-space`; the fallback tries the new
# spelling first — an unsupported flag errors out without pruning anything, so
# retrying with the old one is safe.
_PRUNE_CMD = (
    "docker system prune -af && "
    "if [ -x /usr/local/bin/bay-docker-builder-prune ]; then "
    "  /usr/local/bin/bay-docker-builder-prune; "
    "elif [ -x /usr/local/bin/argo-docker-builder-prune ]; then "  # legacy-argo: pre-migration host, remove in a future major release
    "  /usr/local/bin/argo-docker-builder-prune; "  # legacy-argo: pre-migration host, remove in a future major release
    "else "
    + _RESOLVE_OWNER
    + f"  for b in default {_BUILDER_NAME}; do "
    '    if ! resolve_owner "$b"; then echo "skip builder=$b (not reachable)"; continue; fi; '
    '    echo "pruning builder=$b as=${owner:-root}"; '
    f'    as_owner docker buildx prune -af --builder "$b" --reserved-space {_KEEP_STORAGE} || '
    f'    as_owner docker buildx prune -af --builder "$b" --keep-storage {_KEEP_STORAGE} || '
    '    echo "prune failed for builder=$b"; '
    "  done; "
    "fi"
)

# `docker system df` reports the default builder's cache only — the number that
# looked healthy while the disk filled. `buildx du` is the honest one, so the
# dry run shows both.
_DF_CMD = (
    "echo '=== df -h / ===' && df -h / && echo && "
    "echo '=== docker system df (default builder only) ===' && docker system df && echo && "
    f"echo '=== docker buildx du --builder {_BUILDER_NAME} ===' && "
    + _RESOLVE_OWNER
    + f'if resolve_owner {_BUILDER_NAME}; then '
    f'  echo "(registered to ${{owner:-root}})"; '
    f'  as_owner docker buildx du --builder {_BUILDER_NAME} | tail -5; '
    f"else echo '({_BUILDER_NAME} not reachable from this host)'; fi"
)


def _resolve_build_server(env: str, bay_dir: Path) -> str:
    """Resolve the value of `build_server` for the env via ansible-inventory.

    Runs `ansible-inventory --list` once and scans hostvars for any host that
    defines `build_server`. Returns that value. If multiple hosts disagree,
    the env is misconfigured; raises BayError with the conflicting values so
    the operator can see the problem.
    """
    uv_cmd = ansible._uv_run_cmd(bay_dir)
    consumer_root = paths.consumer_root(bay_dir)
    inventory = consumer_root / "hosts" / env
    if not inventory.exists():
        raise BayError(
            f"Inventory file not found: {inventory}",
            hint=f"Known envs: {', '.join(p.name for p in (consumer_root / 'hosts').iterdir() if p.is_file())}",
        )

    # Merge with os.environ so uv / other binaries on PATH are still found —
    # ansible._collections_env only returns ANSIBLE_COLLECTIONS_PATH.
    result = subprocess.run(
        [*uv_cmd, "ansible-inventory", "-i", str(inventory), "--list"],
        capture_output=True,
        text=True,
        env={**os.environ, **ansible._collections_env(bay_dir)},
        check=False,
    )
    if result.returncode != 0:
        raise BayError(
            "ansible-inventory failed",
            hint=result.stderr.strip() or "no stderr",
        )

    data = json.loads(result.stdout)
    hostvars = data.get("_meta", {}).get("hostvars", {})
    seen: set[str] = set()
    for vars_dict in hostvars.values():
        if "build_server" in vars_dict:
            seen.add(str(vars_dict["build_server"]))

    if not seen:
        raise BayError(
            "`build_server` is not set in any group_vars/hostvars for this env",
            hint="Add `build_server: <inventory_hostname>` to group_vars/all/main.yml",
        )
    if len(seen) > 1:
        raise BayError(
            f"`build_server` has conflicting values across hosts: {sorted(seen)}",
            hint="All hosts in an env must agree on a single build server",
        )
    return seen.pop()


def _run_ad_hoc(
    env: str,
    cmd: str,
    *,
    bay_dir: Path,
    limit: str | None,
    message: str,
) -> None:
    consumer_root = paths.consumer_root(bay_dir)
    inventory = consumer_root / "hosts" / env
    uv_cmd = ansible._uv_run_cmd(bay_dir)
    # Use `all` as the host pattern — `--limit` then scopes the run. The env
    # group name may not contain the build_server host (e.g. sandbox's
    # `testing` group doesn't include `infra`), so targeting the env group
    # directly can leave zero hosts when combined with --limit.
    full_cmd = [
        *uv_cmd,
        "ansible",
        "all",
        "-i",
        str(inventory),
        "-m",
        "ansible.builtin.shell",
        "-a",
        cmd,
        "--become",
    ]
    if limit:
        full_cmd.extend(["--limit", limit])
    runner.run(
        full_cmd,
        capture=False,
        message=message,
        env=ansible._collections_env(bay_dir),
        check=True,
    )


def prune(
    env: str = typer.Argument(..., help="Target environment (e.g. production, testing)."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show disk usage without pruning. Runs `docker system df`.",
    ),
    target: str = typer.Option(
        "build",
        "--target",
        "-t",
        help="Which hosts to prune: `build` (default, build_server only) or `all`.",
    ),
) -> None:
    """Reclaim disk space by pruning unused Docker images and build cache.

    Default target is `build` — only the `build_server` host is affected,
    since that is where remote-strategy builds accumulate old image layers.
    Use `--target all` to prune every host in the env.

    `--volumes` is intentionally omitted to prevent accidental loss of
    postgres / other service data. If you need to prune volumes, do it by
    hand via `ssh argo-admin@<host> "docker volume prune"`.  # legacy-argo: live host artifact name, migrate separately

    Examples:

        bin/bay prune production --dry-run
        bin/bay prune production
        bin/bay prune production --target all
    """
    if target not in {"build", "all"}:
        raise BayError(
            f"--target must be 'build' or 'all', got: {target!r}",
        )

    bay_dir = paths.find_bay_dir()

    limit: str | None = None
    if target == "build":
        limit = _resolve_build_server(env, bay_dir)
        console.info(f"Targeting build_server: {limit}")
    else:
        console.warning("Targeting ALL hosts in env — use --target build for the common case")

    if dry_run:
        console.info("Dry run — showing disk usage only (no prune will be executed)")
        _run_ad_hoc(
            env,
            _DF_CMD,
            bay_dir=bay_dir,
            limit=limit,
            message="Querying disk usage",
        )
        return

    _run_ad_hoc(
        env,
        _PRUNE_CMD,
        bay_dir=bay_dir,
        limit=limit,
        message=f"Pruning docker images + build cache on {limit or 'all hosts'}",
    )
    console.success("Prune complete")
