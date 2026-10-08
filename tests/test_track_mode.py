"""M117/05: track mode, commit-tagged builds and the hold guard (build side).

Three layers:

* ``bay_reconcile.tomlhash``: the canonical hash the CLI compiles into the
  services file and the build side recomputes from the pushed commit.
* ``rebuild.sh``: the rendered script's hold helpers, run in a bash harness
  with a fake ``docker`` and a capturing ``notify_build``.
* The build tasks: every build is tagged and labelled by its 12-character commit.

The CLI side (``bay up`` releases a hold, ``bay rollback`` freezes) lives in
tests/test_plan_up.py and tests/test_rollback_tag_integrity.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bay_cli import compiler
from bay_cli.fleet import load_inputs
from bay_reconcile import tomlhash

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_cb_state_schema import _extract_helper, _helpers_bash  # noqa: E402
from test_rebuild_config import (  # noqa: E402
    _local_service,
    _remote_service_with_token,
    _render_rebuild_sh,
    _render_webhook_config,
)

ROOT = _TESTS_DIR.parent
SRC = ROOT / "src"
FIXTURE = ROOT / "tests" / "fixtures" / "fleet_min"
ROLE_TASKS = ROOT / "roles" / "git_deploy" / "tasks"

TOML = """\
# An app. This comment is not config.
name = "app"
fleet = "demo"
port = 3000

[deploy.production]
domain = "app.example.com"
"""

#: The same document: other comments, other key order, other quoting.
TOML_REWORDED = """\
fleet = 'demo'
name = "app"   # trailing comment
port = 3000

# another comment
[deploy.production]
domain = 'app.example.com'
"""

TOML_CHANGED = TOML.replace("port = 3000", "port = 3001")


# ── the canonical hash ──────────────────────────────────────────────────


def test_bay_toml_hash_canonical_ignores_comments() -> None:
    first = tomlhash.canonical_hash(TOML.encode())
    assert first.startswith("sha256:") and len(first) == len("sha256:") + 64
    assert tomlhash.canonical_hash(TOML_REWORDED.encode()) == first
    assert tomlhash.canonical_hash(TOML_CHANGED.encode()) != first
    # The definition: sha256 of the parsed document as sorted, compact JSON.
    import hashlib
    import tomllib

    canon = json.dumps(tomllib.loads(TOML), sort_keys=True, separators=(",", ":"))
    assert first == "sha256:" + hashlib.sha256(canon.encode()).hexdigest()
    # TOML dates are not JSON; they hash by their ISO text instead of failing.
    dated = tomlhash.canonical_hash(b"when = 2026-10-07\n")
    assert dated == tomlhash.hash_doc({"when": "2026-10-07"})
    with pytest.raises(ValueError):
        tomlhash.canonical_hash(b"name = = 1\n")


def test_reconcile_helper_hash_matches_cli(tmp_path: Path) -> None:
    """The hash `bay compile` writes is what the shipped helper prints for the same file."""
    fleet = tmp_path / "fleet"
    shutil.copytree(FIXTURE, fleet)
    inputs = load_inputs(fleet, checkouts={"shop": fleet / "checkouts" / "shop"})
    data = yaml.safe_load(compiler.compile_fleet(inputs).body())
    compiled = data["services"]["shop"]["build"]["bay_toml_hash"]

    # The box runs the package from <stack_dir>/.reconcile, isolated (-I would
    # drop PYTHONPATH, so -s -E is the closest), with nothing else installed.
    shipped = tmp_path / "reconcile"
    shutil.copytree(SRC / "bay_reconcile", shipped / "bay_reconcile")
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(shipped)}

    def helper(*args: str | Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-s", "-m", "bay_reconcile.tomlhash", *map(str, args)],
            capture_output=True, text=True, env=env, cwd=tmp_path, check=False,
        )

    proc = helper(fleet / "checkouts" / "shop" / "bay.toml")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == compiled
    build_hash = data["services"]["shop"]["build"]["bay_build_hash"]
    proc = helper("--section", "build", fleet / "checkouts" / "shop" / "bay.toml")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == build_hash != compiled

    bad = tmp_path / "bad.toml"
    bad.write_text("name = = 1\n")
    assert helper(bad).returncode == 4
    assert helper(tmp_path / "missing.toml").returncode == 2


BUILD_TOML = TOML + """
[build]
dockerfile = "Dockerfile"

[build.args]
NODE_ENV = "production"
"""


def test_tomlhash_section_build() -> None:
    first = tomlhash.section_hash(BUILD_TOML.encode(), "build")
    assert first.startswith("sha256:") and first != tomlhash.canonical_hash(BUILD_TOML.encode())
    # Config that does not feed the image: the build hash stays.
    for same in (
        BUILD_TOML.replace("port = 3000", "port = 3001"),
        BUILD_TOML.replace('domain = "app.example.com"', 'domain = "other.example.com"'),
        "# a comment\n" + BUILD_TOML,
    ):
        assert tomlhash.section_hash(same.encode(), "build") == first
    # What the image is built from: the build hash moves.
    for other in (
        BUILD_TOML.replace('NODE_ENV = "production"', 'NODE_ENV = "staging"'),
        BUILD_TOML.replace('dockerfile = "Dockerfile"', 'dockerfile = "web.Dockerfile"'),
        BUILD_TOML + '\n[deploy.production.build.args]\nNODE_ENV = "prod"\n',
        BUILD_TOML + '\n[services.worker]\ncommand = "w"\n\n[services.worker.build]\n'
        'target = "worker"\n',
        BUILD_TOML.replace('name = "app"', 'name = "app"\nimage = "ghcr.io/acme/app:1"'),
    ):
        assert tomlhash.section_hash(other.encode(), "build") != first, other
    assert tomlhash.build_inputs({"deploy": {"production": {"domain": "x"}}}) == {}
    with pytest.raises(KeyError):
        tomlhash.section_hash(BUILD_TOML.encode(), "env")
    # The CLI: --section build prints the same hash; an unknown section is a usage error.
    assert tomlhash.main(["--section", "env", "bay.toml"]) == 2


# ── rebuild.sh harness ──────────────────────────────────────────────────


def _harness(
    rendered: str,
    script: str,
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    """Run ``script`` with the hold helpers, the CB helpers, a fake docker and a capture.

    Returns the process, the docker calls and the alerts (``<id> <message>``).
    """
    docker_log = tmp_path / "docker.log"
    alerts = tmp_path / "alerts.log"
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    shipped = tmp_path / "reconcile"
    if not shipped.exists():
        shutil.copytree(SRC / "bay_reconcile", shipped / "bay_reconcile")
    lines = "\n".join(f"{k}={v!r}" for k, v in (env or {}).items())
    preamble = f"""#!/usr/bin/env bash
