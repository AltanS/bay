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

    def helper(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-s", "-m", "bay_reconcile.tomlhash", str(path)],
            capture_output=True, text=True, env=env, cwd=tmp_path, check=False,
        )

    proc = helper(fleet / "checkouts" / "shop" / "bay.toml")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == compiled

    bad = tmp_path / "bad.toml"
    bad.write_text("name = = 1\n")
    assert helper(bad).returncode == 4
    assert helper(tmp_path / "missing.toml").returncode == 2


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
    assert calls == [
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
