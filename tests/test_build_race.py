"""M121/02: the build race between rebuild.sh and codepin, and the codepin label lookup.

``rebuild.sh`` holds its build lock (``git_deploy_build_lock_path``) from the
start of its run until it exits, so across the promotion of ``:latest`` and
the start of the container. ``bay_reconcile.codepin`` takes the same lock
before it moves ``:latest``. The interleavings are simulated with a fake
docker and a lock the test holds itself; time is a fake clock, so the bounded
wait runs out without sleeping.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_rebuild_config import _local_service, _render_rebuild_sh  # noqa: E402
from test_track_mode import _Images  # noqa: E402

from bay_reconcile import codepin  # noqa: E402

ROOT = _TESTS_DIR.parent
PIN, NEW = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
SPEC = {"web": "app/web:latest"}


def _held_elsewhere(path: Path) -> bool:
    """True when another open file description holds the lock on ``path``."""
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class _Build:
    """A webhook build that holds the build lock, as rebuild.sh does with fd 9."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def exit(self) -> None:
        if self.fd is not None:
            os.close(self.fd)  # the kernel drops the lock with the last fd
            self.fd = None


class _LockCheckedImages(_Images):
    """A box whose ``:latest``/``:previous`` may only move while codepin holds the lock."""

    def __init__(self, lock_path: Path, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lock_path = lock_path
        self.moves: list[tuple[str, str]] = []

    def tag(self, source: str, target: str) -> None:
        if target.endswith((":latest", ":previous")):
            assert _held_elsewhere(self.lock_path), f"{target} moved without the build lock"
            self.moves.append((source, target))
        super().tag(source, target)


class _Clock:
    """Fake time: ``sleep`` advances ``now``; ``on_sleep`` runs at each sleep."""

    def __init__(self, on_sleep: Callable[[int], None] | None = None) -> None:
        self.now = 1000.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


def _run(
    images: _Images,
    targets: dict,
    capsys: pytest.CaptureFixture[str],
    *,
    lock: Path | None = None,
    wait: float = 300,
    clock: _Clock | None = None,
) -> tuple[int, dict]:
    args = ["--targets", json.dumps(targets), "--images", json.dumps(SPEC)]
    if lock is not None:
        args += ["--build-lock", str(lock), "--lock-wait", str(wait)]
    clock = clock or _Clock()
    code = codepin.main(args, images=images, clock=clock.clock, sleep=clock.sleep)
    return code, json.loads(capsys.readouterr().out)


def _race_box(lock: Path) -> _LockCheckedImages:
    """The window of the race: pin A runs, a build just moved :latest to B.

    No ``<repo>:A`` tag (a local build from before 2.1, or a pruned tag);
    ``_promote_latest`` kept A as ``:previous``.
    """
    return _LockCheckedImages(
        lock,
        {"app/web:latest": "id-b", "app/web:previous": "id-a", f"app/web:{NEW}": "id-b"},
        labels={"id-a": {"com.bay.commit": PIN}, "id-b": {"com.bay.commit": NEW}},
        containers={"web": "id-a"},
    )


# ── the lock ────────────────────────────────────────────────────────────


def test_codepin_waits_for_build_lock(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lock = tmp_path / "build.lock"

    # 1. A build holds the lock for longer than the wait: :latest stays on B,
    #    the move is skipped as "build running", also for a strict target, and
    #    the wait is bounded by --lock-wait.
    build = _Build(lock)
    images = _race_box(lock)
    clock = _Clock()
    code, report = _run(
        images, {"web": {"commit": PIN, "strict": True}}, capsys, lock=lock, clock=clock
    )
    (move,) = report["moves"]
    assert code == 0 and report["ok"] is True and report["changed"] is False
    assert move["status"] == "skipped" and move["detail"].startswith("build running: ")
    assert str(lock) in move["detail"] and "300s" in move["detail"]
    assert report["build_lock"] == "timeout"
    assert images.ids["app/web:latest"] == "id-b" and images.moves == []
    assert 300 <= clock.now - 1000.0 < 301
    # bay up turns the skipped move into a `kept` note with that detail.
    from bay_cli.apply import kept_code

    receipt = {"fleet_commit": "f", "code_moves": report["moves"]}
    (kept,) = kept_code([{"box": "b1", "receipt": receipt}], "f", {"web": {}})
    assert kept["reason"].startswith("build running: ")

    # 2. The build finishes while codepin waits: rebuild.sh started B (by now
    #    the container runs it) and exits. codepin takes the lock and plans
    #    again from what docker says then: A is found by its label, tagged, and
    #    :latest moves to it under the lock (the tag fake checks the lock).
    def build_done(n: int) -> None:
        if n == 3:
            images.containers["web"] = "id-b"
            build.exit()

    clock = _Clock(build_done)
    code, report = _run(images, {"web": {"commit": PIN}}, capsys, lock=lock, clock=clock)
    (move,) = report["moves"]
    assert code == 0 and clock.sleeps == 3 and report["build_lock"] == "held"
    assert move["status"] == "retag" and move["source"] == f"app/web:{PIN}"
    assert images.ids["app/web:latest"] == "id-a" and images.ids[f"app/web:{PIN}"] == "id-a"
    assert images.ids["app/web:previous"] == "id-b"
    assert images.moves == [
        ("app/web:latest", "app/web:previous"),
        (f"app/web:{PIN}", "app/web:latest"),
    ]
    # codepin let the lock go when it exited: the next build can take it.
    assert not _held_elsewhere(lock)

    # 3. Nothing to move: no wait at all, even while a build holds the lock.
    build = _Build(lock)
    clock = _Clock()
    code, report = _run(images, {"web": {"commit": PIN}}, capsys, lock=lock, clock=clock)
    assert report["moves"][0]["status"] == "noop" and clock.sleeps == 0
    assert report["build_lock"] == "not needed"
    build.exit()

    # 4. No --build-lock (an old task file): the moves run as before 2.5.1.
    plain = _race_box(lock)
    plain.tag = _Images.tag.__get__(plain)  # type: ignore[method-assign]
    code, report = _run(plain, {"web": {"commit": PIN}}, capsys)
    assert report["moves"][0]["status"] == "retag" and report["build_lock"] == "not needed"


def test_codepin_lock_interlocks_with_shell_flock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The lock is the one rebuild.sh takes: `exec 9>file; flock -x 9` in bash."""
    if shutil.which("flock") is None:
        pytest.skip("flock(1) not installed")
    lock = tmp_path / "build.lock"
    shell = subprocess.Popen(
        ["bash", "-c", f"exec 9>{str(lock)!r}; flock -x 9; echo locked; read -r _"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert shell.stdout is not None and shell.stdout.readline().strip() == "locked"
        images = _race_box(lock)
        code, report = _run(images, {"web": {"commit": PIN}}, capsys, lock=lock, wait=1)
        assert report["moves"][0]["status"] == "skipped" and report["build_lock"] == "timeout"
        assert images.ids["app/web:latest"] == "id-b"
    finally:
        assert shell.stdin is not None
        shell.stdin.write("\n")
        shell.stdin.close()
        shell.wait(timeout=10)
    images = _race_box(lock)
    code, report = _run(images, {"web": {"commit": PIN}}, capsys, lock=lock, wait=1)
    assert report["moves"][0]["status"] == "retag" and report["build_lock"] == "held"


def test_codepin_build_lock_file_modes(tmp_path: Path) -> None:
    """A read-only lock file still locks; a missing one is created, not truncated later."""
    lock = tmp_path / "build.lock"
    lock.write_text("")
    lock.chmod(0o444)
    held = codepin.BuildLock(str(lock))
    assert held.acquire(0) is True and _held_elsewhere(lock)
    held.release()
    assert not _held_elsewhere(lock)

    fresh = tmp_path / "sub" / "build.lock"
    fresh.parent.mkdir()
    made = codepin.BuildLock(str(fresh))
    assert made.acquire(0) is True and fresh.exists()
    made.release()
    # rebuild.sh opens it for writing afterwards, as the app user.
    os.close(os.open(fresh, os.O_WRONLY | os.O_TRUNC))

    # A lock file that cannot be opened: the moves go on, and the report says so.
    gone = tmp_path / "no-such-dir" / "build.lock"
    images = _race_box(tmp_path / "unused")
    images.tag = _Images.tag.__get__(images)  # type: ignore[method-assign]
    moves, state = codepin.pin_code(
        [{"name": "web", "image": "app/web:latest", "commit": PIN}],
        images,
        lock=codepin.BuildLock(str(gone)),
    )
    assert moves[0].status == "retag" and state.startswith("not taken")


def test_rebuild_holds_build_lock_through_start() -> None:
    """rebuild.sh takes fd 9 before the promotion, never closes it, and codepin uses its path."""
    rendered = _render_rebuild_sh(_local_service(), ["localapp"], git_deploy_services=["localapp"])
    locked = rendered.index("flock -x -w 3600 9")
    promote = rendered.index('_promote_latest "${IMAGE_NAME}" "${SHA}"')
    start = rendered.index("\n_run_container\n", promote)
    assert locked < promote < start
    assert "9>&-" not in rendered and "flock -u 9" not in rendered
    # The pull path promotes and starts under the same lock.
    assert locked < rendered.index('docker pull "${IMAGE_REF}"')

    defaults = yaml.safe_load((ROOT / "roles/git_deploy/defaults/main.yml").read_text())
    assert defaults["git_deploy_build_lock_path"] == "{{ stack_dir }}/build.lock"
    tasks = yaml.safe_load((ROOT / "roles/container_lifecycle/tasks/reconcile.yml").read_text())
    block = next(
        t for t in tasks if t.get("name") == "Reconcile the containers and record the result"
    )
    task = next(
        t for t in block["block"] if t.get("name") == "Point :latest at the pinned commit images"
    )
    argv = task["ansible.builtin.command"]["argv"]
    path = argv[argv.index("--build-lock") + 1]
    assert path == "{{ git_deploy_build_lock_path | default(stack_dir ~ '/build.lock') }}"
    assert (
        argv[argv.index("--lock-wait") + 1] == "{{ container_lifecycle_build_lock_wait | string }}"
    )
    lc = yaml.safe_load((ROOT / "roles/container_lifecycle/defaults/main.yml").read_text())
    assert lc["container_lifecycle_build_lock_wait"] == codepin.BUILD_LOCK_WAIT == 300


# ── the label lookup ────────────────────────────────────────────────────


def test_codepin_finds_target_by_label(capsys: pytest.CaptureFixture[str]) -> None:
    # The false `kept` of 2.5.0: the container runs B, :latest is A's image
    # (labelled A), and there is no <repo>:A tag. The image is found by its
    # label and tagged; :latest already is it, so the pass recreates to A.
    images = _Images(
        {"app/web:latest": "id-a"},
        labels={"id-a": {"com.bay.commit": PIN}, "id-b": {"com.bay.commit": NEW}},
        containers={"web": "id-b"},
    )
    code, report = _run(images, {"web": {"commit": PIN, "strict": True}}, capsys)
    (move,) = report["moves"]
    assert code == 0 and move["status"] == "noop"
    assert move["detail"] == f"found by its commit label, tagged app/web:{PIN}"
    assert images.ids[f"app/web:{PIN}"] == "id-a" and images.ids["app/web:latest"] == "id-a"

    # Only under :previous: tagged, then :latest moves to it.
    images = _Images(
        {"app/web:latest": "id-b", "app/web:previous": "id-a"},
        labels={"id-a": {"org.opencontainers.image.revision": PIN + "0" * 28}},
        containers={"web": "id-b"},
    )
    code, report = _run(images, {"web": {"commit": PIN}}, capsys)
    (move,) = report["moves"]
    assert move["status"] == "retag" and move["detail"].startswith("found by its commit label")
    assert images.ids["app/web:latest"] == "id-a" and images.ids["app/web:previous"] == "id-b"

    # Several images carry the label: the running one wins, then :latest's.
    images = _Images(
        {"app/web:latest": "id-a2", "app/web:previous": "id-a1", "app/web:old": "id-a3"},
        labels={i: {"com.bay.commit": PIN} for i in ("id-a1", "id-a2", "id-a3")},
        containers={"web": "id-zz"},
    )
    assert codepin.by_label(images, "web", "app/web:latest", PIN, set()) == "id-a2"
    images.containers["web"] = "id-a3"
    assert codepin.by_label(images, "web", "app/web:latest", PIN, set()) == "id-a3"

    # Not found: a labelled image of another repository, or one that failed here.
    images = _Images(
        {"app/web:latest": "id-b", "other/web:latest": "id-a"},
        labels={"id-a": {"com.bay.commit": PIN}, "id-b": {"com.bay.commit": NEW}},
        containers={"web": "id-b"},
    )
    code, report = _run(images, {"web": {"commit": PIN}}, capsys)
    assert report["moves"][0]["status"] == "skipped"
    assert report["moves"][0]["detail"] == f"app/web:{PIN} is not on this box"
    failed = _Images(
        {"app/web:latest": "id-b", "app/web:previous": "id-a"},
        labels={"id-a": {"com.bay.commit": PIN}},
        containers={"web": "id-b"},
    )
    (move,) = codepin.plan_moves(
        [{"name": "web", "image": "app/web:latest", "commit": PIN}],
        failed,
        failed={"web": codepin.Failed(images={"id-a"})},
    )
    assert move.status == "skipped" and f"app/web:{PIN}" not in failed.ids


def test_sdk_labelled_reads_own_repo_newest_first() -> None:
    class Img:
        def __init__(self, image_id: str, tags: list[str], created: str, labels: dict) -> None:
            self.id, self.tags, self.labels = image_id, tags, labels
            self.attrs = {"Created": created}

    class Listing:
        def list(self, name: str) -> list[Img]:
            assert name == "app/web"
            return [
                Img(
                    "id-old", ["app/web:previous"], "2026-10-01T00:00:00Z", {"com.bay.commit": PIN}
                ),
                Img("id-new", ["app/web:latest"], "2026-10-08T00:00:00Z", {}),
                Img("id-foreign", ["app/web-copy:latest"], "2026-10-09T00:00:00Z", {}),
            ]

    sdk = codepin.SdkImages.__new__(codepin.SdkImages)
    sdk._c = type("C", (), {"images": Listing()})()
    sdk._docker = None
    assert sdk.labelled("app/web") == [("id-new", {}), ("id-old", {"com.bay.commit": PIN})]