set -uo pipefail
STATE_DIR={str(state)!r}
STATE_FILE={str(state / "svc.json")!r}
STACK_DIR={str(tmp_path)!r}
CB_MAX_FAILURES=3
SERVICE="svc"
HOSTNAME="testhost"
SHA="0123456789ab"
SHARED_TOML_PATHS=()
CHECKOUT_SERVICES=()
RECONCILE_PYTHONPATH={str(shipped)!r}
FAILED_COMMITS_DIR={str(tmp_path / "failed-commits")!r}
docker() {{ printf '%s\\n' "$*" >> {str(docker_log)!r}; }}
notify_build() {{ printf '%s %s\\n---END---\\n' "$1" "$2" >> {str(alerts)!r}; }}
format_timestamp() {{ echo "Jan 01, 00:00 UTC"; }}
{lines}
"""
    helpers = "\n\n".join(
        [
            _helpers_bash(rendered),
            _extract_helper(rendered, "_hold_reason"),
            _extract_helper(rendered, "_hold_build"),
            _extract_helper(rendered, "_promote_latest"),
            _extract_helper(rendered, "_config_hash_of"),
            _extract_helper(rendered, "_config_only"),
            _extract_helper(rendered, "_running_commit"),
            _extract_helper(rendered, "_previous_commit"),
            _extract_helper(rendered, "_same_commit"),
            _extract_helper(rendered, "_config_only_source"),
            _extract_helper(rendered, "_config_only_push"),
            _extract_helper(rendered, "_forget_failed_build"),
            _extract_helper(rendered, "_clear_failed_commit"),
            _extract_helper(rendered, "_seen_head"),
            _extract_helper(rendered, "_mark_seen"),
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", preamble + "\n" + helpers + "\n" + script],
        capture_output=True, text=True, check=False, cwd=tmp_path,
    )
    calls = docker_log.read_text().splitlines() if docker_log.exists() else []
    sent = [m.strip() for m in alerts.read_text().split("---END---")] if alerts.exists() else []
    return proc, calls, [m for m in sent if m]


@pytest.fixture(scope="module")
def local_sh() -> str:
    return _render_rebuild_sh(_local_service(), ["localapp"], git_deploy_services=["localapp"])


@pytest.fixture(scope="module")
def remote_sh() -> str:
    services = _remote_service_with_token()
    services["animals"]["regions"] = ["eu"]
    return _render_rebuild_sh(
        services,
        ["animals"],
        git_deploy_services=["animals"],
        git_deploy_build_strategy="remote",
        peer_urls={"eu": "https://eu.example.com"},
    )


def _repo(tmp_path: Path, text: str = TOML) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "bay.toml").write_text(text)
    return repo, tomlhash.canonical_hash(TOML.encode())


def test_hold_guard_holds_on_config_change(local_sh: str, tmp_path: Path) -> None:
    repo, pinned = _repo(tmp_path)
    env = {"BAY_TOML_PATH": "bay.toml", "PINNED_TOML_HASH": pinned, "TRACK": "branch", "FROZEN": ""}
    say = f'printf "[%s]" "$(_hold_reason {str(repo)!r})"'

    proc, _, _ = _harness(local_sh, say, tmp_path, env=env)
    assert proc.stdout == "[]", proc.stderr  # same config: the push deploys

    (repo / "bay.toml").write_text(TOML_REWORDED)
    proc, _, _ = _harness(local_sh, say, tmp_path, env=env)
    assert proc.stdout == "[]", "a comment or key-order edit is not a config change"

    (repo / "bay.toml").write_text(TOML_CHANGED)
    proc, _, _ = _harness(local_sh, say, tmp_path, env=env)
    assert proc.stdout == "[config changed: bay.toml differs from the pinned one]"

    (repo / "bay.toml").write_text("name = = 1\n")
    proc, _, _ = _harness(local_sh, say, tmp_path, env=env)
    # an unreadable config holds, never deploys blind
    assert proc.stdout.startswith("[config not checked (")

    (repo / "bay.toml").unlink()
    proc, _, _ = _harness(local_sh, say, tmp_path, env=env)
    assert proc.stdout == "[config changed: bay.toml is gone at this commit]"

    # No pinned hash (an in-fleet project): nothing to compare, the push deploys.
    proc, _, _ = _harness(local_sh, say, tmp_path, env={**env, "PINNED_TOML_HASH": ""})
    assert proc.stdout == "[]"


def test_track_pin_always_holds(local_sh: str, tmp_path: Path) -> None:
    repo, pinned = _repo(tmp_path)
    say = f'printf "[%s]" "$(_hold_reason {str(repo)!r})"'
    env = {"BAY_TOML_PATH": "bay.toml", "PINNED_TOML_HASH": pinned, "FROZEN": ""}
    proc, _, _ = _harness(local_sh, say, tmp_path, env={**env, "TRACK": "pin"})
    assert proc.stdout == "[track = pin: a push only builds, bay up deploys]"
    # pin holds even with no hash to compare
    proc, _, _ = _harness(local_sh, say, tmp_path, env={"TRACK": "pin", "FROZEN": ""})
    assert proc.stdout.startswith("[track = pin")
    # a freeze holds whatever track says
    proc, _, _ = _harness(local_sh, say, tmp_path, env={**env, "TRACK": "branch", "FROZEN": "true"})
    assert proc.stdout.startswith("[frozen by bay rollback")

    # The compiled services file carries the mode into the rendered script.
    pinned_svc = _local_service()
    pinned_svc["localapp"]["build"]["track"] = "pin"
    rendered = _render_rebuild_sh(pinned_svc, ["localapp"], git_deploy_services=["localapp"])
    assert 'TRACK="pin"' in rendered
    assert 'TRACK="branch"' in _render_rebuild_sh(
        _local_service(), ["localapp"], git_deploy_services=["localapp"]
    )


def test_held_build_consumes_trigger_and_skips_breaker(local_sh: str, tmp_path: Path) -> None:
    trigger = tmp_path / "svc.trigger.running"
    trigger.write_text("corr-1\n")
    # A breaker already at 2 of 3: a held build must neither trip nor reset it.
    script = f"""
trap 'rm -f {str(trigger)!r}' EXIT
_record_failure "0000aaaa1111" "Build" "earlier failure" >/dev/null
_record_failure "0000aaaa2222" "Build" "earlier failure" >/dev/null
cp "${{STATE_FILE}}" {str(tmp_path / "before.json")!r}
: > {str(tmp_path / "alerts.log")!r}
_hold_build "config changed: bay.toml differs from the pinned one" "repo/svc:0123456789ab"
echo "not reached"
"""
    proc, calls, alerts = _harness(local_sh, script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "not reached" not in proc.stdout, "a held build ends the run"
    assert not trigger.exists(), "the trigger is consumed"
    before = json.loads((tmp_path / "before.json").read_text())
    after = json.loads((tmp_path / "state" / "svc.json").read_text())
    assert after == before and after["consecutive_failures"] == 2
    assert calls == [], "a held build never touches docker: :latest and the container stay"
    assert len(alerts) == 1 and alerts[0].startswith("build.held ")
    assert "0123456789ab" in alerts[0] and "svc" in alerts[0]
    assert "config changed, run bay up" in alerts[0]


def test_latest_moves_only_when_deploy_proceeds(
    local_sh: str, remote_sh: str, tmp_path: Path
) -> None:
    proc, calls, _ = _harness(local_sh, '_promote_latest "bay-app/svc" "0123456789ab"', tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert [c for c in calls if c.startswith("tag ")] == [
        "tag bay-app/svc:latest bay-app/svc:previous",
        "tag bay-app/svc:0123456789ab bay-app/svc:latest",
    ]

    # Local path: the build tags only the commit; :latest moves after the guard.
    build = local_sh.index("_local_buildx() {")
    guard = local_sh.index('HOLD_REASON=$(_hold_reason "${REPO_DIR}")', build)
    held = local_sh.index('_hold_build "${HOLD_REASON}" "${IMAGE_NAME}:${SHA}"', guard)
    promote = local_sh.index('_promote_latest "${IMAGE_NAME}" "${SHA}"', held)
    stop = local_sh.index('docker stop "${SERVICE}"', promote)
    assert build < guard < held < promote < stop
    body = local_sh[build:guard]
    assert ':latest"' not in body and '-t "${IMAGE_NAME}:${SHA}"' in body

    # Remote path: :latest is pushed only when the guard let the push deploy,
    # and a held build sends no pull signal to the app boxes.
    assert 'PUSH_TAGS=(-t "${IMAGE_REPO}:${SHA}")' in remote_sh
    assert '[[ -z "${HOLD_REASON}" ]] && PUSH_TAGS+=(-t "${IMAGE_REF}")' in remote_sh
    held = remote_sh.index('_hold_build "${HOLD_REASON}" "${IMAGE_REPO}:${SHA}"')
    signal = remote_sh.index("PULL_BODY=$(jq", held)
    assert held < signal


def test_build_tags_image_by_commit12(local_sh: str, remote_sh: str) -> None:
    for rendered in (local_sh, remote_sh):
        assert "SHA=$(git rev-parse --short=12 HEAD)" in rendered
        assert "--short HEAD" not in rendered
        assert '--label "com.bay.commit=${SHA}"' in rendered
        assert '--label "org.opencontainers.image.revision=${SHA}"' in rendered
    assert '-t "${IMAGE_NAME}:${SHA}"' in local_sh
    assert 'PUSH_TAGS=(-t "${IMAGE_REPO}:${SHA}")' in remote_sh

    # The deploy-time builds (git_deploy role) tag and label the same way.
    for name in ("build.yml", "remote_build.yml"):
        text = (ROLE_TASKS / name).read_text()
        assert "com.bay.commit=" in text, name
    assert "--short=12" in (ROLE_TASKS / "build.yml").read_text()


def test_webhook_build_stamps_config_hash(local_sh: str, remote_sh: str, tmp_path: Path) -> None:
    """M116/03: a webhook recreate keeps the config-hash label the last deploy set."""
    # Local path and pull path both pass the carried label to docker run.
    for rendered, image in ((local_sh, '"${IMAGE_NAME}:latest"'), (remote_sh, '"${IMAGE_REF}"')):
        carry = rendered.index('_CARRIED_HASH=$(_config_hash_of "${SERVICE}")')
        stop = rendered.index('docker stop "${SERVICE}"', carry)
        run = rendered.index('"${EXTRA_LABELS[@]}" \\', stop)
        assert carry < stop < run
        assert rendered[run:].split("\n", 2)[1].strip().startswith(image)
        assert 'EXTRA_LABELS+=(-l "com.bay.config-hash=${_CARRIED_HASH}")' in rendered

    fake = '_h=$(_config_hash_of svc); printf "[%s]" "$_h"'
    docker_with = 'docker() { printf "%s" "abc123"; }\n'
    proc, _, _ = _harness(local_sh, docker_with + fake, tmp_path)
    assert proc.stdout == "[abc123]", proc.stderr
    proc, _, _ = _harness(local_sh, 'docker() { printf "<no value>"; }\n' + fake, tmp_path)
    assert proc.stdout == "[]"
    proc, _, _ = _harness(local_sh, "docker() { return 1; }\n" + fake, tmp_path)
    assert proc.stdout == "[]"


# ── config-only push (M117/06) ──────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    )
    return proc.stdout.strip()


def _app_repo(tmp_path: Path) -> tuple[Path, str]:
    """An app repo with code, a bay.toml and a mounted file. Returns it and its first commit."""
    repo = tmp_path / "repo"
    (repo / "conf").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / "app.js").write_text("console.log(1)\n")
    (repo / "bay.toml").write_text(TOML)
    (repo / "conf" / "site.yaml").write_text("site: 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "app")
    return repo, _git(repo, "rev-parse", "--short=12", "HEAD")


def _commit(repo: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "push")
    return _git(repo, "rev-parse", "--short=12", "HEAD")


def _pinned_env(text: str = TOML) -> dict[str, str]:
    """The hold inputs ``bay compile`` writes for a project pinned at ``text``."""
    return {
        "BAY_TOML_PATH": "bay.toml",
        "PINNED_TOML_HASH": tomlhash.canonical_hash(text.encode()),
        "PINNED_BUILD_HASH": tomlhash.section_hash(text.encode(), "build"),
        "TRACK": "branch",
        "FROZEN": "",
    }


def _decide(
    repo: Path,
    label: str,
    *,
    strategy: str = "local",
    pre_head: str = "",
    fake: str | None = None,
) -> str:
    """The decision site as rebuild.sh runs it, with a fake docker that knows ``label``.

    ``pre_head`` is the checkout's HEAD before the pull (``_PREV_HEAD``). The
    default fake docker has every image; ``fake`` replaces it (see ``_docker``).
    """
    log = repo.parent / "docker.log"
    docker = fake or f"""
