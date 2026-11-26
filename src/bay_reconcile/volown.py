"""Volume owners: set the owner of a Docker named volume (bay.toml ``owner``).

Runs on the box as root, from ``roles/container_lifecycle/tasks/volume_owners.yml``,
once before the reconciler pass and once after it. Ansible ships this one
file with ``ansible.builtin.script``, so it imports nothing from its package
and nothing outside the standard library. It talks to Docker through the
``docker`` CLI, because root may lack the docker SDK.

Why two passes. When Docker creates a container on an EMPTY named volume and
the mount path exists in the image, Docker copies the owner and mode of that
image directory onto the volume's top directory. A path absent from the image
leaves the owner alone, and a volume with content is never touched. So Bay
sets the owner before the reconciler (a first start finds the volume
writable) and again after it (Docker may just have reset an empty volume).

The top-directory rule. Bay looks only at the volume's top directory. When
its owner matches, the run changes nothing and walks nothing (the same idea
as Kubernetes ``fsGroupChangePolicy: OnRootMismatch``). When it differs, Bay
walks the whole tree, changes every entry that differs, and changes the top
LAST. An interrupted run leaves the top wrong, so the next run walks again.

The tree is writable by a container and this code runs as root. So it never
follows a symlink, works from directory file descriptors only, and does not
descend into another file system.

``ensure --spec '{"volumes": {"<volume>": "<uid>[:<gid>]"}}'`` prints one JSON
document on stdout: ``{"volumes": {<volume>: {mountpoint, created, before,
owner, changed}}}``. ``created`` and ``changed`` are the word ``would`` in a
``--dry-run`` when the run would act. On any error it prints only
``{"error": "<message>"}`` and exits 1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

#: Highest uid or gid Bay accepts. 4294967295 is (uid_t)-1, "no change" to chown.
MAX_ID = 4294967294
_OWNER_RE = re.compile(r"([0-9]+)(?::([0-9]+))?")
#: `docker info` security options that make container uids differ from host uids.
_REMAPPED = ("name=userns", "name=rootless")


class VolOwnError(Exception):
    """A refusal or a failure; the message names the volume."""


#: Runs one command and returns (returncode, stdout, stderr).
Runner = Callable[[Sequence[str]], tuple[int, str, str]]


def run_cmd(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    except OSError as exc:
        raise VolOwnError(f"cannot run {argv[0]}: {exc}") from exc
    return proc.returncode, proc.stdout, proc.stderr


def parse_owner(text: str, volume: str = "") -> tuple[int, int | None]:
    """``"uid"`` or ``"uid:gid"``, digits only, each 0..MAX_ID. No gid: None."""
    match = _OWNER_RE.fullmatch(str(text))
    if not match:
        raise VolOwnError(
            f"volume {volume}: owner {text!r} is not <uid> or <uid>:<gid> (digits only)"
        )
    uid = int(match.group(1))
    gid = int(match.group(2)) if match.group(2) is not None else None
    for value in (uid, gid):
        if value is not None and value > MAX_ID:
            raise VolOwnError(f"volume {volume}: owner {text!r} is out of range (0..{MAX_ID})")
    return uid, gid


def check_daemon(run: Runner) -> None:
    """Refuse a daemon with user namespaces or in rootless mode."""
    rc, out, err = run(["docker", "info", "--format", "{{json .SecurityOptions}}"])
    if rc != 0:
        raise VolOwnError(f"docker info failed: {err.strip() or out.strip()}")
    try:
        options = json.loads(out or "null") or []
    except ValueError as exc:
        raise VolOwnError(f"docker info printed no JSON: {out.strip()!r}") from exc
    for option in options:
        parts = str(option).split(",")
        for bad in _REMAPPED:
            if bad in parts:
                raise VolOwnError(
                    f"the Docker daemon runs with {bad}: container uids do not map "
                    "to host uids there, so Bay cannot set a volume owner"
                )


def inspect_volume(run: Runner, name: str) -> dict[str, Any] | None:
    rc, out, err = run(["docker", "volume", "inspect", name])
    if rc != 0:
        if "no such volume" in (err + out).lower():
            return None
        raise VolOwnError(f"volume {name}: docker volume inspect failed: {err.strip()}")
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise VolOwnError(f"volume {name}: docker volume inspect printed no JSON") from exc
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise VolOwnError(f"volume {name}: docker volume inspect gave an unexpected answer")
    return data[0]


def check_volume(name: str, attrs: Mapping[str, Any]) -> str:
    """Return the mountpoint of a plain local volume, or refuse."""
    driver = attrs.get("Driver")
    if driver != "local":
        raise VolOwnError(
            f"volume {name}: driver is {driver!r}, not 'local'; Bay sets the owner "
            "of a plain local volume only"
        )
    if attrs.get("Options"):
        raise VolOwnError(
            f"volume {name}: it has driver options (a bind or NFS volume); "
            "Bay sets the owner of a plain local volume only"
        )
    mountpoint = attrs.get("Mountpoint")
    if not isinstance(mountpoint, str) or not os.path.isabs(mountpoint):
        raise VolOwnError(f"volume {name}: mountpoint {mountpoint!r} is not an absolute path")
    head, last = os.path.split(mountpoint.rstrip("/"))
    if last != "_data" or os.path.basename(head) != name:
        raise VolOwnError(
            f"volume {name}: mountpoint {mountpoint!r} does not end in {name}/_data"
        )
    return mountpoint


def open_top(name: str, mountpoint: str) -> int:
    try:
        return os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise VolOwnError(f"volume {name}: cannot open {mountpoint}: {exc}") from exc


def _matches(st: os.stat_result, uid: int, gid: int | None) -> bool:
    return st.st_uid == uid and (gid is None or st.st_gid == gid)


def _entry_stat(name: str, dir_fd: int) -> os.stat_result:
    """lstat of one entry, relative to its directory fd (a seam for tests)."""
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def chown_tree(top_fd: int, uid: int, gid: int | None, volume: str = "") -> int:
    """Change every entry under ``top_fd`` that differs, the top LAST.

    Returns how many entries changed. Never follows a symlink, never enters
    another device, skips an entry that vanished.
    """
    gid_arg = -1 if gid is None else gid
    top_dev = os.fstat(top_fd).st_dev
    changed = 0
    errors: list[OSError] = []

    def onerror(exc: OSError) -> None:
        if not isinstance(exc, FileNotFoundError):
            errors.append(exc)

    for _path, dirnames, filenames, dirfd in os.fwalk(
        ".", topdown=True, onerror=onerror, follow_symlinks=False, dir_fd=top_fd
    ):
        if errors:
            break
        keep: list[str] = []
        for entry in list(dirnames) + list(filenames):
            try:
                st = _entry_stat(entry, dirfd)
            except FileNotFoundError:
                continue
            if st.st_dev != top_dev:
                # Another file system mounted inside the volume: not Bay's.
                continue
            if entry in dirnames and stat.S_ISDIR(st.st_mode):
                keep.append(entry)
            if _matches(st, uid, gid):
                continue
            try:
                os.chown(entry, uid, gid_arg, dir_fd=dirfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            changed += 1
        dirnames[:] = [d for d in dirnames if d in keep]
    if errors:
        raise VolOwnError(f"volume {volume}: walk failed: {errors[0]}")
    if not _matches(os.fstat(top_fd), uid, gid):
        os.fchown(top_fd, uid, gid_arg)
        changed += 1
    return changed


def ensure(
    volumes: Mapping[str, str],
    *,
    dry_run: bool = False,
    run: Runner = run_cmd,
    geteuid: Callable[[], int] | None = None,
) -> dict[str, dict[str, Any]]:
    owners = {name: parse_owner(owner, name) for name, owner in volumes.items()}
    if not owners:
        return {}
    if not dry_run and (geteuid or os.geteuid)() != 0:
        raise VolOwnError(
            "setting a volume owner needs root; run it with become_user root "
            "(or with --dry-run to only report)"
        )
    check_daemon(run)
    result: dict[str, dict[str, Any]] = {}
    for name, (uid, gid) in owners.items():
        entry: dict[str, Any] = {
            "mountpoint": None,
            "created": False,
            "before": None,
            "owner": str(volumes[name]),
            "changed": 0,
        }
        attrs = inspect_volume(run, name)
        if attrs is None:
            if dry_run:
                entry["created"] = "would"
                entry["changed"] = "would"
                result[name] = entry
                continue
            rc, _out, err = run(["docker", "volume", "create", name])
            if rc != 0:
                raise VolOwnError(f"volume {name}: docker volume create failed: {err.strip()}")
            entry["created"] = True
            attrs = inspect_volume(run, name)
            if attrs is None:
                raise VolOwnError(f"volume {name}: not found right after docker volume create")
        mountpoint = check_volume(name, attrs)
        entry["mountpoint"] = mountpoint
        top_fd = open_top(name, mountpoint)
        try:
            top = os.fstat(top_fd)
            entry["before"] = f"{top.st_uid}:{top.st_gid}"
            if _matches(top, uid, gid):
                entry["changed"] = 0
            elif dry_run:
                entry["changed"] = "would"
            else:
                try:
                    entry["changed"] = chown_tree(top_fd, uid, gid, name)
                except OSError as exc:
                    raise VolOwnError(f"volume {name}: chown failed: {exc}") from exc
        finally:
            os.close(top_fd)
        result[name] = entry
    return result


def main(argv: Sequence[str] | None = None, run: Runner = run_cmd) -> int:
    parser = argparse.ArgumentParser(prog="volown")
    parser.add_argument("action", choices=("ensure",))
    parser.add_argument("--spec", required=True, help='{"volumes": {"<name>": "<uid>[:<gid>]"}}')
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        try:
            spec = json.loads(args.spec)
        except ValueError as exc:
            raise VolOwnError(f"--spec is not JSON: {exc}") from exc
        volumes = spec.get("volumes") if isinstance(spec, dict) else None
        if not isinstance(volumes, dict):
            raise VolOwnError('--spec needs {"volumes": {"<name>": "<uid>[:<gid>]"}}')
        result = {"volumes": ensure(volumes, dry_run=args.dry_run, run=run)}
    except VolOwnError as exc:
        print(json.dumps({"error": str(exc)}))
        print(f"volown: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
