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

A commit whose webhook build failed its health check on this box is refused
the same way: ``rebuild.sh`` records ``<commit12> <image id>`` per container in
``/var/lib/bay/failed-commits/<container>`` (:data:`FAILED_COMMITS_DIR`) and
untags it. A target whose commit, or whose image, is recorded there never
moves ``:latest`` (unless ``:latest`` already is that image: then nothing
moves anyway).

``python -m bay_reconcile.codepin --targets <json> [--prev <path>]
[--failed-dir <dir>]``. Prints one JSON report. Pure planning in :func:`plan_moves`; the docker calls are in
:class:`SdkImages`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .images import is_commit, short, split_ref

#: Written by rebuild.sh (FAILED_COMMITS_DIR there): one file per container.
FAILED_COMMITS_DIR = "/var/lib/bay/failed-commits"


class Images(Protocol):
    def image_id(self, ref: str) -> str | None: ...

    def pull(self, ref: str) -> bool: ...

    def tag(self, source: str, target: str) -> None: ...

    def commit_tags(self, repo: str) -> list[str]: ...


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
) -> list[Move]:
    """Decide and apply each move. Returns one Move per target."""
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
            out.append(Move(name, "noop", source=source, target=image, strict=strict))
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
        out.append(Move(name, "retag", source=source, target=image, strict=strict))
    return out


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


def _load_prev(path: str | None) -> Mapping[str, Any] | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None


def main(argv: Sequence[str] | None = None, *, images: Images | None = None) -> int:
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
    moves = plan_moves(
        rows, images or SdkImages(), prev=_load_prev(args.prev), failed=failed
    )
    refused = [m for m in moves if m.status == "missing" and m.strict]
    print(
        json.dumps(
            {
                "ok": not refused,
                "changed": any(m.status == "retag" for m in moves),
                "moves": [m.to_dict() for m in moves],
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