docker() {{
  printf '%s\\n' "$*" >> {str(log)!r}
  [[ "$1" == "inspect" ]] && printf '%s' {label!r}
  return 0
}}"""
    return docker + f"""
BUILD_STRATEGY={strategy!r}
BAY_TOML_FILES=("conf/site.yaml")
SHA=$(git -C {str(repo)!r} rev-parse --short=12 HEAD)
PREV_COMMIT=$(_previous_commit svc {pre_head!r})
if _config_only {str(repo)!r} "${{PREV_COMMIT}}"; then
  _config_only_push "bay-app/svc" "${{PREV_COMMIT}}" \\
    || _log "config-only push ${{SHA}}: no image known to hold ${{PREV_COMMIT:0:12}}, building"
fi
printf 'BUILD [%s]\\n' "$(_hold_reason {str(repo)!r})"
"""


def _docker(
    log: Path,
    *,
    label: str = "",
    running: str = "",
    local: tuple[str, ...] = (),
    latest: dict | None = None,
    registry: tuple[str, ...] = (),
    registry_latest: str | None = None,
) -> str:
    """A fake docker for the previous-image checks.

    ``label`` is the running container's com.bay.commit, ``running`` its image
    ID (empty: no container). ``local`` and ``registry`` are the refs that
    exist. ``latest`` is the ``docker image inspect`` entry of
    ``bay-app/svc:latest`` (see ``_image``; None: no such image),
    ``registry_latest`` the ``imagetools inspect`` JSON of it.
    """
    def words(refs: tuple[str, ...]) -> str:
        return " ".join(refs) or "-"

    latest_json = json.dumps([latest]) if latest else ""
    return f"""
docker() {{
  printf '%s\\n' "$*" >> {str(log)!r}
  local ref="${{@: -1}}"
  case "$1 $2" in
    "inspect --format")
      case "$3" in
        *com.bay.commit*) printf '%s' {label!r}; return 0 ;;
        *) [[ -n {running!r} ]] || return 1; printf '%s' {running!r}; return 0 ;;
      esac ;;
    "image inspect")
      if [[ "${{ref}}" == "bay-app/svc:latest" ]]; then
        [[ -n {latest_json!r} ]] || return 1
        printf '%s' {latest_json!r}; return 0
      fi
      [[ " {words(local)} " == *" ${{ref}} "* ]]; return ;;
    "manifest inspect")
      [[ " {words(registry)} " == *" ${{ref}} "* ]]; return ;;
    "buildx imagetools")
      if [[ "$3" == "inspect" ]]; then
        [[ "${{ref}}" == "bay-app/svc:latest" && -n {(registry_latest or "")!r} ]] || return 1
        printf '%s' {(registry_latest or "")!r}; return 0
      fi
      return 0 ;;
  esac
  return 0
}}"""


def test_config_only_push_does_not_build(
    local_sh: str, remote_sh: str, tmp_path: Path
) -> None:
    repo, first = _app_repo(tmp_path)
    pushed = _commit(repo, {"bay.toml": TOML_CHANGED, "conf/site.yaml": "site: 2\n"})
    trigger = tmp_path / "svc.trigger.running"
    trigger.write_text("corr-1\n")
    env = _pinned_env()
    script = f"""
