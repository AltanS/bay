"""A pull signal for the image the container already runs leaves it alone (2.27.0).

The build server skips the build for a commit that is already in the registry
and still sends the image-level pull signal. A box that runs that image has
nothing to deploy. Before 2.27.0 its rebuild.sh pulled (nothing new), then
stopped, removed and recreated the container with the same image id: downtime
for nothing.

These tests render rebuild.sh, point its paths at a temp folder and run it for
real. `docker`, `curl`, `python3` and `sleep` are fakes on PATH that log every
call; `flock`, `jq` and bash are the real ones.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_rebuild_config import _render_rebuild_sh  # noqa: E402

IMAGE_REF = "zot.example.com/acme/app:latest"
IMAGE_REPO = "zot.example.com/acme/app"
REVISION = "abc123def456"
OLD_ID = "sha256:" + "1" * 64
NEW_ID = "sha256:" + "2" * 64

# One fake for every docker call the pull path makes. State lives in files of
# $FAKE: `container` (image id of the running container, absent: no container),
# `state` (the "<Running> <Restarting>" pair), `pulled` (image id of the pull).
_FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >>"${FAKE}/calls.log"
fmt=""
for ((i = 1; i <= $#; i++)); do
  [[ "${!i}" == "--format" || "${!i}" == "-f" ]] && { j=$((i + 1)); fmt="${!j}"; }
done
case "$1" in
  pull) [[ -f "${FAKE}/pull_fails" ]] && exit 1; exit 0 ;;
  inspect)
    [[ -f "${FAKE}/container" ]] || exit 1
    case "${fmt}" in
      "{{.Image}}") cat "${FAKE}/container" ;;
      "{{.State.Running}} {{.State.Restarting}}") cat "${FAKE}/state" ;;
      "{{.State.Running}}") cut -d' ' -f1 "${FAKE}/state" ;;
      *"State.Health"*) echo none ;;
      *) echo "" ;;
    esac
    exit 0 ;;
  image)
    case "${fmt}" in
      "{{.Id}}") cat "${FAKE}/pulled" ;;
      *) echo "[]" ;;
    esac
    exit 0 ;;
  run | create)
    # A recreate: the container now runs the pulled image and is up.
    cp "${FAKE}/pulled" "${FAKE}/container"
    echo "true false" >"${FAKE}/state"
    exit 0 ;;
  *) exit 0 ;;
esac
"""

_FAKE_CURL = '#!/usr/bin/env bash\necho "curl $*" >>"${FAKE}/curl.log"\necho 200\n'
_FAKE_PYTHON = '#!/usr/bin/env bash\necho "python3 $*" >>"${FAKE}/python.log"\nexit 0\n'
_FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"


def _service(**build) -> dict:
    return {
        "build": {"repo": "https://github.com/acme/app.git", "branch": "main",
                  "strategy": "remote", **build},
        "image": IMAGE_REF,
        "access": "public",
        "domains": ["app.example.com"],
        "ports": {"internal": 8000},
    }


@pytest.fixture(scope="module")
def rendered() -> str:
    return _render_rebuild_sh({"app": _service()}, ["app"])


@pytest.fixture(scope="module")
def rendered_with_release() -> str:
    svc = _service()
    svc["release"] = ["bin/migrate"]
    return _render_rebuild_sh({"app": svc}, ["app"])


