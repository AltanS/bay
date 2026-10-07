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
            _extract_helper(rendered, "_config_only_push"),
            _extract_helper(rendered, "_forget_failed_build"),
            _extract_helper(rendered, "_clear_failed_commit"),
            _extract_helper(rendered, "_prev_commit_remote"),
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


def _decide(repo: Path, label: str, *, strategy: str = "local") -> str:
    """The decision site as rebuild.sh runs it, with a fake docker that knows ``label``."""
    log = repo.parent / "docker.log"
    return f"""
docker() {{
  printf '%s\\n' "$*" >> {str(log)!r}
  [[ "$1" == "inspect" ]] && printf '%s' {label!r}
  return 0
}}
BUILD_STRATEGY={strategy!r}
BAY_TOML_FILES=("conf/site.yaml")
SHA=$(git -C {str(repo)!r} rev-parse --short=12 HEAD)
PREV_COMMIT=$(_running_commit svc)
if _config_only {str(repo)!r} "${{PREV_COMMIT}}"; then
  _config_only_push "bay-app/svc" "${{PREV_COMMIT}}"
fi
printf 'BUILD [%s]\\n' "$(_hold_reason {str(repo)!r})"
"""


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
    assert calls[1:] == [f"tag bay-app/svc:{first} bay-app/svc:{pushed}"]
    assert not any("build" in c or ":latest" in c for c in calls)

    # The build server path: the same rule, the tag is made in the registry.
    (tmp_path / "docker.log").unlink()
    remote_script = _decide(repo, first, strategy="remote")
    proc, _, alerts = _harness(remote_sh, remote_script, tmp_path, env=env)
    assert f"config-only push {pushed}: run bay up" in proc.stdout and alerts == []
    calls = (tmp_path / "docker.log").read_text().splitlines()
    assert calls[1:] == [
        f"buildx imagetools create -t bay-app/svc:{pushed} bay-app/svc:{first}"
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
    assert (
        'PREV_COMMIT=$(_prev_commit_remote "${SERVICE}" "${IMAGE_REPO}" "${_PREV_HEAD}")'
        in remote_sh
    )
    assert remote_sh.index("_PREV_HEAD=$(git rev-parse --short=12 HEAD") < remote_sh.index(
        "git reset --hard FETCH_HEAD"
    )

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
    # No label on the running container (or no container): the normal path.
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


def test_build_server_previous_commit_needs_its_image(remote_sh: str, tmp_path: Path) -> None:
    """S2: the checkout's last commit counts only when its image is in the registry."""
    say = 'printf "[%s]" "$(_prev_commit_remote svc bay-app/svc 0123456789abcdef)"'
    # No running container, image in the registry: that commit.
    have = 'docker() { [[ "$1" == manifest ]] && return 0; printf ""; return 0; }\n'
    proc, _, _ = _harness(remote_sh, have + say, tmp_path)
    assert proc.stdout == "[0123456789abcdef]", proc.stderr
    # Its build failed (no image pushed): no previous commit, so not config only.
    missing = 'docker() { [[ "$1" == manifest ]] && return 1; printf ""; return 0; }\n'
    proc, _, _ = _harness(remote_sh, missing + say, tmp_path)
    assert proc.stdout == "[]"
    # A running container wins, without asking the registry.
    running = (
        'docker() { [[ "$1" == manifest ]] && exit 9; '
        '[[ "$1" == inspect ]] && printf "fedcba987654"; return 0; }\n'
    )
    proc, _, _ = _harness(remote_sh, running + say, tmp_path)
    assert proc.stdout == "[fedcba987654]"


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
    assert calls[1:] == [f"tag bay-app/svc:{running} bay-app/svc:{adopt}"]

    # The script from before the adopt (no BAY_TOML_PATH) would build it.
    old = {"BAY_TOML_PATH": "", "PINNED_TOML_HASH": "", "PINNED_BUILD_HASH": "",
           "TRACK": "branch", "FROZEN": ""}
    proc, _, _ = _harness(local_sh, _decide(repo, running), tmp_path, env=old)
    assert "config-only push" not in proc.stdout and "BUILD []" in proc.stdout
