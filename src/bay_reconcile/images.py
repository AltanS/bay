"""Image references and commit tags. Pure: no docker, no I/O.

Every build is tagged ``<repo>:<commit12>`` (``rebuild.sh``,
``git_deploy/tasks/build.yml`` and ``remote_build.yml``) and labelled
``com.bay.commit=<commit12>``. A container inherits its image's labels, so the
running commit is read from the container's labels. Images built before the
label existed may still carry ``org.opencontainers.image.revision`` (remote
builds always set it). An image with neither label may still carry a commit
tag: :func:`commit_from_tags` reads it when exactly one commit tag of the
container's own repository names the image. A tag of another repository
never counts: an image can carry tags of several repos (a retag, a manual
`docker tag`), and only the repo the container was started from is its code.
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


def is_commit_tag(value: object) -> bool:
    """A tag as the builds write a commit: exactly 12 lowercase hex characters."""
    return isinstance(value, str) and bool(_COMMIT_TAG.match(value))


def short(commit: str) -> str:
    return commit.lower()[:COMMIT_LEN]


def commit_from_labels(labels: Mapping[str, str] | None) -> str | None:
    """The 12-character commit a container's labels name, or None."""
    for key in (COMMIT_LABEL, REVISION_LABEL):
        value = (labels or {}).get(key)
        if is_commit(value):
            return short(str(value))
    return None


def repo_key(repo: str) -> str:
    """``repo`` as Docker names it: no ``docker.io/`` and no ``library/`` prefix.

    The daemon lists ``postgres:16`` for an image pulled as
    ``docker.io/library/postgres:16``; both are the same repository.
    """
    for prefix in ("docker.io/", "index.docker.io/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix) :]
            break
    if repo.startswith("library/") and repo.count("/") == 1:
        repo = repo[len("library/") :]
    return repo


def own_commit_tags(refs: Sequence[str] | None, image: str | None) -> list[str]:
    """The sorted commit tags among ``refs`` that are in the repository of ``image``.

    ``refs`` are the repo tags of one image (``<repo>:<tag>``); ``image`` is
    the reference the container was started with (its tag does not matter).
    A commit tag is exactly 12 lowercase hex characters, as the builds write
    it. A tag of another repository never counts. No ``image``, no tags.
    """
    if not image:
        return []
    want = repo_key(split_ref(image)[0])
    found: set[str] = set()
    for ref in refs or []:
        repo, tag = split_ref(str(ref))
        if tag and _COMMIT_TAG.match(tag) and repo_key(repo) == want:
            found.add(tag)
    return sorted(found)


def commit_from_tags(
    refs: Sequence[str] | None, image: str | None
) -> tuple[str, str] | None:
    """``(commit12, ref)`` when exactly one commit tag of ``image``'s repo names the image.

    ``refs`` are the repo tags of one image (``<repo>:<tag>``); ``image`` is
    the container's configured reference. Only tags of that repository count
    (:func:`own_commit_tags`): two different own commit tags are no answer,
    and neither is none. ``ref`` is the own-repo tag that names the commit, as
    the daemon lists it, so it resolves on the box.
    """
    tags = own_commit_tags(refs, image)
    if len(tags) != 1:
        return None
    commit = tags[0]
    want = repo_key(split_ref(str(image))[0])
    ref = sorted(
        str(r) for r in refs or []
        if split_ref(str(r))[1] == commit and repo_key(split_ref(str(r))[0]) == want
    )[0]
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
