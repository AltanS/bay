"""Guards for the scheduled buildkit cache prune (GH#34).

Regression context:
  `roles/cronjobs` scheduled `docker builder prune`, which operates on the
  *default* builder only. `roles/git_deploy` builds with a separate
  docker-container-driver builder (`bay_buildx_builder`), whose cache lives
  in its own Docker volume — invisible to `docker builder prune` and, because
  the buildkit container holds the volume open, also to
  `docker system prune -af --volumes`.

  Net effect: the prune had never reclaimed a byte of the cache the framework
  actually creates. A deployment host reached 41.26 GB of stale buildkit cache
  and tripped an 80% disk alert while `/var/log/docker-prune.log` recorded
  "Total reclaimed space: 0B" week after week. `docker system df` reports only
  the default builder, so the host looked healthy throughout.

These tests pin the three properties that made it silent:
  * the prune targets the builder git_deploy actually uses,
  * the builder name is one shared variable rather than a per-role literal,
  * the deprecated `--keep-storage` spelling is not hardcoded.
"""

from __future__ import annotations

from pathlib import Path

import shlex

import jinja2
import yaml

_REPO_ROOT = Path(__file__).parent.parent
_CRONJOBS = _REPO_ROOT / "roles" / "cronjobs"
_CRONJOBS_DEFAULTS = _CRONJOBS / "defaults" / "main.yml"
_CRONJOBS_TASKS = _CRONJOBS / "tasks" / "main.yml"
_PRUNE_TEMPLATE = _CRONJOBS / "templates" / "bay-docker-builder-prune.sh.j2"
_GIT_DEPLOY_DEFAULTS = _REPO_ROOT / "roles" / "git_deploy" / "defaults" / "main.yml"


def _defaults(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def test_builder_name_is_shared_between_roles() -> None:
    """cronjobs must prune the builder git_deploy builds with. Role defaults
    cannot be shared in Ansible, so the two must be pinned equal here."""
    cronjobs = _defaults(_CRONJOBS_DEFAULTS)["bay_buildx_builder"]
    git_deploy = _defaults(_GIT_DEPLOY_DEFAULTS)["bay_buildx_builder"]
    assert cronjobs == git_deploy, (
        f"bay_buildx_builder differs: cronjobs={cronjobs!r} "
        f"git_deploy={git_deploy!r}. A mismatch means the scheduled prune "
        f"silently reclaims nothing (GH#34)."
    )


def test_git_deploy_builds_with_the_shared_variable() -> None:
    """Every `docker buildx build --builder` call site must use the variable,
    or the prune can be pointed at a builder nothing builds with."""
    sites = [
        _REPO_ROOT / "roles" / "git_deploy" / "tasks" / "build.yml",
        _REPO_ROOT / "roles" / "git_deploy" / "tasks" / "remote_build.yml",
        _REPO_ROOT / "roles" / "git_deploy" / "templates" / "rebuild.sh.j2",
        _REPO_ROOT / "roles" / "git_deploy" / "tasks" / "setup_builder.yml",
    ]
    for path in sites:
        contents = path.read_text()
        assert "--builder argo-builder" not in contents, (  # legacy-argo: live buildx builder default, migrate separately
            f"{path.name} hardcodes the builder name; use "
            f"{{{{ bay_buildx_builder }}}} so cronjobs prunes the same builder."
        )
        assert "--name argo-builder" not in contents, (  # legacy-argo: live buildx builder default, migrate separately
            f"{path.name} creates a hardcoded builder name; use "
            f"{{{{ bay_buildx_builder }}}}."
        )


def test_prune_list_covers_default_and_bay_builders() -> None:
    """Pruning only the bay builder would swap one blind spot for another —
    hosts still accumulate default-builder cache from manual `docker build`."""
    defaults = _defaults(_CRONJOBS_DEFAULTS)
    builders = defaults["docker_prune_builders"]
    assert "default" in builders, "default builder dropped from the prune sweep"
    assert "{{ bay_buildx_builder }}" in builders, (
        "the bay builder must be in docker_prune_builders — this is the "
        "cache that actually grows (GH#34)"
    )


def test_cron_entry_no_longer_prunes_only_the_default_builder() -> None:
    tasks = _CRONJOBS_TASKS.read_text()
    assert "docker builder prune" not in tasks, (
        "`docker builder prune` only touches the default builder — the cron "
        "entry must call the prune script, which sweeps docker_prune_builders."
    )
    assert "/usr/local/bin/bay-docker-builder-prune" in tasks, (
        "cron entry must invoke the rendered prune script"
    )


def test_prune_script_is_not_pinned_to_the_deprecated_flag() -> None:
    """buildx deprecated --keep-storage in favour of --reserved-space. The
    script probes rather than pinning, so it works on both."""
    template = _PRUNE_TEMPLATE.read_text()
    assert "--reserved-space" in template
    assert "--keep-storage" in template, "fallback for older buildx removed"


def _render(**overrides) -> str:
    defaults = _defaults(_CRONJOBS_DEFAULTS)
    builder = overrides.get("bay_buildx_builder", defaults["bay_buildx_builder"])
    ctx = {
        "docker_prune_builder_keep_storage": defaults["docker_prune_builder_keep_storage"],
        # Ansible resolves the nested Jinja in the default list before the
        # template sees it; do the same substitution here.
        "docker_prune_builders": [
            builder if b == "{{ bay_buildx_builder }}" else b
            for b in defaults["docker_prune_builders"]
        ],
        "docker_prune_builder_users": overrides.get("docker_prune_builder_users", ["bay"]),
    }
    ctx.update({k: v for k, v in overrides.items() if k != "bay_buildx_builder"})
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_PRUNE_TEMPLATE.parent)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    # Ansible ships `quote`; a bare Environment does not. The template runs
    # from root cron, so every value in it is shlex-quoted.
    env.filters["quote"] = shlex.quote
    return env.get_template(_PRUNE_TEMPLATE.name).render(**ctx)


