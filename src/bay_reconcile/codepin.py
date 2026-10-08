"""Move ``:latest`` to a commit's image before the reconciler pass.

``bay up`` and ``bay rollback`` pass ``bay_code_targets`` (docs/plan.md, "Code
and config"). The container_lifecycle role runs this module right before
``python -m bay_reconcile``: for each target it points ``<repo>:latest`` at
``<repo>:<commit12>`` (keeping the old one as ``:previous``). The reconciler
then sees the local ``:latest`` resolve to another image than the running one
and recreates the container, as it does for any rebuilt floating tag.

A target names its commit in one of two ways:

* ``{"commit": "<sha>"}``: that commit (``bay up`` of an app-repo project, the
  pin; ``bay rollback --to``).
* ``{"source": "prev"}``: the commit the container ran before the last deploy,
  read from ``<env>.prev.json`` (``bay rollback``).

``strict`` targets fail the run (exit 1) when the image is not on the box and
cannot be pulled, and the report lists the commit tags that are there. A
non-strict target is skipped with a note, and the container keeps its image.

A box may already run the target even when ``<repo>:<commit12>`` is not on it
(a local build that tagged only ``:latest``). Before a move reports "not on
this box", :func:`runs_commit` looks at the image of the running container (or
of ``:latest``, when the container is gone): when its commit label names the
target, or it carries the tag ``<repo>:<commit12>``, the move is ``noop``
(already running). When ``:latest`` points at another image, it moves to the
running one (``retag``), so the pass keeps the container on the target.

A commit whose webhook build failed its health check on this box is refused
the same way: ``rebuild.sh`` records ``<commit12> <image id>`` per container in
``/var/lib/bay/failed-commits/<container>`` (:data:`FAILED_COMMITS_DIR`) and
untags it. A target whose commit, or whose image, is recorded there never
moves ``:latest`` (unless ``:latest`` already is that image: then nothing
moves anyway).

When ``<repo>:<commit12>`` is not on the box and the container does not run
the target, :func:`by_label` looks for a local image of the repository whose
commit label names the target (an older local build that lost its tag, or a
``:latest`` the container no longer runs). It tags that image
``<repo>:<commit12>`` and the move goes on as if the tag had been there.

``rebuild.sh`` holds the build lock (:class:`BuildLock`, the file
``git_deploy_build_lock_path``) for its whole run, from the build through the
promotion of ``:latest`` to the start of the container. Before codepin moves
``:latest`` or ``:previous`` it takes the same lock, waiting at most
``--lock-wait`` seconds (:data:`BUILD_LOCK_WAIT`), and plans again under the
lock. So ``bay up`` never moves ``:latest`` between a build's promotion and
its start. When the wait runs out, no ``:latest`` moves: each such move is
``skipped`` with the detail ``build running: ...``, and ``bay up`` notes it
as kept. A run that moves no tag never waits.

``python -m bay_reconcile.codepin --targets <json> [--prev <path>]
[--failed-dir <dir>] [--build-lock <path> [--lock-wait <seconds>]]``. Prints
one JSON report. Pure planning in :func:`plan_moves`; the docker calls are in
:class:`SdkImages`; the lock in :func:`pin_code`.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .images import (
    commit_from_labels,
    is_commit,
    own_commit_tags,
    repo_key,
    short,
    split_ref,
)

#: Written by rebuild.sh (FAILED_COMMITS_DIR there): one file per container.
FAILED_COMMITS_DIR = "/var/lib/bay/failed-commits"

#: How long codepin waits for the build lock before it skips a ``:latest`` move
#: (seconds). The deploy waits as long for a running backup
#: (``roles/deploy_stack``, "Wait for active backups"). One local build takes a
#: few minutes; a longer queue of builds is better met by running ``bay up``
#: again than by a deploy that hangs for the build lock's own hour.
BUILD_LOCK_WAIT = 300.0
#: How often codepin tries the lock while it waits (seconds).
BUILD_LOCK_POLL = 0.5
#: The tags only a deploy (codepin) and a build (rebuild.sh) move.
_MOVING_TAGS = ("latest", "previous")


class Images(Protocol):
    def image_id(self, ref: str) -> str | None: ...

    def pull(self, ref: str) -> bool: ...

    def tag(self, source: str, target: str) -> None: ...

    def commit_tags(self, repo: str) -> list[str]: ...

    def container_image(self, name: str) -> str | None:
        """The image id the container ``name`` runs, or None when there is no such container."""
        ...

    def image_meta(self, ref: str) -> tuple[Mapping[str, str], list[str]]:
        """``(labels, repo tags)`` of the image ``ref`` names (an id works); empty when absent."""
        ...

    def labelled(self, repo: str) -> list[tuple[str, Mapping[str, str]]]:
        """``(image id, labels)`` of every local image with a tag in ``repo``, newest first."""
        ...


@dataclass
class Move:
    name: str
    status: str  # retag | noop | skipped | missing
    source: str | None = None
    target: str | None = None
    detail: str = ""
    strict: bool = False
    available: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "source": self.source,
            "target": self.target,
            "detail": self.detail,
            "strict": self.strict,
            "available": self.available,
        }


def _prev_commit(prev: Mapping[str, Any] | None, name: str) -> str | None:
    for row in (prev or {}).get("containers") or []:
        if isinstance(row, Mapping) and row.get("name") == name:
            commit = row.get("commit")
            if is_commit(commit):
                return short(str(commit))
            # A receipt from before the commit field: the image tag may be one.
            _, tag = split_ref(str(row.get("image") or ""))
            if tag and is_commit(tag):
                return short(tag)
            return None
    return None


def _registry_ref(repo: str) -> bool:
    """A repo with a registry host (``host.tld/...`` or ``host:port/...``) can be pulled."""
    first = repo.split("/", 1)[0]
    return "/" in repo and ("." in first or ":" in first or first == "localhost")


def runs_commit(images: Images, name: str, image: str, commit: str) -> str | None:
    """The image id that already runs ``commit`` for container ``name``, or None.

    The image of the running container, or of ``image`` (``:latest``) when
    the container is gone. It runs ``commit`` when its commit label names it
    (``com.bay.commit``, then the revision label), or when it carries the tag
    ``<repo>:<commit12>`` in the repo of ``image`` (a tag of another repo
    never counts, as in the receipt).
    """
    running = images.container_image(name) or images.image_id(image)
    if not running:
        return None
    labels, tags = images.image_meta(running)
    if commit_from_labels(labels) == commit:
        return running
    # The receipt's rule (images.own_commit_tags): only a tag in the repo of
    # ``image`` counts; the same commit tag of another repo is not this code.
    if commit in own_commit_tags(tags, image):
        return running
    return None


def by_label(
    images: Images, name: str, image: str, commit: str, bad_images: set[str]
) -> str | None:
    """The id of a local image of ``image``'s repository labelled with ``commit``, or None.

    Only images with a tag in that repository count, and never one that
    failed its health check here. When several carry the label: the image of
    the running container, then the one ``image`` (``:latest``) names, then
    the newest.
    """
    repo, _ = split_ref(image)
    found = [
        image_id
        for image_id, labels in images.labelled(repo)
        if commit_from_labels(labels) == commit and image_id not in bad_images
    ]
    if not found:
        return None
    for preferred in (images.container_image(name), images.image_id(image)):
        if preferred in found:
            return preferred
    return found[0]


@dataclass
class Failed:
    """What failed its health check on this box, for one container."""

    commits: set[str] = field(default_factory=set)
    images: set[str] = field(default_factory=set)


def load_failed(directory: str | None, name: str) -> Failed:
    """The failed builds recorded for container ``name``: ``<commit12> <image id>`` lines."""
    out = Failed()
    if not directory or not name or "/" in name or name.startswith("."):
        return out
    try:
        with open(f"{directory}/{name}", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if parts and is_commit(parts[0]):
            out.commits.add(short(parts[0]))
        if len(parts) > 1 and parts[1] != "-":
            out.images.add(parts[1])
    return out


def plan_moves(
    targets: Sequence[Mapping[str, Any]],
    images: Images,
    *,
    prev: Mapping[str, Any] | None = None,
    failed: Mapping[str, Failed] | None = None,
    found: dict[str, str] | None = None,
) -> list[Move]:
    """Decide and apply each move. Returns one Move per target.

    ``found`` collects the commit tags :func:`by_label` made (tag -> image id),
    so a second plan (under the build lock) still says how the image was found.
    """
    found = {} if found is None else found
    out: list[Move] = []
    for t in targets:
        name = str(t.get("name") or "")
        image = str(t.get("image") or "")
        strict = bool(t.get("strict"))
        repo, tag = split_ref(image)
        if not name or not image or tag != "latest":
            out.append(Move(name, "skipped", detail=f"image {image or '?'} is not a :latest tag"))
            continue
        if t.get("source") == "prev":
            commit = _prev_commit(prev, name)
            if commit is None:
                out.append(
                    Move(
                        name,
                        "missing" if strict else "skipped",
                        target=image,
                        detail="the previous receipt names no commit for this container",
                        strict=strict,
                        available=images.commit_tags(repo),
                    )
                )
                continue
        elif is_commit(t.get("commit")):
            commit = short(str(t["commit"]))
        else:
            out.append(Move(name, "skipped", detail="the target names no commit"))
            continue
        source = f"{repo}:{commit}"
        bad = (failed or {}).get(name) or Failed()
        have = images.image_id(source)
        if have is None and commit in bad.commits:
            have = ""  # untagged after its failure: never pulled back
        if have is None:
            running = runs_commit(images, name, image, commit)
            if running is not None and running not in bad.images:
                latest = images.image_id(image)
                if latest == running:
                    out.append(
                        Move(
                            name,
                            "noop",
                            source=source,
                            target=image,
                            detail=f"already running {commit}",
                            strict=strict,
                        )
                    )
                    continue
                # :latest moved on, but the container runs the target: keep it there.
                if latest is not None:
                    images.tag(image, f"{repo}:previous")
                images.tag(running, image)
                out.append(
                    Move(
                        name,
                        "retag",
                        source=running,
                        target=image,
                        detail=f"already running {commit}; {image} points at it again",
                        strict=strict,
                    )
                )
                continue
        labelled = f"found by its commit label, tagged {source}" if source in found else ""
        if have is None:
            hit = by_label(images, name, image, commit, bad.images)
            if hit is not None:
                images.tag(hit, source)
                found[source] = hit
                have = images.image_id(source) or hit
                labelled = f"found by its commit label, tagged {source}"
        if have is None and _registry_ref(repo) and images.pull(source):
            have = images.image_id(source)
        if have is None:
            out.append(
                Move(
                    name,
                    "missing" if strict else "skipped",
                    source=source,
                    target=image,
                    detail=f"{source} is not on this box",
                    strict=strict,
                    available=images.commit_tags(repo),
                )
            )
            continue
        if have and images.image_id(image) == have:
            out.append(
                Move(name, "noop", source=source, target=image, detail=labelled, strict=strict)
            )
            continue
        if commit in bad.commits or have in bad.images:
            out.append(
                Move(
                    name,
                    "missing" if strict else "skipped",
                    source=source,
                    target=image,
                    detail=f"{source} failed its health check on this box, so it is never "
                    "deployed again; push a fix",
                    strict=strict,
                    available=images.commit_tags(repo),
                )
            )
            continue
        if images.image_id(image) is not None:
            images.tag(image, f"{repo}:previous")
        images.tag(source, image)
        out.append(
            Move(name, "retag", source=source, target=image, detail=labelled, strict=strict)
        )
    return out


# ── the build lock ─────────────────────────────────────────────────────────


class BuildLock:
    """``rebuild.sh``'s build lock: an exclusive ``flock`` on one file per stack.

    ``rebuild.sh`` opens the file for writing (``exec 9>``, as the app user,
    mode 0644) and holds the lock until it exits. ``flock`` does not need
    write access, so codepin opens it read-only and can lock it as the app
    user or as root. A missing file is created; as root it is then handed to
    the owner of its directory, or ``rebuild.sh`` could no longer open it.
    ``clock`` and ``sleep`` are injectable for tests.
    """

    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll: float = BUILD_LOCK_POLL,
    ) -> None:
        self.path = path
        self._clock = clock
        self._sleep = sleep
        self._poll = poll
        self._fd: int | None = None
        self.waited = 0.0

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _open(self) -> int:
        try:
            return os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            pass
        fd = os.open(self.path, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
        if os.geteuid() == 0:
            parent = os.stat(os.path.dirname(os.path.abspath(self.path)))
            try:
                os.fchown(fd, parent.st_uid, parent.st_gid)
            except OSError:
                pass
        return fd

    def acquire(self, wait: float) -> bool:
        """Take the lock, waiting at most ``wait`` seconds. Raises OSError when unopenable."""
        if self._fd is not None:
            return True
        fd = self._open()
        start = self._clock()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                self._fd = fd
                self.waited = self._clock() - start
                return True
            left = start + wait - self._clock()
            if left <= 0:
                os.close(fd)
                self.waited = self._clock() - start
                return False
            self._sleep(min(self._poll, left))

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


class _Replan(Exception):
    """The lock was taken just now: what was read before it may be stale."""


class _BuildRunning(Exception):
    """The wait for the lock ran out."""


class _Guarded:
    """``images`` whose ``:latest``/``:previous`` moves need the build lock.

    The first such move takes the lock and raises :class:`_Replan`: the plan
    is made again under the lock, from what docker says then. A wait that runs
    out raises :class:`_BuildRunning`. With ``refuse`` set, such a move is not
    made and its repository is recorded in ``refused``. Every other call
    passes through (a commit tag never needs the lock: no build moves it).
    """

    def __init__(self, images: Images, lock: BuildLock | None, wait: float) -> None:
        self._images = images
        self.lock = lock
        self.wait = wait
        self.refuse = False
        self.refused: set[str] = set()
        self.unavailable = ""

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._images, attr)

    def tag(self, source: str, target: str) -> None:
        repo, tag = split_ref(target)
        if tag in _MOVING_TAGS and self.lock is not None and not self.lock.held:
            if self.refuse:
                self.refused.add(f"{repo}:latest")
                return
            if not self.unavailable:
                try:
                    acquired = self.lock.acquire(self.wait)
                except OSError as exc:
                    # rebuild.sh cannot open it either (it aborts then), so no
                    # build runs: go on without it, and say so in the report.
                    self.unavailable = f"not taken ({exc.strerror or exc})"
                    acquired = None
                if acquired:
                    raise _Replan
                if acquired is False:
                    raise _BuildRunning
        self._images.tag(source, target)


def pin_code(
    rows: Sequence[Mapping[str, Any]],
    images: Images,
    *,
    prev: Mapping[str, Any] | None = None,
    failed: Mapping[str, Failed] | None = None,
    lock: BuildLock | None = None,
    wait: float = BUILD_LOCK_WAIT,
) -> tuple[list[Move], str]:
    """:func:`plan_moves` under the build lock. Returns the moves and the lock's state.

    The state is ``not needed`` (no ``:latest`` moved, or no lock given),
    ``held``, ``timeout`` or ``not taken (<why>)``. The caller releases the lock.
    """
    guard = _Guarded(images, lock, wait)
    found: dict[str, str] = {}
    try:
        moves = plan_moves(rows, guard, prev=prev, failed=failed, found=found)
    except _Replan:
        moves = plan_moves(rows, guard, prev=prev, failed=failed, found=found)
    except _BuildRunning:
        guard.refuse = True
        moves = plan_moves(rows, guard, prev=prev, failed=failed, found=found)
        detail = (
            f"build running: a build held {lock.path if lock else 'the build lock'} for "
            f"{wait:g}s, so :latest was not moved; run bay up again when it is done"
        )
        moves = [
            Move(m.name, "skipped", m.source, m.target, detail, m.strict, m.available)
            if m.status == "retag" and m.target in guard.refused
            else m
            for m in moves
        ]
        return moves, "timeout"
    if guard.unavailable:
        return moves, guard.unavailable
    return moves, "held" if lock is not None and lock.held else "not needed"


class SdkImages:
    """The docker SDK side. Only built on the box."""

    def __init__(self) -> None:
        import docker

        self._docker = docker
        self._c: Any = docker.from_env()

    def image_id(self, ref: str) -> str | None:
        try:
            return str(self._c.images.get(ref).id) or None
        except (self._docker.errors.ImageNotFound, self._docker.errors.APIError):
            return None

    def pull(self, ref: str) -> bool:
        repo, tag = split_ref(ref)
        try:
            self._c.images.pull(repo, tag=tag)
        except self._docker.errors.APIError:
            return False
        return True

    def tag(self, source: str, target: str) -> None:
        repo, tag = split_ref(target)
        self._c.images.get(source).tag(repo, tag=tag)

    def container_image(self, name: str) -> str | None:
        try:
            ctr = self._c.containers.get(name)
        except (self._docker.errors.NotFound, self._docker.errors.APIError):
            return None
        return str((ctr.attrs or {}).get("Image") or "") or None

    def image_meta(self, ref: str) -> tuple[Mapping[str, str], list[str]]:
        try:
            img = self._c.images.get(ref)
        except (self._docker.errors.ImageNotFound, self._docker.errors.APIError):
            return {}, []
        labels = getattr(img, "labels", None) or {}
        return dict(labels), [str(t) for t in (getattr(img, "tags", None) or [])]

    def commit_tags(self, repo: str) -> list[str]:
        try:
            found = self._c.images.list(name=repo)
        except self._docker.errors.APIError:
            return []
        tags: set[str] = set()
        for img in found:
            for ref in getattr(img, "tags", []) or []:
                r, t = split_ref(ref)
                if r == repo and t and is_commit(t):
                    tags.add(t)
        return sorted(tags)

    def labelled(self, repo: str) -> list[tuple[str, Mapping[str, str]]]:
        try:
            found = self._c.images.list(name=repo)
        except self._docker.errors.APIError:
            return []
        want = repo_key(repo)
        rows: list[tuple[str, str, Mapping[str, str]]] = []
        for img in found:
            tags = getattr(img, "tags", None) or []
            if not any(repo_key(split_ref(str(t))[0]) == want for t in tags):
                continue
            created = str((getattr(img, "attrs", None) or {}).get("Created") or "")
            rows.append((created, str(img.id), dict(getattr(img, "labels", None) or {})))
        rows.sort(key=lambda r: r[0], reverse=True)
        return [(image_id, labels) for _, image_id, labels in rows]


def _load_prev(path: str | None) -> Mapping[str, Any] | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None


def main(
    argv: Sequence[str] | None = None,
    *,
    images: Images | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m bay_reconcile.codepin")
    parser.add_argument(
        "--targets", required=True, help='JSON {container: {"commit"|"source", "strict"}}'
    )
    parser.add_argument("--images", default="{}", help="JSON {container: spec image}")
    parser.add_argument("--prev", default=None, help="the env's .prev.json receipt")
    parser.add_argument(
        "--failed-dir",
        default=FAILED_COMMITS_DIR,
        help="where rebuild.sh records failed builds, one file per container",
    )
    parser.add_argument(
        "--build-lock",
        default=None,
        help="rebuild.sh's build lock (git_deploy_build_lock_path); none: no lock",
    )
    parser.add_argument(
        "--lock-wait",
        type=float,
        default=BUILD_LOCK_WAIT,
        help=f"seconds to wait for the build lock (default {BUILD_LOCK_WAIT:g})",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        targets = json.loads(args.targets)
        spec_images = json.loads(args.images)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": f"invalid arguments: {exc}"}))
        return 2
    if not isinstance(targets, Mapping) or not isinstance(spec_images, Mapping):
        print(json.dumps({"ok": False, "error": "--targets and --images must be JSON objects"}))
        return 2
    # Only containers this box deploys: a target for a container on another box
    # of the env is not this box's business.
    rows = [
        {**dict(t), "name": str(name), "image": spec_images[name]}
        for name, t in sorted(targets.items())
        if isinstance(t, Mapping) and name in spec_images
    ]
    failed = {str(r["name"]): load_failed(args.failed_dir, str(r["name"])) for r in rows}
    lock = BuildLock(args.build_lock, clock=clock, sleep=sleep) if args.build_lock else None
    try:
        moves, lock_state = pin_code(
            rows,
            images or SdkImages(),
            prev=_load_prev(args.prev),
            failed=failed,
            lock=lock,
            wait=max(0.0, args.lock_wait),
        )
    finally:
        if lock is not None:
            lock.release()
    refused = [m for m in moves if m.status == "missing" and m.strict]
    print(
        json.dumps(
            {
                "ok": not refused,
                "changed": any(m.status == "retag" for m in moves),
                "moves": [m.to_dict() for m in moves],
                "build_lock": lock_state,
                "error": "; ".join(
                    f"{m.name}: {m.detail}; commit tags on this box: "
                    + (", ".join(m.available) or "none")
                    for m in refused
                )
                or None,
            }
        )
    )
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