trap 'rm -f {str(trigger)!r}' EXIT
_record_failure "0000aaaa1111" "Build" "earlier failure" >/dev/null
cp "${{STATE_FILE}}" {str(tmp_path / "before.json")!r}
: > {str(tmp_path / "alerts.log")!r}
""" + _decide(repo, first)
    proc, _, alerts = _harness(local_sh, script, tmp_path, env=env)
    assert proc.returncode == 0, proc.stderr
    assert f"config-only push {pushed}: run bay up" in proc.stdout
    assert "BUILD" not in proc.stdout, "a config-only push ends the run before the build"
    assert not trigger.exists(), "the trigger is consumed"
    before = json.loads((tmp_path / "before.json").read_text())
    after = json.loads((tmp_path / "state" / "svc.json").read_text())
    assert after == before and after["consecutive_failures"] == 1, "the breaker is untouched"
    assert alerts == [], "no build.held, no other alert"
    calls = (tmp_path / "docker.log").read_text().splitlines()
    assert calls[0].startswith("inspect --format")
    # No build and no :latest move: only the commit tag of the code it already runs.
    assert calls[1:] == [
        f"image inspect bay-app/svc:{first}",
        f"tag bay-app/svc:{first} bay-app/svc:{pushed}",
    ]
    assert not any("build" in c or ":latest" in c for c in calls)

    # The build server path: the same rule, the tag is made in the registry.
    (tmp_path / "docker.log").unlink()
    remote_script = _decide(repo, first, strategy="remote")
    proc, _, alerts = _harness(remote_sh, remote_script, tmp_path, env=env)
    assert f"config-only push {pushed}: run bay up" in proc.stdout and alerts == []
    calls = (tmp_path / "docker.log").read_text().splitlines()
    assert calls[1:] == [
        f"manifest inspect bay-app/svc:{first}",
        f"buildx imagetools create -t bay-app/svc:{pushed} bay-app/svc:{first}",
    ]

    # Both rendered paths decide before the build and before the hold guard.
    site = 'if _config_only "${REPO_DIR}" "${PREV_COMMIT}"; then'
    local = local_sh.index(site)
    assert local_sh.index('SHA=$(git rev-parse --short=12 HEAD)') < local
    assert local < local_sh.index("_local_buildx() {", local)
    assert local < local_sh.index('HOLD_REASON=$(_hold_reason "${REPO_DIR}")', local)
    remote = remote_sh.index(site)
    assert remote < remote_sh.index('HOLD_REASON=$(_hold_reason "${REPO_DIR}")', remote)
    assert remote < remote_sh.index("_remote_buildx() {", remote)
    # Both paths take the previous commit from the label, else from the
    # checkout's HEAD read before the fetch (remote) or the pull (local: as
    # this service last saw the shared checkout, _seen_head; the mark moves
    # right after the pull).
    prev = 'PREV_COMMIT=$(_previous_commit "${SERVICE}" "${_PREV_HEAD}")'
    head = "_PREV_HEAD=$(git rev-parse --short=12 HEAD 2>/dev/null || true)"
    fetch = remote_sh.index("git reset --hard FETCH_HEAD")
    assert remote_sh.index(head) < fetch < remote_sh.index(prev, fetch)
    start = local_sh.index("# ── Local strategy")
    pull = local_sh.index('eval "${PULL_CMD}"', start)
    assert local_sh.count(prev) == 2, "the remote path and the local path"
    seen = local_sh.index('_PREV_HEAD=$(_seen_head "${REPO_DIR}")', start)
    mark = local_sh.index('_mark_seen "${REPO_DIR}"', pull)
    assert start < seen < pull < mark < local_sh.index(prev, start)

    # A directory mount counts for every file under it.
    (repo / "conf" / "extra").mkdir()
    _commit(repo, {"conf/extra/a.yaml": "a\n"})
    dir_script = _decide(repo, first).replace(
        'BAY_TOML_FILES=("conf/site.yaml")', 'BAY_TOML_FILES=("conf")'
    )
    proc, _, _ = _harness(local_sh, dir_script, tmp_path, env=env)
    assert "config-only push" in proc.stdout


def test_push_touching_code_and_toml_builds_and_holds(local_sh: str, tmp_path: Path) -> None:
    repo, first = _app_repo(tmp_path)
    _commit(repo, {"bay.toml": TOML_CHANGED, "app.js": "console.log(2)\n"})
    env = _pinned_env()
    proc, _, alerts = _harness(local_sh, _decide(repo, first), tmp_path, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "config-only push" not in proc.stdout
    # It builds, then the hold guard holds it: the toml hash differs from the pin.
    assert "BUILD [config changed: bay.toml differs from the pinned one]" in proc.stdout
    calls = (tmp_path / "docker.log").read_text().splitlines()
    assert all(c.startswith("inspect") for c in calls), "no tag before the build"

    # Code only, toml unchanged: it builds and deploys as before.
    repo2 = tmp_path / "second"
    repo2.mkdir()
    repo2, first2 = _app_repo(repo2)
    _commit(repo2, {"app.js": "console.log(3)\n"})
    proc, _, _ = _harness(local_sh, _decide(repo2, first2), tmp_path, env=env)
    assert "BUILD []" in proc.stdout and "config-only" not in proc.stdout


def test_build_section_change_is_not_config_only(local_sh: str, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "conf").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / "app.js").write_text("console.log(1)\n")
    (repo / "bay.toml").write_text(BUILD_TOML)
    (repo / "conf" / "site.yaml").write_text("site: 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "app")
    first = _git(repo, "rev-parse", "--short=12", "HEAD")
    env = _pinned_env(BUILD_TOML)

    # Only a build arg changes, in bay.toml alone: not config only. It builds,
    # and the hold guard holds it (new image, bay up releases it).
    _commit(repo, {"bay.toml": BUILD_TOML.replace('"production"', '"staging"', 1)})
    proc, _, _ = _harness(local_sh, _decide(repo, first), tmp_path, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "config-only push" not in proc.stdout
    assert "BUILD [config changed: bay.toml differs from the pinned one]" in proc.stdout

    # The same file list with a non-build edit stays config only.
    repo2 = tmp_path / "second"
    repo2.mkdir()
    repo2, first2 = _app_repo(repo2)
    _commit(repo2, {"bay.toml": TOML_CHANGED})
    proc, _, _ = _harness(local_sh, _decide(repo2, first2), tmp_path, env=_pinned_env())
    assert "config-only push" in proc.stdout
    # No pinned build hash (a services file from before 2.1): never config only.
    proc, _, _ = _harness(
        local_sh, _decide(repo2, first2), tmp_path, env={**_pinned_env(), "PINNED_BUILD_HASH": ""}
    )
    assert "config-only push" not in proc.stdout

    # The compiled hash reaches the rendered script next to PINNED_TOML_HASH.
    svc = _local_service()
    svc["localapp"]["build"].update(
        bay_toml_path="bay.toml",
        bay_toml_hash="sha256:" + "0" * 64,
        bay_build_hash="sha256:" + "1" * 64,
    )
    rendered = _render_rebuild_sh(svc, ["localapp"], git_deploy_services=["localapp"])
    assert f'PINNED_BUILD_HASH="sha256:{"1" * 64}"' in rendered
    assert 'PINNED_BUILD_HASH=""' in _render_rebuild_sh(
        _local_service(), ["localapp"], git_deploy_services=["localapp"]
    )


def test_config_only_rule_needs_a_previous_commit(local_sh: str, tmp_path: Path) -> None:
    repo, first = _app_repo(tmp_path)
    _commit(repo, {"bay.toml": TOML_REWORDED})
    env = _pinned_env()

    def runs(script: str, extra: dict[str, str] | None = None) -> str:
        proc, _, _ = _harness(local_sh, script, tmp_path, env={**env, **(extra or {})})
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    # The previous commit is known: config only.
    assert "config-only push" in runs(_decide(repo, first))
    # No label on the running container (or no container) and no checkout
    # HEAD from before the pull: the normal path.
    assert runs(_decide(repo, "")) == "BUILD []\n"
    assert runs(_decide(repo, "<no value>")) == "BUILD []\n"
    # A previous commit the checkout does not know: the normal path.
    assert runs(_decide(repo, "deadbeef0000")) == "BUILD []\n"
    # Nothing changed (a manual rebuild of the running commit): the normal path.
    head = _git(repo, "rev-parse", "--short=12", "HEAD")
    assert runs(_decide(repo, head)) == "BUILD []\n"
    # The bay.toml lives in the fleet (no BAY_TOML_PATH): never config only.
    assert runs(_decide(repo, first), {"BAY_TOML_PATH": "", "PINNED_TOML_HASH": ""}) == (
        "BUILD []\n"
    )


def test_bay_toml_files_reach_the_rendered_script() -> None:
    svc = _local_service()
    svc["localapp"]["build"].update(
        bay_toml_path="apps/web/bay.toml",
        bay_toml_hash="sha256:" + "0" * 64,
        bay_toml_files=["apps/web/conf/site.yaml", "apps/web/rules"],
    )
    rendered = _render_rebuild_sh(svc, ["localapp"], git_deploy_services=["localapp"])
    assert 'BAY_TOML_FILES=("apps/web/conf/site.yaml" "apps/web/rules")' in rendered
    plain = _render_rebuild_sh(_local_service(), ["localapp"], git_deploy_services=["localapp"])
    assert "BAY_TOML_FILES=()" in plain


# ── the box side of bay up / bay rollback: bay_reconcile.codepin ───────────


class _Images:
    def __init__(self, ids: dict[str, str], pullable: dict[str, str] | None = None) -> None:
        self.ids = dict(ids)
        self.pullable = dict(pullable or {})
        self.pulled: list[str] = []

    def image_id(self, ref: str) -> str | None:
        return self.ids.get(ref)

    def pull(self, ref: str) -> bool:
        self.pulled.append(ref)
        if ref in self.pullable:
            self.ids[ref] = self.pullable[ref]
            return True
        return False

    def tag(self, source: str, target: str) -> None:
        self.ids[target] = self.ids[source]

    def commit_tags(self, repo: str) -> list[str]:
        tags = (r.rsplit(":", 1)[1] for r in self.ids if r.startswith(repo + ":"))
        return sorted(t for t in tags if t not in ("latest", "previous"))


def _pin(images: _Images, targets: dict, spec: dict, capsys) -> tuple[int, dict]:
    from bay_reconcile import codepin

    code = codepin.main(
        ["--targets", json.dumps(targets), "--images", json.dumps(spec)], images=images
    )
    return code, json.loads(capsys.readouterr().out)


def test_codepin_moves_latest_and_refuses_a_missing_strict_image(capsys) -> None:
    a, b, gone = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"
    images = _Images({"app/web:latest": "id-b", f"app/web:{a}": "id-a", f"app/web:{b}": "id-b"})
    spec = {"web": "app/web:latest", "db": "postgres:16"}

    # Same image already: noop.
    code, report = _pin(images, {"web": {"commit": b}}, spec, capsys)
    assert code == 0 and report["moves"][0]["status"] == "noop" and report["changed"] is False

    # A full sha works; a target for a container this box does not run is ignored.
    targets = {"web": {"commit": a + "0000", "strict": True}, "other": {"commit": a}}
    code, report = _pin(images, targets, spec, capsys)
    assert code == 0 and [m["name"] for m in report["moves"]] == ["web"]
    assert report["changed"] is True
    assert images.ids["app/web:latest"] == "id-a" and images.ids["app/web:previous"] == "id-b"

    # Strict and not on the box: exit 1, nothing moves, the commit tags are listed.
    code, report = _pin(images, {"web": {"commit": gone, "strict": True}}, spec, capsys)
    assert code == 1 and report["ok"] is False
    assert f"commit tags on this box: {a}, {b}" in report["error"]
    assert images.ids["app/web:latest"] == "id-a"
    # Not strict: skipped, exit 0 (branch mode keeps what the last push deployed).
    code, report = _pin(images, {"web": {"commit": gone}}, spec, capsys)
    assert code == 0 and report["moves"][0]["status"] == "skipped"

    # A registry image that is not local is pulled by its commit tag first.
    ref = "registry.example.com/app/web"
    reg = _Images({f"{ref}:latest": "id-old"}, {f"{ref}:{a}": "id-a"})
    code, _ = _pin(reg, {"web": {"commit": a, "strict": True}}, {"web": f"{ref}:latest"}, capsys)
    assert code == 0 and reg.pulled == [f"{ref}:{a}"]
    assert reg.ids[f"{ref}:latest"] == "id-a"


def test_codepin_runs_before_the_pass_only_on_a_full_real_deploy() -> None:
    tasks_file = ROOT / "roles" / "container_lifecycle" / "tasks" / "reconcile.yml"
    top = yaml.safe_load(tasks_file.read_text())
    block = next(
        t for t in top if t.get("name") == "Reconcile the containers and record the result"
    )["block"]
    names = [t.get("name") for t in block]
    at = names.index("Point :latest at the pinned commit images")
    assert at < names.index("Run server-side reconciler")
    task = block[at]
    assert task["ansible.builtin.command"]["argv"][:3] == ["python3", "-m", "bay_reconcile.codepin"]
    when = " ".join(task["when"])
    # --check skips a command task by itself; the task must not opt back in.
    assert "bay_code_targets" in when and "check_mode" not in task
    assert "container_lifecycle_only" in when and "bay_reconciler_plan_only" in when
    assert task["failed_when"] == "_codepin_result.rc != 0"
    # rebuild.sh runs as the app user and stamps the receipt: group-writable dir.
    receipt = next(t for t in block if t.get("name") == "Write the deploy receipt")["block"]
    mkdir = next(t for t in receipt if t.get("name") == "Ensure the receipts directory exists")
    assert mkdir["ansible.builtin.file"]["group"] == "docker"
    assert mkdir["ansible.builtin.file"]["mode"] == "0775"


# ── review fixes: :previous, failed builds, the build server's previous commit ──


def test_same_commit_rebuild_keeps_previous(local_sh: str, tmp_path: Path) -> None:
    """S6: when :latest already is the candidate image, :previous stays the rollback target."""
    same = 'docker() { printf "%s\\n" "$*" >> "$DOCKER_LOG"; [[ "$1" == image ]] && printf "sha256:aaa"; return 0; }\n'
    script = f'DOCKER_LOG={str(tmp_path / "docker.log")!r}\n' + same + (
        '_promote_latest "bay-app/svc" "0123456789ab"'
    )
    proc, calls, _ = _harness(local_sh, script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    tags = [c for c in calls if c.startswith("tag ")]
    assert tags == ["tag bay-app/svc:0123456789ab bay-app/svc:latest"], calls

    # Another image: :previous moves as before.
    (tmp_path / "docker.log").unlink()
    other = (
        'docker() { printf "%s\\n" "$*" >> "$DOCKER_LOG"; '
        '[[ "$1" == image ]] && printf "%s" "${@: -1}"; return 0; }\n'
    )
    script = f'DOCKER_LOG={str(tmp_path / "docker.log")!r}\n' + other + (
        '_promote_latest "bay-app/svc" "0123456789ab"'
    )
    proc, calls, _ = _harness(local_sh, script, tmp_path)
    assert [c for c in calls if c.startswith("tag ")] == [
        "tag bay-app/svc:latest bay-app/svc:previous",
        "tag bay-app/svc:0123456789ab bay-app/svc:latest",
    ]
    # The pull path keeps :previous when the pulled image is the running one.
    remote = _render_rebuild_sh(
        _remote_service_with_token(), ["animals"], git_deploy_services=["animals"],
        git_deploy_build_strategy="remote",
    )
    assert '[[ -n "${_PREV_DIGEST}" && "${_PREV_DIGEST}" != "${_PULLED_ID}" ]]' in remote


def test_failed_health_check_untags_and_records_the_commit(
    local_sh: str, remote_sh: str, tmp_path: Path
) -> None:
    """B4: a rolled-back build loses its commit tag and is recorded for codepin."""
    fake = (
        'docker() { printf "%s\\n" "$*" >> "$DOCKER_LOG"; '
        '[[ "$1" == image ]] && printf "sha256:bad"; return 0; }\n'
    )
    script = f'DOCKER_LOG={str(tmp_path / "docker.log")!r}\n' + fake + (
        '_forget_failed_build svc "bay-app/svc:0123456789ab"\n'
        '_forget_failed_build svc ""\n'
    )
    proc, calls, _ = _harness(local_sh, script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "rmi bay-app/svc:0123456789ab" in calls
    record = tmp_path / "failed-commits" / "svc"
    assert record.read_text() == "0123456789ab sha256:bad\n"

    # A later deploy of that commit that passes its health check clears it.
    record.write_text("0123456789ab sha256:bad\nfedcba987654 sha256:old\n")
    proc, _, _ = _harness(
        local_sh, '_clear_failed_commit svc 0123456789ab\n_clear_failed_commit svc "x; rm -rf /"',
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert record.read_text() == "fedcba987654 sha256:old\n"

    # Both health-check rollbacks pass the failed build's commit tag, and a
    # deploy that passes clears the record of its commit.
    assert '_handle_rollback "${SERVICE}" "${IMAGE_NAME}:previous" "${IMAGE_NAME}:latest" "${IMAGE_NAME}:${SHA}"' in local_sh
    assert '"${EXPECTED_REVISION:+${IMAGE_REPO}:${EXPECTED_REVISION}}"' in remote_sh
    assert '_forget_failed_build "${svc}" "${failed_tag}"' in local_sh
    assert local_sh.index('_clear_failed_commit "${SERVICE}" "${SHA}"') > local_sh.index(
        '_handle_rollback "${SERVICE}" "${IMAGE_NAME}:previous"'
    )
    assert '_clear_failed_commit "${SERVICE}" "${EXPECTED_REVISION}"' in remote_sh
    assert 'FAILED_COMMITS_DIR="/var/lib/bay/failed-commits"' in local_sh


def test_codepin_never_promotes_a_failed_build(tmp_path: Path, capsys) -> None:
    """B4: bay up whose WANTED is a commit that failed its health check moves nothing."""
    from bay_reconcile import codepin

    bad, good = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
    failed = tmp_path / "failed"
    failed.mkdir()
    (failed / "web").write_text(f"{bad} sha256:id-bad\n")
    spec = {"web": "app/web:latest"}

    def pin(images: _Images, targets: dict) -> tuple[int, dict]:
        code = codepin.main(
            ["--targets", json.dumps(targets), "--images", json.dumps(spec),
             "--failed-dir", str(failed)],
            images=images,
        )
        return code, json.loads(capsys.readouterr().out)

    # The local tag is gone (rebuild.sh removed it) and the registry would serve it.
    ref = "registry.example.com/app/web"
    reg = _Images({f"{ref}:latest": "id-good"}, {f"{ref}:{bad}": "sha256:id-bad"})
    code = codepin.main(
        ["--targets", json.dumps({"web": {"commit": bad}}),
         "--images", json.dumps({"web": f"{ref}:latest"}), "--failed-dir", str(failed)],
        images=reg,
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["moves"][0]["status"] == "skipped"
    assert "failed its health check" in report["moves"][0]["detail"]
    assert reg.pulled == [] and reg.ids[f"{ref}:latest"] == "id-good"

    # Strict (track = "pin"): the deploy stops.
    images = _Images({"app/web:latest": "sha256:id-good", f"app/web:{good}": "sha256:id-good"})
    code, report = pin(images, {"web": {"commit": bad, "strict": True}})
    assert code == 1 and "failed its health check" in report["error"]

    # Another commit tag on the same image (a config-only push named it): refused too.
    images = _Images({"app/web:latest": "sha256:id-good", "app/web:cccccccccccc": "sha256:id-bad"})
    code, report = pin(images, {"web": {"commit": "cccccccccccc"}})
    assert report["moves"][0]["status"] == "skipped"
    assert images.ids["app/web:latest"] == "sha256:id-good"

    # A healthy commit still moves.
    images = _Images({"app/web:latest": "sha256:id-x", f"app/web:{good}": "sha256:id-good"})
    code, report = pin(images, {"web": {"commit": good}})
    assert code == 0 and report["moves"][0]["status"] == "retag"


def test_previous_commit_is_the_label_else_the_pre_pull_head(
    local_sh: str, remote_sh: str, tmp_path: Path
) -> None:
    """S2: the label wins; without one the checkout's HEAD before the pull; else nothing."""
    say = 'printf "[%s]" "$(_previous_commit svc 0123456789ab)"'
    no_label = 'docker() { [[ "$1" == inspect ]] && printf "<no value>"; return 0; }\n'
    labelled = 'docker() { [[ "$1" == inspect ]] && printf "fedcba987654"; return 0; }\n'
    no_container = 'docker() { return 1; }\n'
    for rendered in (local_sh, remote_sh):
        proc, _, _ = _harness(rendered, labelled + say, tmp_path)
        assert proc.stdout == "[fedcba987654]", proc.stderr
        proc, _, _ = _harness(rendered, no_label + say, tmp_path)
        assert proc.stdout == "[0123456789ab]"
        proc, _, _ = _harness(rendered, no_container + say, tmp_path)
        assert proc.stdout == "[0123456789ab]"
        proc, _, _ = _harness(
            rendered, no_label + 'printf "[%s]" "$(_previous_commit svc "")"', tmp_path
        )
        assert proc.stdout == "[]"


