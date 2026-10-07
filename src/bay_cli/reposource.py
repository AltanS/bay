"""Where Bay reads an app repo: the checkout you stand in, else the fleet's repo cache.

The lock names a project's ``repo`` (its clone URL) and never a path on this
machine. Bay looks for the repo in this order:

1. The git checkout the command runs in, when its ``origin`` URL is the
   lock's ``repo``. URLs are compared in a normal form (:func:`normalize_url`),
   so ``git@host:owner/app.git`` and ``https://host/owner/app`` match.
2. A mirror clone at ``<fleet>/.bay-cache/repos/<slug>``. The slug comes from
   the normal form of the URL (:func:`cache_slug`), so two projects in one
   repo share one cache, and the same URL gives the same slug on every
   machine. It is cloned on first use and fetched (``--prune``) before a plan.

Both are only read: ``git show``, ``git archive``, ``rev-parse``. Bay never
writes to your checkout. There is no machine map file: the slug is the map.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from bay_cli import gitrepo
from bay_cli.context import cache_dir_for

#: Under ``<fleet>/.bay-cache``.
REPOS_DIR = "repos"

_SCP_RE = re.compile(r"^(?:[^@/:]+@)?([^/:]+):(?!//)(.+)$")
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?(/.*)?$", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """A clone URL in one form: ``host/owner/repo``, lower case, no ``.git``.

    ``git@github.com:Acme/App.git``, ``ssh://git@github.com/acme/app`` and
    ``https://github.com/acme/app.git/`` all give ``github.com/acme/app``.
    A local path (or ``file://`` URL) gives its absolute path without ``.git``.
    The user, the password and the port are dropped.
    """
    raw = url.strip()
    if raw.lower().startswith("file://"):
        path = raw[len("file://") :]
        return _strip(str(Path(path).expanduser().resolve()))
    match = _URL_RE.match(raw)
    if match:
        host, path = match.group(1), match.group(2) or ""
        return _strip(f"{host}/{path.lstrip('/')}").lower()
    match = _SCP_RE.match(raw)
    if match and not raw.startswith(("/", ".", "~")):
        host, path = match.group(1), match.group(2)
        return _strip(f"{host}/{path.lstrip('/')}").lower()
    return _strip(str(Path(raw).expanduser().resolve()))


def _strip(text: str) -> str:
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return text.rstrip("/")


def same_repo(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and normalize_url(str(a)) == normalize_url(str(b))


def cache_slug(url: str) -> str:
    """A directory name for ``url``: readable, and the same for every form of it.

    ``github.com/acme/app`` gives ``github.com-acme-app-<8 hex>``. The hash of
    the normal form keeps two URLs apart that read the same after the
    character swap.
    """
    norm = normalize_url(url)
    readable = re.sub(r"[^a-z0-9._-]+", "-", norm.lower()).strip("-.")[-80:].strip("-.")
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]
    return f"{readable or 'repo'}-{digest}"


def cache_path(fleet_root: Path, url: str) -> Path:
    return cache_dir_for(fleet_root) / REPOS_DIR / cache_slug(url)


def checkout_for(cwd: Path | None, url: str | None) -> Path | None:
    """The top of the git checkout at ``cwd`` when its ``origin`` is ``url``."""
    if cwd is None or not url or not cwd.is_dir():
        return None
    top = gitrepo.toplevel(cwd)
    if top is None:
        return None
    return top if same_repo(gitrepo.remote_url(top), url) else None


def ensure_cache(fleet_root: Path, url: str, *, fetch: bool) -> tuple[Path | None, str | None]:
    """The mirror of ``url`` in the fleet cache: ``(path, problem)``.

    Clones it when it is missing. With ``fetch`` an existing mirror is
    fetched first. A failed fetch keeps the path and returns the problem, so
    the caller can still read what the cache holds.
    """
    path = cache_path(fleet_root, url)
    _ignore_cache(fleet_root)
    if not (path / "HEAD").is_file():
        problem = gitrepo.clone_mirror(url, path)
        if problem:
            return None, f"cannot clone {url} into {path}: {problem}"
        return path, None
    if fetch:
        problem = gitrepo.fetch_prune(path)
        if problem:
            return path, f"cannot fetch {url} into the cache: {problem}"
    return path, None


def _ignore_cache(fleet_root: Path) -> None:
    """Make git ignore the whole cache directory, whatever the fleet's .gitignore says.

    A ``.gitignore`` of ``*`` inside ``.bay-cache`` ignores everything in it,
    itself included, so a clone there never shows in ``git status`` and a
    ``git add -A`` never takes it.
    """
    cache = cache_dir_for(fleet_root)
    marker = cache / ".gitignore"
    if not marker.is_file():
        cache.mkdir(parents=True, exist_ok=True)
        marker.write_text("# Bay caches; never committed.\n*\n")


@dataclass
class Found:
    """Where a commit of a repo was found. ``path`` is None when nowhere."""

    path: Path | None
    commit: str | None
    kind: str | None  # "checkout" | "cache"
    problem: str | None = None


def find_commit(
    fleet_root: Path, url: str, ref: str, *, cwd: Path | None = None
) -> Found:
    """Find ``ref`` of repo ``url``: in the checkout at ``cwd``, else in the cache.

    The cache is cloned when missing, and fetched when ``ref`` is not in it
    yet. A commit in neither place returns ``path=None`` and a problem line.
    """
    checkout = checkout_for(cwd, url)
    if checkout is not None:
        full = gitrepo.resolve_commit(checkout, ref)
        if full is not None:
            return Found(checkout, full, "checkout")
    cache, problem = ensure_cache(fleet_root, url, fetch=False)
    if cache is not None:
        full = gitrepo.resolve_commit(cache, ref)
        if full is None:
            cache, problem = ensure_cache(fleet_root, url, fetch=True)
            full = gitrepo.resolve_commit(cache, ref) if cache is not None else None
        if full is not None and cache is not None:
            return Found(cache, full, "cache")
    where = [f"the checkout {checkout}"] if checkout is not None else []
    where.append(f"the cache of {url}")
    detail = f" ({problem})" if problem else ""
    return Found(None, None, None, f"commit {ref[:12]} is in neither {' nor '.join(where)}{detail}")