def test_rendered_script_sweeps_both_builders() -> None:
    rendered = _render()
    assert "PRUNE_BUILDERS=(default argo-builder )" in rendered, rendered  # legacy-argo: live buildx builder default, migrate separately
    assert "docker buildx prune -af --builder" in rendered
    assert 'KEEP=2G' in rendered


def test_rendered_script_honours_a_renamed_builder() -> None:
    """A consumer overriding bay_buildx_builder in group_vars overrides both
    roles at once — that is the whole point of the shared name."""
    rendered = _render(bay_buildx_builder="custom-builder")
    assert "PRUNE_BUILDERS=(default custom-builder )" in rendered


def test_rendered_script_deduplicates_builders() -> None:
    rendered = _render(docker_prune_builders=["default", "default", "argo-builder"])  # legacy-argo: live buildx builder default, migrate separately
    assert "PRUNE_BUILDERS=(default argo-builder )" in rendered  # legacy-argo: live buildx builder default, migrate separately


# --- buildx registrations are per-user --------------------------------------
#
# Found while verifying the fix on live consumer hosts: the buildkit container
# and its 5.2 GB cache volume were present, but `docker buildx ls` as root
# listed only the default builder. git_deploy creates the builder as `app_user`,
# so it lives in ~bay/.docker/buildx. The cron runs as root — naming the
# builder from root fails with "no builder found", which would have reproduced
# the exact silent no-op this whole fix exists to remove.


def test_prune_looks_in_the_app_users_buildx_registry() -> None:
    users = _defaults(_CRONJOBS_DEFAULTS)["docker_prune_builder_users"]
    assert "{{ app_user | default('') }}" in users, (
        "the builder is registered to app_user, not root — root's cron cannot "
        "see it without switching user"
    )


def test_rendered_script_switches_user_to_reach_the_builder() -> None:
    rendered = _render()
    assert "PRUNE_AS_USERS=(bay )" in rendered
    assert "runuser -u" in rendered, (
        "root cannot inspect or prune a builder registered to another user"
    )