def test_adopt_push_against_new_script_is_config_only(local_sh: str, tmp_path: Path) -> None:
    """B1: the adopt commit adds only bay.toml and its files; the script bay up wrote sees
    a config-only push: it tags the running image with the new commit and exits 0."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / "app.js").write_text("console.log(1)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "app")
    running = _git(repo, "rev-parse", "--short=12", "HEAD")
    # The adopt commit: bay.toml and the mounted file beside it, nothing else.
    (repo / "conf").mkdir()
    adopt = _commit(repo, {"bay.toml": TOML, "conf/site.yaml": "site: 1\n"})

    trigger = tmp_path / "svc.trigger.running"
    trigger.write_text("corr-1\n")
    script = f"trap 'rm -f {str(trigger)!r}' EXIT\n" + _decide(repo, running)
    proc, _, alerts = _harness(local_sh, script, tmp_path, env=_pinned_env())
    assert proc.returncode == 0, proc.stderr
    assert f"config-only push {adopt}: run bay up" in proc.stdout
    assert "BUILD" not in proc.stdout and alerts == []
    calls = (tmp_path / "docker.log").read_text().splitlines()
    assert calls[1:] == [
        f"image inspect bay-app/svc:{running}",
        f"tag bay-app/svc:{running} bay-app/svc:{adopt}",
    ]

    # The script from before the adopt (no BAY_TOML_PATH) would build it.
    old = {"BAY_TOML_PATH": "", "PINNED_TOML_HASH": "", "PINNED_BUILD_HASH": "",
           "TRACK": "branch", "FROZEN": ""}
    proc, _, _ = _harness(local_sh, _decide(repo, running), tmp_path, env=old)
    assert "config-only push" not in proc.stdout and "BUILD []" in proc.stdout


# ── containers from before 2.1.0: no label, no commit tag ──────────────

#: The config-only exit, and the log line of a config-only check that found no image.
DONE = ": run bay up"
REFUSED = "no image known to hold"


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    calls = log.read_text().splitlines() if log.exists() else []
    log.unlink(missing_ok=True)
    return calls


def _image(image_id: str, commit: str = "", revision: str = "") -> dict:
    """A ``docker image inspect`` entry. No label at all: no ``Labels`` key."""
    labels = {}
    if commit:
        labels["com.bay.commit"] = commit
    if revision:
        labels["org.opencontainers.image.revision"] = revision
    config: dict = {"Env": ["PATH=/usr/bin"]}
    if labels:
        config["Labels"] = labels
    return {"Id": image_id, "Config": config}


def test_config_only_push_falls_back_to_pre_pull_head_without_label(
    local_sh: str, remote_sh: str, tmp_path: Path
) -> None:
    """A container built before 2.1.0 has no com.bay.commit label. The checkout's
    HEAD before the pull is the previous commit, so the adopt push is config only."""
    repo, first = _app_repo(tmp_path)
    pushed = _commit(repo, {"bay.toml": TOML_CHANGED, "conf/site.yaml": "site: 2\n"})
    log = tmp_path / "docker.log"
    env = _pinned_env()

    fake = _docker(log, label="<no value>", running="sha256:old", local=(f"bay-app/svc:{first}",))
    proc, _, alerts = _harness(
        local_sh, _decide(repo, "", pre_head=first, fake=fake), tmp_path, env=env
    )
    assert proc.returncode == 0, proc.stderr
    assert f"config-only push {pushed}{DONE}" in proc.stdout and alerts == []
    assert "BUILD" not in proc.stdout
    assert _calls(tmp_path)[1:] == [
        f"image inspect bay-app/svc:{first}",
        f"tag bay-app/svc:{first} bay-app/svc:{pushed}",
    ]

    # The build server: the same fallback, the image found in the registry.
    fake = _docker(log, label="", registry=(f"bay-app/svc:{first}",))
    proc, _, _ = _harness(
        remote_sh, _decide(repo, "", strategy="remote", pre_head=first, fake=fake),
        tmp_path, env=env,
    )
    assert f"config-only push {pushed}{DONE}" in proc.stdout
    assert _calls(tmp_path)[1:] == [
        f"manifest inspect bay-app/svc:{first}",
        f"buildx imagetools create -t bay-app/svc:{pushed} bay-app/svc:{first}",
    ]

    # The label still wins over the checkout: the running commit differs from
    # the pushed one in code, so it builds, though the checkout HEAD alone
    # would make the push config only.
    code = _commit(repo, {"app.js": "console.log(2)\n"})
    _commit(repo, {"bay.toml": TOML})
    tags = (f"bay-app/svc:{first}", f"bay-app/svc:{code}")
    fake = _docker(log, label=first, running="sha256:old", local=tags)
    proc, _, _ = _harness(local_sh, _decide(repo, "", pre_head=code, fake=fake), tmp_path, env=env)
    assert DONE not in proc.stdout and "BUILD [" in proc.stdout
    fake = _docker(log, label="", running="sha256:old", local=tags)
    proc, _, _ = _harness(local_sh, _decide(repo, "", pre_head=code, fake=fake), tmp_path, env=env)
    assert DONE in proc.stdout
    # A checkout HEAD before the code commit: the diff has code, so it builds.
    proc, _, _ = _harness(
        local_sh, _decide(repo, "", pre_head=pushed, fake=fake), tmp_path, env=env
    )
    assert DONE not in proc.stdout and "BUILD [" in proc.stdout


def test_config_only_push_tags_from_latest_when_no_commit_tag(
    local_sh: str, remote_sh: str, tmp_path: Path
) -> None:
    """A build from before 2.1.0 has only :latest. It names the pushed commit and gets
    <image>:<prev12> too, but only when :latest provably holds the previous commit."""
    repo, first = _app_repo(tmp_path)
    pushed = _commit(repo, {"bay.toml": TOML_CHANGED})
    log = tmp_path / "docker.log"
    env = _pinned_env()

    def local(**kw: object) -> str:
        args: dict = {"label": "", "running": "sha256:old", "latest": _image("sha256:old")}
        args.update(kw)
        proc, _, _ = _harness(
            local_sh, _decide(repo, "", pre_head=first, fake=_docker(log, **args)),
            tmp_path, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    # No label anywhere, the breaker clean, the container runs :latest: config only.
    out = local()
    assert f"config-only push {pushed}{DONE}" in out and REFUSED not in out
    calls = _calls(tmp_path)
    assert [c for c in calls if c.startswith("tag ")] == [
        f"tag sha256:old bay-app/svc:{pushed}",
        f"tag sha256:old bay-app/svc:{first}",
    ]
    assert not any("build" in c for c in calls)

    # :latest labelled with the previous commit (a 2.1 build whose commit tag is
    # gone, or the revision label of an older build): config only, even with a
    # failure on the breaker or another image running.
    _harness(local_sh, '_record_failure "aaaaaaaaaaaa" "Build" "x" >/dev/null', tmp_path)
    assert DONE in local(latest=_image("sha256:old", first), running="sha256:other")
    assert DONE in local(latest=_image("sha256:old", revision=first[:7]), running="sha256:other")
    _calls(tmp_path)

    # A failure since the last good build: the checkout may be past :latest.
    out = local()
    assert REFUSED in out and DONE not in out and "BUILD [" in out
    assert not [c for c in _calls(tmp_path) if c.startswith("tag ")]
    (tmp_path / "state" / "svc.json").unlink()
    # :latest labelled with another commit.
    assert REFUSED in local(latest=_image("sha256:old", "bbbbbbbbbbbb"))
    # The container runs another image than :latest.
    assert REFUSED in local(running="sha256:other")
    # The commit, or the :latest image, failed its health check on this box.
    record = tmp_path / "failed-commits" / "svc"
    record.parent.mkdir(exist_ok=True)
    record.write_text(f"{first} sha256:bad\n")
    assert REFUSED in local()
    record.write_text("cccccccccccc sha256:old\n")
    assert REFUSED in local()
    record.unlink()
    assert not [c for c in _calls(tmp_path) if c.startswith("tag ")]
    # No container at all: :latest is all there is.
    assert DONE in local(running="")
    _calls(tmp_path)

    # The build server: :latest in the registry, checked by its revision label.
    def remote(image: dict) -> str:
        fake = _docker(log, registry_latest=json.dumps(image))
        proc, _, _ = _harness(
            remote_sh, _decide(repo, "", strategy="remote", pre_head=first, fake=fake),
            tmp_path, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    rev = {"config": {"Labels": {"org.opencontainers.image.revision": first}}}
    assert f"config-only push {pushed}{DONE}" in remote(rev)
    assert _calls(tmp_path)[-1] == (
        f"buildx imagetools create -t bay-app/svc:{pushed} -t bay-app/svc:{first} "
        "bay-app/svc:latest"
    )
    # A multi-platform index (provenance attestations) nests it by platform.
    assert DONE in remote({"linux/amd64": rev})
    _calls(tmp_path)
    # Another commit's image (the build of the previous commit failed), or no label.
    assert REFUSED in remote({"config": {"Labels": {"com.bay.commit": "bbbbbbbbbbbb"}}})
    assert REFUSED in remote({"config": {"Env": []}})
    assert not [c for c in _calls(tmp_path) if "create" in c]


def test_no_previous_commit_and_no_latest_is_a_normal_build(
    local_sh: str, remote_sh: str, tmp_path: Path
) -> None:
    repo, first = _app_repo(tmp_path)
    _commit(repo, {"bay.toml": TOML_CHANGED})
    log = tmp_path / "docker.log"
    env = _pinned_env()
    hold = "BUILD [config changed: bay.toml differs from the pinned one]"

    # A previous commit, but neither its tag nor :latest exists.
    fake = _docker(log, label="", running="sha256:old")
    proc, _, alerts = _harness(
        local_sh, _decide(repo, "", pre_head=first, fake=fake), tmp_path, env=env
    )
    assert proc.returncode == 0, proc.stderr
    assert REFUSED in proc.stdout and DONE not in proc.stdout and hold in proc.stdout
    assert not [c for c in _calls(tmp_path) if c.startswith("tag ")]
    proc, _, _ = _harness(
        remote_sh, _decide(repo, "", strategy="remote", pre_head=first, fake=_docker(log)),
        tmp_path, env=env,
    )
    assert REFUSED in proc.stdout and DONE not in proc.stdout and hold in proc.stdout
    assert not [c for c in _calls(tmp_path) if "create" in c]

    # No label and no checkout HEAD from before the pull: no previous commit.
    fake = _docker(log, label="", latest=_image("sha256:old"), local=(f"bay-app/svc:{first}",))
    proc, _, _ = _harness(local_sh, _decide(repo, "", fake=fake), tmp_path, env=env)
    assert "config-only" not in proc.stdout and hold in proc.stdout
    assert all(c.startswith("inspect --format") for c in _calls(tmp_path))

    # A checkout HEAD the clone does not know (a force push): not config only.
    proc, _, _ = _harness(
        local_sh, _decide(repo, "", pre_head="deadbeef0000", fake=fake), tmp_path, env=env
    )
    assert "config-only" not in proc.stdout and hold in proc.stdout


# ── two projects, one app repo (M119/02, gap 30) ───────────────────────

SHARED_REPO = "git@github.com:acmecorp/shop.git"

#: Two projects in one repo, each with its own bay.toml under bay/.
WEB_TOML = """\
name = "web"
fleet = "demo"
port = 3000