def _run(script: str, tmp_path: Path, *, container: str | None, state: str = "true false",
         pulled: str = NEW_ID, revision: str | None = REVISION, failed_mark: bool = False) -> subprocess.CompletedProcess:
    """Run the rendered script as a pull trigger against fakes; return the process.

    The result carries `.calls` (docker calls), `.curl`, `.python`, `.cb` (the
    circuit-breaker state) and `.failed` (the failed-commit file).
    """
    stack = tmp_path / "stack"
    fake = tmp_path / "fake"
    bin_dir = tmp_path / "bin"
    for d in (stack / "triggers", stack / "state", fake, bin_dir, tmp_path / "failed"):
        d.mkdir(parents=True, exist_ok=True)
    text = script.replace("/opt/teststack", str(stack))
    text = text.replace("/var/lib/bay/failed-commits", str(tmp_path / "failed"))
    text = text.replace("/etc/bay/alert.env", str(tmp_path / "no-alert.env"))
    rebuild = tmp_path / "rebuild.sh"
    rebuild.write_text(text)
    for name, body in (("docker", _FAKE_DOCKER), ("curl", _FAKE_CURL),
                       ("python3", _FAKE_PYTHON), ("sleep", _FAKE_SLEEP)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (fake / "pulled").write_text(pulled)
    if container is not None:
        (fake / "container").write_text(container)
        (fake / "state").write_text(state)
    # The circuit breaker has one failure on record; a finished deploy clears it.
    (stack / "state" / "app.json").write_text(
        '{"version":1,"consecutive_failures":1,"opened_at":null,'
        '"last_failure":null,"alerts":{"opened_sent":false,"last_blocked_alert_at":null}}\n')
    if failed_mark:
        (tmp_path / "failed" / "app").write_text(f"{REVISION} {OLD_ID}\n")
    trigger = ["corr-1", "pull"] + ([revision] if revision else [])
    (stack / "triggers" / "app.trigger").write_text("\n".join(trigger) + "\n")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(fake), "HOME": str(tmp_path),
           "TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"}
    proc = subprocess.run(["bash", str(rebuild), "app"], env=env, capture_output=True,
                          text=True, check=False, timeout=60)

    def _read(path: Path) -> str:
        return path.read_text() if path.exists() else ""

    proc.calls = _read(fake / "calls.log")
    proc.curl = _read(fake / "curl.log")
    proc.python = _read(fake / "python.log")
    proc.cb = _read(stack / "state" / "app.json")
    proc.failed = _read(tmp_path / "failed" / "app")
    proc.all_output = proc.stdout + proc.stderr
    return proc


def _recreated(proc: subprocess.CompletedProcess) -> bool:
    return "docker stop app" in proc.calls and "docker rm app" in proc.calls


def _created(proc: subprocess.CompletedProcess) -> bool:
    return bool(re.search(r"^docker (run|create) ", proc.calls, re.MULTILINE))


def test_same_image_running_container_is_left_alone(rendered: str, tmp_path: Path) -> None:
    proc = _run(rendered, tmp_path, container=NEW_ID, pulled=NEW_ID, failed_mark=True)
    assert proc.returncode == 0, proc.all_output
    # The pull itself still happens (it is how the image id is known).
    assert f"docker pull {IMAGE_REF}" in proc.calls
    assert "docker stop" not in proc.calls
    assert "docker rm" not in proc.calls
    assert not _created(proc), proc.calls
    assert "docker start" not in proc.calls
    # Release and :previous stay out of it.
    assert "bin/migrate" not in proc.calls
    assert ":previous" not in proc.calls
    assert (f"[rebuild] [corr-1] Pull signal for {IMAGE_REF}: app already runs this image "
            f"({'2' * 12} as {REVISION}); container left alone") in proc.stdout
    # No alert at all: the fake curl never ran.
    assert proc.curl == ""
    assert "Pull deploy complete" not in proc.all_output
    # The bookkeeping that is true here.
    assert f"stamp --env" in proc.python and f"--commit {REVISION}" in proc.python
    assert f"--image {IMAGE_REPO}:{REVISION}" in proc.python
    assert REVISION not in proc.failed
    assert '"consecutive_failures": 0' in proc.cb or '"consecutive_failures":0' in proc.cb


def test_same_image_without_a_revision_is_left_alone_and_logs_no_revision(
    rendered: str, tmp_path: Path
) -> None:
    proc = _run(rendered, tmp_path, container=NEW_ID, pulled=NEW_ID, revision=None)
    assert proc.returncode == 0, proc.all_output
    assert not _recreated(proc) and not _created(proc)
    assert f"app already runs this image ({'2' * 12}); container left alone" in proc.stdout
    assert " as " not in proc.stdout.split("already runs this image")[1].split(";")[0]