def test_rendered_script_probes_and_prunes_as_the_same_user() -> None:
    """An inspect that succeeds as bay followed by a prune as root would look
    healthy and reclaim nothing — the failure mode being fixed."""
    rendered = _render()
    assert 'run_as "${owner}" docker buildx prune' in rendered
    assert 'run_as "${user}" docker buildx inspect' in rendered


def test_unreachable_builder_is_loud_not_silent() -> None:
    """`docker system df` under-reporting is what hid this for months. A
    builder with a live buildkit container that no user can reach must say so
    in the prune log rather than log a tidy skip."""
    rendered = _render()
    assert "WARNING builder=" in rendered
    assert "is NOT being pruned" in rendered
    assert 'docker inspect "buildx_buildkit_${builder}0"' in rendered


# --- `bin/bay prune`, the manual escape hatch, had the same blind spot ------


def test_cli_prune_builder_name_matches_role_defaults() -> None:
    from bay_cli.commands import prune

    assert prune._BUILDER_NAME == _defaults(_GIT_DEPLOY_DEFAULTS)["bay_buildx_builder"], (
        "bin/bay prune targets a different builder than git_deploy builds "
        "with — it would reclaim nothing (GH#34)."
    )


def test_cli_prune_sweeps_the_bay_builder() -> None:
    from bay_cli.commands import prune

    assert "docker builder prune" not in prune._PRUNE_CMD, (
        "`docker builder prune` reaches the default builder only"
    )
    assert "/usr/local/bin/bay-docker-builder-prune" in prune._PRUNE_CMD, (
        "prefer the script installed by the cronjobs role"
    )
    assert f"for b in default {prune._BUILDER_NAME}" in prune._PRUNE_CMD, (
        "the inline fallback (non-rig hosts) must sweep both builders"
    )


def test_cli_dry_run_shows_the_honest_cache_number() -> None:
    """`docker system df` under-reports — it is what made the 41 GB invisible."""
    from bay_cli.commands import prune

    assert f"docker buildx du --builder {prune._BUILDER_NAME}" in prune._DF_CMD


def test_cli_resolves_the_builder_owner() -> None:
    """`bin/bay prune` runs under --become; the builder is registered to the
    app user, so root alone cannot see it (verified live on a consumer host)."""
    from bay_cli.commands import prune

    for cmd in (prune._PRUNE_CMD, prune._DF_CMD):
        assert "resolve_owner" in cmd
        assert "runuser -u" in cmd
    assert "bay" in prune._BUILDER_USERS


def test_build_server_prunes_the_builder_more_than_once_a_day() -> None:
    """`--reserved-space` binds only at prune time, so the interval IS the cap.

    Measured on a production infra host 2026-08-19: pruned to ~1.8 GB at 02:55, back to
    11 GB by 21:00 the same day. With a once-daily prune the host crossed its
    80% warn line every afternoon and was quietly fixed overnight — a sawtooth
    that pages a human for a problem the cleanup already knew how to solve.

    The cache lives in the builder's own Docker volume, which `docker system
    df` does not report and `docker system prune -af --volumes` cannot touch
    while buildkit holds it open, so nothing else in cronjobs bounds it.
    """
    schedule = _defaults(_CRONJOBS_DEFAULTS)["docker_prune_builder_build_server_schedule"]
    fields = schedule.split()
    assert len(fields) == 5, f"not a 5-field cron expression: {schedule!r}"
    hour = fields[1]
    assert hour != "*", "an every-hour prune would keep every build cold"
    assert "/" in hour or "," in hour, (
        "build servers need the builder cache pruned several times a day, not "
        f"once — got hour field {hour!r} in {schedule!r}"
    )
    if "/" in hour:
        step = int(hour.split("/")[1])
        assert 2 <= step <= 12, (
            f"prune every {step}h is outside the useful band: below 2h keeps "
            "builds permanently cold, above 12h stops bounding the cache"
        )