[build]
dockerfile = "apps/web/Dockerfile"

[deploy.production]
domain = "web.example.com"
"""
ADMIN_TOML = WEB_TOML.replace('"web"', '"admin"').replace("apps/web", "apps/admin").replace(
    "web.example.com", "admin.example.com"
)

#: The config keys of one service, as its block of the rendered rebuild.sh sets them.
_CONFIG_KEYS = (
    "BUILD_STRATEGY", "IMAGE_NAME", "IMAGE_REPO", "TRACK", "FROZEN", "BAY_TOML_PATH",
    "PINNED_TOML_HASH", "PINNED_BUILD_HASH", "BAY_TOML_FILES", "SHARED_TOML_PATHS",
    "CHECKOUT_SERVICES",
)


def _shared_services(strategy: str) -> dict:
    """web and admin build from one repo and branch; two controls do not share it."""

    def build(path: str, text: str, **extra: object) -> dict:
        return {
            "repo": SHARED_REPO,
            "branch": "main",
            "strategy": strategy,
            "bay_toml_path": path,
            "bay_toml_hash": tomlhash.canonical_hash(text.encode()),
            "bay_build_hash": tomlhash.section_hash(text.encode(), "build"),
            **extra,
        }

    def svc(name: str, b: dict) -> dict:
        out = {
            "build": b, "access": "public", "domains": [f"{name}.example.com"],
            "ports": {"internal": 3000},
        }
        if strategy == "remote":
            out["image"] = f"zot.example.com/demo/{name}:latest"
        return out

    return {
        "web": svc("web", build("bay/web.toml", WEB_TOML)),
        "admin": svc(
            "admin", build("bay/admin.toml", ADMIN_TOML, bay_toml_files=["apps/admin/conf"])
        ),
        # The same repo on another branch: another checkout, another history.
        "web-next": svc("web-next", {**build("bay/web-next.toml", WEB_TOML), "branch": "next"}),
        # Another repo with the same toml path.
        "blog": svc(
            "blog",
            {**build("bay/web.toml", WEB_TOML), "repo": "git@github.com:acmecorp/blog.git"},
        ),
    }


def _service_config(rendered: str, name: str) -> str:
    """The config lines of ``name``'s block in the rendered script, ready to run."""
    start = rendered.index(f"if [[ \"${{SERVICE}}\" == '{name}' ]]; then")
    end = rendered.index("if [[ \"${SERVICE}\" == '", start + 10)
    keys = "|".join(_CONFIG_KEYS)
    lines = re.findall(rf"^  (?:{keys})=.*$", rendered[start:end], flags=re.MULTILINE)
    assert len(lines) == len(_CONFIG_KEYS), lines
    return "\n".join(line.strip() for line in lines)