def test_same_image_skips_the_release_command(rendered_with_release: str, tmp_path: Path) -> None:
    proc = _run(rendered_with_release, tmp_path, container=NEW_ID, pulled=NEW_ID)
    assert proc.returncode == 0, proc.all_output
    assert "Release for app" not in proc.stdout
    assert "docker run --rm" not in proc.calls
    assert "container left alone" in proc.stdout


def test_another_image_recreates_as_before(rendered: str, tmp_path: Path) -> None:
    proc = _run(rendered, tmp_path, container=OLD_ID, pulled=NEW_ID)
    assert proc.returncode == 0, proc.all_output
    assert _recreated(proc) and _created(proc)
    assert f"docker tag {OLD_ID} {IMAGE_REPO}:previous" in proc.calls
    assert "container left alone" not in proc.stdout
    assert "Pull deploy complete" in proc.all_output
    assert "curl" in proc.curl  # the deploy.pull_complete alert went out
    assert "stamp --env" in proc.python


def test_another_image_runs_the_release_command(rendered_with_release: str,
                                                tmp_path: Path) -> None:
    proc = _run(rendered_with_release, tmp_path, container=OLD_ID, pulled=NEW_ID)
    assert proc.returncode == 0, proc.all_output
    assert "Release for app with" in proc.stdout
    assert _recreated(proc)


@pytest.mark.parametrize("state", ["false false", "false true", "true true"])
def test_same_image_but_not_running_recreates_as_before(rendered: str, tmp_path: Path,
                                                        state: str) -> None:
    """Stopped or exited (false false), restarting (false true or true true)."""
    proc = _run(rendered, tmp_path, container=NEW_ID, pulled=NEW_ID, state=state)
    assert proc.returncode == 0, proc.all_output
    assert _recreated(proc) and _created(proc), proc.calls
    assert "container left alone" not in proc.stdout
    assert "Pull deploy complete" in proc.all_output
    # Same id: :previous stays, as it did before 2.27.0.
    assert f"{IMAGE_REPO}:previous" not in proc.calls


def test_no_container_creates_one_as_before(rendered: str, tmp_path: Path) -> None:
    proc = _run(rendered, tmp_path, container=None, pulled=NEW_ID)
    assert proc.returncode == 0, proc.all_output
    assert _created(proc), proc.calls
    assert "container left alone" not in proc.stdout
    assert "Pull deploy complete" in proc.all_output


@pytest.mark.parametrize(("hold", "word"), [({"track": "pin"}, "track = pin"),
                                            ({"frozen": True}, "frozen")])
def test_the_hold_path_is_unchanged(tmp_path: Path, hold: dict, word: str) -> None:
    """A held pull never reaches the new branch, even for the running image."""
    script = _render_rebuild_sh({"app": _service(**hold)}, ["app"])
    proc = _run(script, tmp_path, container=NEW_ID, pulled=NEW_ID)
    assert proc.returncode == 0, proc.all_output
    assert "HOLD app" in proc.stdout and word in proc.stdout
    assert "container left alone" not in proc.stdout
    assert f"docker pull {IMAGE_REPO}:{REVISION}" in proc.calls
    assert f"docker pull {IMAGE_REF}\n" not in proc.calls
    assert not _recreated(proc) and not _created(proc)
    assert "stamp --env" not in proc.python


def test_a_failed_pull_still_fails(rendered: str, tmp_path: Path) -> None:
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "pull_fails").write_text("")
    proc = _run(rendered, tmp_path, container=NEW_ID, pulled=NEW_ID)
    assert proc.returncode == 1, proc.all_output
    assert "container left alone" not in proc.stdout
    assert not _recreated(proc)
