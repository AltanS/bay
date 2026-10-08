"""Image references and commit tags. Pure: no docker, no I/O.

Every build is tagged ``<repo>:<commit12>`` (``rebuild.sh``,
``git_deploy/tasks/build.yml`` and ``remote_build.yml``) and labelled
``com.bay.commit=<commit12>``. A container inherits its image's labels, so the
running commit is read from the container's labels. Images built before the
label existed may still carry ``org.opencontainers.image.revision`` (remote
builds always set it). An image with neither label may still carry a commit
tag: :func:`commit_from_tags` reads it when exactly one commit tag names the
image.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence

COMMIT_LABEL = "com.bay.commit"
REVISION_LABEL = "org.opencontainers.image.revision"
#: Length of a commit tag.
COMMIT_LEN = 12

_HEX = re.compile(r"^[0-9a-f]{7,64}$")
#: A commit tag as the builds write it: exactly 12 lowercase hex characters.
_COMMIT_TAG = re.compile(r"^[0-9a-f]{12}$")


def split_ref(ref: str) -> tuple[str, str | None]:
    """``repo:tag`` -> ``(repo, tag)``; a digest or a bare repo has no tag.

    A colon inside the registry host (``registry:5000/app``) is not a tag:
    the tag is what follows the last colon after the last slash.
    """
    ref = ref.strip()
    if "@" in ref:
        return ref.split("@", 1)[0], None
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1 :]
    return ref, None


def is_commit(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX.match(value.lower()))


def short(commit: str) -> str:
    return commit.lower()[:COMMIT_LEN]


def commit_from_labels(labels: Mapping[str, str] | None) -> str | None:
    """The 12-character commit a container's labels name, or None."""
    for key in (COMMIT_LABEL, REVISION_LABEL):
        value = (labels or {}).get(key)
        if is_commit(value):
            return short(str(value))
    return None


def commit_from_tags(
    refs: Sequence[str] | None, prefer: str | None = None
) -> tuple[str, str] | None:
    """``(commit12, ref)`` when the tags of one image name exactly one commit, else None.

    ``refs`` are the repo tags of one image (``<repo>:<tag>``). A commit tag
    is exactly 12 lowercase hex characters, as the builds write it. The same
    commit under several repos is one commit; two different commit tags are
    no answer, and neither is none. ``ref`` is the tag that names it, the one
    in the repo of ``prefer`` when there is one.
    """
    found: dict[str, list[str]] = {}
    for ref in refs or []:
        repo, tag = split_ref(str(ref))
        if tag and _COMMIT_TAG.match(tag):
            found.setdefault(tag, []).append(f"{repo}:{tag}")
    if len(found) != 1:
        return None
    commit, named = next(iter(found.items()))
    want = split_ref(prefer)[0] if prefer else None
    ref = next((r for r in named if split_ref(r)[0] == want), sorted(named)[0])
    return commit, ref


def commit_ref(image: str, commit: str) -> str:
    """``<repo>:<commit12>`` for the repo of ``image``."""
    repo, _ = split_ref(image)
    return f"{repo}:{short(commit)}"


def resolved_image(
    image: str | None,
    commit: str | None,
    running_id: str | None,
    lookup: Callable[[str], str | None] | None,
) -> str | None:
    """The image a container runs, by commit tag when that tag is provably it.

    ``<repo>:<commit12>`` when the commit is known and that tag resolves on
    the box to the image the container runs. Otherwise the reference the
    container was started with (``image``), as before.
    """
    if not image or not commit or lookup is None:
        return image
    candidate = commit_ref(image, commit)
    try:
        found = lookup(candidate)
    except Exception:  # noqa: BLE001 - a status read never fails the deploy
        return image
    if found and running_id and found == running_id:
        return candidate
    return image
