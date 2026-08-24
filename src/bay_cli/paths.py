"""Path resolution for bay framework and consumer directories.

The canonical consumer conventions are ``.bay/`` (framework clone),
``.bay-version`` (pin file) and ``.bay-dev`` (dev-link sentinel).

Every reference to the pre-1.0 spellings below is tagged ``legacy-argo:`` —
this module is the single place where the old convention is still readable, so
removing the transition shim (deferred to a future major release, not v1.1 as
an earlier draft of this docstring said) is `grep legacy-argo src/bay_cli/paths.py`.
An un-migrated consumer keeps working (with a warning naming the renames)
rather than failing with "not found". The `bay migrate-consumer` command that
used to perform those renames was removed in v1.6.0: every consumer that
existed had already been migrated, so it had no remaining callers.
"""

import os
import sys
from pathlib import Path

from bay_cli.errors import BayError

FRAMEWORK_DIR = ".bay"
VERSION_FILE = ".bay-version"
DEV_LINK_FILE = ".bay-dev"

# legacy-argo: pre-1.0 spellings, dual-read only. Remove in a future major
# release, not v1.1 as previously noted here.
_LEGACY_FRAMEWORK_DIR = ".argo"  # legacy-argo: old clone dir
_LEGACY_VERSION_FILE = ".argo-version"  # legacy-argo: old pin file
_LEGACY_DEV_LINK_FILE = ".argo-dev"  # legacy-argo: old dev-link sentinel

# Back-compat alias for callers that imported the private name.
_DEV_LINK_FILE = DEV_LINK_FILE

_warned = False


def _warn_legacy_layout() -> None:
    """Print one stderr warning per process when a legacy path is used."""
    global _warned
    if _warned:
        return
    _warned = True
    print(
        "warning: this project still uses the pre-1.0 layout "
        "(.argo/, .argo-version, .argo-dev)\n"  # legacy-argo: transition warning text
        "         rename them to .bay/, .bay-version and .bay-dev, and "
        "bin/argo to bin/bay",  # legacy-argo: transition warning text
        file=sys.stderr,
    )


def _is_framework_dir(candidate: Path) -> bool:
    return candidate.is_dir() and (candidate / ".git").exists()


def _describe_broken_framework(entry: Path) -> str:
    """Explain a framework entry that exists on disk but is not usable.

    The common case is a DANGLING symlink: a dev-linked consumer whose
    ``.argo`` points at a sibling checkout that was renamed away by the 1.0  # legacy-argo: names the pre-1.0 dir
    rename. That is a broken *link*, not a missing framework, and the fix is
    to repoint it at the renamed checkout — not ``bin/bay setup``.
    """
    is_legacy = entry.name == _LEGACY_FRAMEWORK_DIR  # legacy-argo: pre-1.0 clone dir
    if entry.is_symlink():
        target = os.readlink(entry)
        what = f"{entry} → {target} (target missing)"
    else:
        what = f"{entry} (not a framework checkout — no .git)"
    if is_legacy:
        return (
            f"framework link is broken: {what}\n"
            "this consumer still uses the pre-1.0 layout — "  # legacy-argo: transition hint
            "repoint it at the renamed framework checkout"
        )
    return (
        f"framework link is broken: {what}\n"
        "run 'bin/bay install' to restore the pinned framework checkout"
    )


def find_bay_dir(start: Path | None = None) -> Path:
    """Locate the .bay/ framework directory.

    Walks up from start (default: cwd) looking for a .bay/ directory. Falls
    back to the pre-1.0 clone dir (with a warning) so an un-migrated
    consumer keeps working through the transition.

    A framework entry that exists but does not resolve (most often a dangling
    dev-link symlink) raises a targeted "link is broken" error instead of the
    generic not-found, so the operator is pointed at the right repair.
    """
    cwd = start or Path.cwd()
    current = cwd
    broken: Path | None = None
    while True:
        candidate = current / FRAMEWORK_DIR
        if _is_framework_dir(candidate):
            return candidate
        legacy = current / _LEGACY_FRAMEWORK_DIR  # legacy-argo: dual-read fallback
        if _is_framework_dir(legacy):
            _warn_legacy_layout()
            return legacy
        # Neither resolved. Remember the innermost entry that exists
        # *lexically* (os.path.lexists is true for a dangling symlink) so the
        # error can name it, but keep walking — an outer real checkout wins.
        if broken is None:
            for entry in (candidate, legacy):
                if os.path.lexists(entry):
                    broken = entry
                    break
        parent = current.parent
        if parent == current:
            break
        current = parent
    if broken is not None:
        raise BayError(_describe_broken_framework(broken))
    raise BayError("bay not found — run 'bin/bay setup' first")


def consumer_root(bay_dir: Path | None = None) -> Path:
    """Return the consumer project root (parent of .bay/).

    Requires a *resolvable* framework checkout, which every caller needs in
    order to do anything.
    """
    return (bay_dir or find_bay_dir()).parent


def version_file(root: Path | None = None) -> Path:
    """Return path to the .bay-version pin file.

    If only the pre-1.0 pin file exists, that path is returned (and a
    warning printed) so reads keep working before migration. Writers always
    get the new path once the legacy file is gone.
    """
    r = root or consumer_root()
    new = r / VERSION_FILE
    if new.exists():
        return new
    legacy = r / _LEGACY_VERSION_FILE  # legacy-argo: dual-read fallback
    if legacy.exists():
        _warn_legacy_layout()
        return legacy
    return new


def read_pinned_version(root: Path | None = None) -> str | None:
    """Read the pinned version from .bay-version, or None if missing."""
    vf = version_file(root)
    if vf.exists():
        return vf.read_text().strip()
    return None


def dev_link_file(root: Path | None = None) -> Path:
    """Return path to the .bay-dev sentinel file.

    Falls back to the pre-1.0 sentinel spelling when only that one exists, so
    `dev-unlink` on an un-migrated consumer still finds and removes it.
    """
    r = root or consumer_root()
    new = r / DEV_LINK_FILE
    if new.exists():
        return new
    legacy = r / _LEGACY_DEV_LINK_FILE  # legacy-argo: dual-read fallback
    if legacy.exists():
        _warn_legacy_layout()
        return legacy
    return new


def is_dev_linked(root: Path | None = None) -> bool:
    """Check if the consumer is in dev-link mode.

    A consumer is considered dev-linked if either the .bay-dev sentinel
    exists, OR the .bay entry is a symlink (the symlink is the
    authoritative signal; the sentinel is an operator convenience).
    """
    r = root or consumer_root()
    if dev_link_file(r).exists():
        return True
    for name in (FRAMEWORK_DIR, _LEGACY_FRAMEWORK_DIR):  # legacy-argo: dual-read fallback
        try:
            if (r / name).is_symlink():
                return True
        except OSError:
            continue
    return False


def read_installed_version(bay_dir: Path) -> str | None:
    """Read the installed framework version from ``bay_dir/version.yml``.

    Returns the ``bay_version`` string if present, ``None`` if the file
    is missing or the key is absent.  Raises :class:`BayError` on YAML
    parse failures.
    """
    version_path = bay_dir / "version.yml"
    if not version_path.is_file():
        return None

    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    yaml = YAML()
    try:
        with version_path.open() as fh:
            data = yaml.load(fh)
    except YAMLError as exc:
        raise BayError(f"Failed to parse {version_path}: {exc}") from exc

    if not isinstance(data, dict):
        return None

    value = data.get("bay_version")
    if value is None:
        return None
    return str(value)