def _shared_repo(tmp_path: Path) -> tuple[Path, str]:
    """The app repo (the "remote") with both projects. Returns it and its first commit."""
    origin = tmp_path / "origin"
    (origin / "apps" / "web").mkdir(parents=True)
    (origin / "apps" / "admin" / "conf").mkdir(parents=True)
    (origin / "bay").mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(origin))
    (origin / "apps" / "web" / "main.js").write_text("web(1)\n")
    (origin / "apps" / "admin" / "main.js").write_text("admin(1)\n")
    (origin / "apps" / "admin" / "conf" / "site.yaml").write_text("site: 1\n")
    (origin / "bay" / "web.toml").write_text(WEB_TOML)
    (origin / "bay" / "admin.toml").write_text(ADMIN_TOML)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "app")
    return origin, _git(origin, "rev-parse", "--short=12", "HEAD")


def _registry_docker(log: Path, refs: Path) -> str:
    """A fake docker: no container, unlabeled; ``refs`` lists the images that exist.

    ``docker tag`` and ``imagetools create`` add their targets to ``refs``.
    """
    return f"""
docker() {{
  printf '%s\\n' "$*" >> {str(log)!r}
  local ref="${{@: -1}}"
  case "$1 $2" in
    "inspect --format") return 1 ;;
    "image inspect"|"manifest inspect") grep -qxF -- "${{ref}}" {str(refs)!r}; return ;;
    "tag "*) printf '%s\\n' "$3" >> {str(refs)!r}; return 0 ;;
    "buildx imagetools")
      [[ "$3" == "create" ]] || return 1
      shift 3
      while [[ "$1" == "-t" ]]; do printf '%s\\n' "$2" >> {str(refs)!r}; shift 2; done
      return 0 ;;
  esac
  return 0
}}"""


def _run_push(
    rendered: str, name: str, checkout: Path, tmp_path: Path, *, extra: str = ""
) -> subprocess.CompletedProcess[str]:
    """One run of ``name`` for a push, as rebuild.sh does it: the config of the
    rendered block, the strategy's fetch or pull, the config-only decision site."""
    git_env = "export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1"
    log, refs = tmp_path / "docker.log", tmp_path / "refs"
    script = f"""
{git_env}
{_registry_docker(log, refs)}
SERVICE={name!r}
{_service_config(rendered, name)}
{extra}
cd {str(checkout)!r}
if [[ "${{BUILD_STRATEGY}}" == "remote" ]]; then
  _PREV_HEAD=$(git rev-parse --short=12 HEAD 2>/dev/null || true)
  git fetch -q origin main && git reset -q --hard FETCH_HEAD
  REPO="${{IMAGE_REPO}}"
else
  _PREV_HEAD=$(_seen_head "${{PWD}}")
  git pull -q --ff-only origin main
  _mark_seen "${{PWD}}"
  REPO="${{IMAGE_NAME}}"
fi
SHA=$(git rev-parse --short=12 HEAD)
PREV_COMMIT=$(_previous_commit "${{SERVICE}}" "${{_PREV_HEAD}}")
if _config_only "${{PWD}}" "${{PREV_COMMIT}}"; then
  _config_only_push "${{REPO}}" "${{PREV_COMMIT}}" \\
    || _log "config-only push ${{SHA}}: no image known to hold ${{PREV_COMMIT:0:12}}, building"
fi
printf 'BUILD %s\\n' "${{SERVICE}}"
"""
    proc, _, alerts = _harness(rendered, script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert alerts == [], "a config-only push sends no alert"
    return proc


def test_config_only_push_tags_every_shared_repo_image(tmp_path: Path) -> None:
    """Gap 30: a push that changes only one project's bay.toml in a repo two projects
    build from tags the image of BOTH with the pushed commit, local and remote: no
    build, no recreate. The case that showed it: the adopt commit of the second project."""
    local_sh = _render_rebuild_sh(
        _shared_services("local"), ["web", "admin", "web-next", "blog"],
        git_deploy_services=["web", "admin", "web-next", "blog"],
    )
    remote_sh = _render_rebuild_sh(
        _shared_services("remote"), ["web", "admin", "web-next", "blog"],
        git_deploy_services=["web", "admin", "web-next", "blog"],
        git_deploy_build_strategy="remote",
    )

    # ── what the render gives each service ──
    for rendered in (local_sh, remote_sh):
        assert 'SHARED_TOML_PATHS=("bay/admin.toml")' in _service_config(rendered, "web")
        assert 'SHARED_TOML_PATHS=("bay/web.toml")' in _service_config(rendered, "admin")
        # Another branch or another repo shares nothing, whatever its toml path.
        assert "SHARED_TOML_PATHS=()" in _service_config(rendered, "web-next")
        assert "SHARED_TOML_PATHS=()" in _service_config(rendered, "blog")
    # Local builds share one checkout per repo and branch; remote ones have one each.
    assert 'CHECKOUT_SERVICES=("web" "admin")' in _service_config(local_sh, "web")
    assert 'CHECKOUT_SERVICES=("web-next")' in _service_config(local_sh, "web-next")
    assert "CHECKOUT_SERVICES=()" in _service_config(remote_sh, "web")
    # The local path reads this service's mark before the pull and moves it after.
    pull = local_sh.index('eval "${PULL_CMD}"', local_sh.index("# ── Local strategy"))
    assert local_sh.rindex('_PREV_HEAD=$(_seen_head "${REPO_DIR}")', 0, pull) < pull
    assert local_sh.index('_mark_seen "${REPO_DIR}"', pull) < local_sh.index("_config_only \"", pull)
    # A project whose bay.toml lives in the fleet has no config-only rule: nothing shared.
    in_fleet = _shared_services("local")
    for key in ("bay_toml_path", "bay_toml_hash", "bay_build_hash"):
        del in_fleet["web"]["build"][key]
    plain = _render_rebuild_sh(in_fleet, ["web", "admin"], git_deploy_services=["web", "admin"])
    assert "SHARED_TOML_PATHS=()" in _service_config(plain, "web")
    assert 'SHARED_TOML_PATHS=()' in _service_config(plain, "admin")

    # ── the receiver passes the sibling's bay.toml push for both ──
    webhook_dir = str(ROOT / "roles" / "git_deploy" / "files" / "webhook")
    if webhook_dir not in sys.path:
        sys.path.insert(0, webhook_dir)
    from app import push_filter

    config = _render_webhook_config(_shared_services("remote"))
    assert config["web"]["shared_toml_paths"] == ["bay/admin.toml"]
    assert config["admin"]["shared_toml_paths"] == ["bay/web.toml"]
    assert "shared_toml_paths" not in config["blog"]
    assert "shared_toml_paths" not in config["web-next"]
    watched = {"paths": {"include": ["apps/web/**"]}}
    assert push_filter({"bay/admin.toml"}, {**config["web"], **watched}) == (
        True, "config file changed: bay/admin.toml"
    )
    # The sibling's bay.toml with the sibling's code is a build for the sibling only:
    # outside web's watch the push is skipped for web, as in 2.3.0 (rebuild.sh would not
    # call it config only and would build and recreate web).
    sibling_code = {"bay/admin.toml", "apps/admin/main.py"}
    ok, reason = push_filter(sibling_code, {**config["web"], **watched})
    assert ok is False, reason
    # With code inside web's watch, web builds anyway.
    assert push_filter({"bay/admin.toml", "apps/web/main.py"}, {**config["web"], **watched})[0]
    # The sibling's bay.toml with web's own bay.toml or a file its mounts read: config only.
    own = {**config["web"], **watched, "bay_toml_path": "bay/web.toml"}
    assert push_filter({"bay/admin.toml", "bay/web.toml", "apps/admin/main.py"}, own)[0]
    # No watch on web: every push builds web, so the mixed push passes (as in 2.3.0).
    assert push_filter(sibling_code, config["web"])[0] is True
    # Before 2.4.0 (no shared paths) `watch` dropped it: the second adopt commit.
    bare = {k: v for k, v in config["web"].items() if k != "shared_toml_paths"}
    assert push_filter({"bay/admin.toml"}, {**bare, **watched})[0] is False
    # Another project's mounted file is no config of web: `watch` decides.
    assert push_filter({"apps/admin/conf/site.yaml"}, {**config["web"], **watched})[0] is False

    # ── local: one shared checkout, containers without a com.bay.commit label ──
    origin, first = _shared_repo(tmp_path)
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    refs = tmp_path / "refs"
    refs.write_text(f"bay-teststack-web:{first}\nbay-teststack-admin:{first}\n")
    # The push: the adopt commit of admin, its bay.toml only (no build change).
    pushed = _commit(origin, {"bay/admin.toml": ADMIN_TOML.replace("admin.example", "adm.example")})

    proc = _run_push(local_sh, "web", checkout, tmp_path)
    assert f"config-only push {pushed}: run bay up" in proc.stdout and "BUILD" not in proc.stdout
    # web ran first and pulled the shared checkout; admin still finds its own
    # previous commit (its mark), not the HEAD web's pull left.
    proc = _run_push(local_sh, "admin", checkout, tmp_path)
    assert f"config-only push {pushed}: run bay up" in proc.stdout and "BUILD" not in proc.stdout
    calls = _calls(tmp_path)
    assert [c for c in calls if c.startswith("tag ")] == [
        f"tag bay-teststack-web:{first} bay-teststack-web:{pushed}",
        f"tag bay-teststack-admin:{first} bay-teststack-admin:{pushed}",
    ]
    assert not any("build" in c or ":latest" in c for c in calls), "no build, no :latest move"
    marks = _git(checkout, "for-each-ref", "--format=%(refname) %(objectname:short=12)", "refs/bay")
    assert marks.splitlines() == [f"refs/bay/seen/admin {pushed}", f"refs/bay/seen/web {pushed}"]

    # Controls. Without the mark, admin reads the HEAD web's pull left: it sees
    # no change and builds. Without the shared path, web builds.
    nxt = _commit(origin, {"bay/admin.toml": ADMIN_TOML})
    _run_push(local_sh, "web", checkout, tmp_path)
    _git(checkout, "update-ref", "-d", "refs/bay/seen/admin")
    proc = _run_push(local_sh, "admin", checkout, tmp_path)
    assert "BUILD admin" in proc.stdout and "config-only" not in proc.stdout
    nxt = _commit(origin, {"bay/admin.toml": ADMIN_TOML.replace("admin.example", "a.example")})
    proc = _run_push(local_sh, "web", checkout, tmp_path, extra="SHARED_TOML_PATHS=()")
    assert "BUILD web" in proc.stdout and f"bay-teststack-web:{nxt}" not in refs.read_text()
    _calls(tmp_path)

    # ── remote: one checkout per service on the build server, tags in the registry ──
    tmp_r = tmp_path / "remote"
    tmp_r.mkdir()
    origin, first = _shared_repo(tmp_r)
    for name in ("web", "admin"):
        _git(tmp_r, "clone", "-q", str(origin), str(tmp_r / name))
    refs = tmp_r / "refs"
    refs.write_text(
        f"zot.example.com/demo/web:{first}\nzot.example.com/demo/admin:{first}\n"
    )
    pushed = _commit(origin, {"bay/admin.toml": ADMIN_TOML.replace("admin.example", "adm.example")})
    for name in ("web", "admin"):
        proc = _run_push(remote_sh, name, tmp_r / name, tmp_r)
        assert f"config-only push {pushed}: run bay up" in proc.stdout, proc.stdout
    calls = _calls(tmp_r)
    assert [c for c in calls if "imagetools" in c] == [
        f"buildx imagetools create -t zot.example.com/demo/web:{pushed} "
        f"zot.example.com/demo/web:{first}",
        f"buildx imagetools create -t zot.example.com/demo/admin:{pushed} "
        f"zot.example.com/demo/admin:{first}",
    ]
    assert not [c for c in calls if c.startswith("tag ")], "the build server tags the registry"

    # A [build] change of admin: web still only gets the tag; admin builds.
    admin_build = ADMIN_TOML.replace("apps/admin/Dockerfile", "apps/admin/Dockerfile.new")
    built = _commit(origin, {"bay/admin.toml": admin_build})
    proc = _run_push(remote_sh, "web", tmp_r / "web", tmp_r)
    assert f"config-only push {built}: run bay up" in proc.stdout
    proc = _run_push(remote_sh, "admin", tmp_r / "admin", tmp_r)
    assert "BUILD admin" in proc.stdout and "config-only" not in proc.stdout
    # admin's mounted file: config only for admin, a build input for web (it builds).
    mounted = _commit(origin, {"apps/admin/conf/site.yaml": "site: 2\n"})
    proc = _run_push(remote_sh, "web", tmp_r / "web", tmp_r)
    assert "BUILD web" in proc.stdout and "config-only" not in proc.stdout
    # (bay up has pinned the [build] change and the build of it is in the registry.)
    refs.write_text(refs.read_text() + f"zot.example.com/demo/admin:{built}\n")
    repinned = tomlhash.section_hash(admin_build.encode(), "build")
    proc = _run_push(
        remote_sh, "admin", tmp_r / "admin", tmp_r, extra=f"PINNED_BUILD_HASH={repinned!r}"
    )
    assert f"config-only push {mounted}: run bay up" in proc.stdout
