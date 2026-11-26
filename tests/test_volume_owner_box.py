"""bay.toml `[[mounts]] owner`: the box side (volown.py + volume_owners.yml).

Why it exists: when Docker creates a container on an EMPTY named volume and
the mount path exists in the image, it copies the image directory's owner
onto the volume. An app that runs as another uid then cannot write its own
volume. Bay sets the owner before the reconciler and again after it.

The helper runs as root over a tree a container can write to, so the tests
that matter most are the guards: no symlink is followed, the top directory is
changed last, a volume that is not a plain local one is refused, a remapped
daemon is refused. Tests run as non-root, so `os.chown` and `os.fchown` are
recorded instead of called.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from bay_reconcile import volown as vo

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLE = _REPO_ROOT / "roles" / "container_lifecycle"
_MAIN = _ROLE / "tasks" / "main.yml"
_TASKS = _ROLE / "tasks" / "volume_owners.yml"

_UID = os.getuid()
_GID = os.getgid()
# An owner that never matches the test user.
_OTHER = 4242 if _UID != 4242 else 4343


# ── fakes ───────────────────────────────────────────────────────────────


class FakeDocker:
    """A `docker` CLI: info, volume inspect, volume create."""

    def __init__(self, volumes: dict[str, dict[str, Any]] | None = None,
                 security: list[str] | None = None,
                 create_root: Path | None = None) -> None:
        self.volumes = volumes or {}
        self.security = security if security is not None else [
            "name=apparmor", "name=seccomp,profile=builtin", "name=cgroupns"]
        self.create_root = create_root
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["docker", "info"]:
            return 0, json.dumps(self.security) + "\n", ""
        if argv[:3] == ["docker", "volume", "inspect"]:
            name = argv[3]
            if name not in self.volumes:
                return 1, "[]\n", f"Error response from daemon: get {name}: no such volume\n"
            return 0, json.dumps([self.volumes[name]]), ""
        if argv[:3] == ["docker", "volume", "create"]:
            name = argv[3]
            assert self.create_root is not None
            data = self.create_root / name / "_data"
            data.mkdir(parents=True)
            self.volumes[name] = _attrs(name, data)
            return 0, name + "\n", ""
        raise AssertionError(f"unexpected command {argv}")


def _attrs(name: str, mountpoint: Path, **extra: Any) -> dict[str, Any]:
    attrs = {"Name": name, "Driver": "local", "Mountpoint": str(mountpoint),
             "Options": None, "Labels": None, "Scope": "local"}
    attrs.update(extra)
    return attrs


def _volume(tmp_path: Path, name: str = "mystack_app-data") -> tuple[Path, FakeDocker]:
    data = tmp_path / "volumes" / name / "_data"
    data.mkdir(parents=True)
    return data, FakeDocker({name: _attrs(name, data)}, create_root=tmp_path / "volumes")


class ChownLog:
    """Records os.chown / os.fchown in call order; changes nothing."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple] = []
        monkeypatch.setattr(vo.os, "chown", self._chown)
        monkeypatch.setattr(vo.os, "fchown", self._fchown)

    def _chown(self, path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        assert dir_fd is not None, "chown must work from a directory fd"
        assert follow_symlinks is False, "chown must never follow a symlink"
        self.calls.append(("chown", path, uid, gid, os.readlink(f"/proc/self/fd/{dir_fd}")))

    def _fchown(self, fd, uid, gid):
        self.calls.append(("fchown", os.readlink(f"/proc/self/fd/{fd}"), uid, gid))


def _run(volumes: dict[str, str], run, *, dry: bool = False, root: bool = True):
    return vo.ensure(volumes, dry_run=dry, run=run, geteuid=lambda: 0 if root else 1000)


# ── owner parsing ───────────────────────────────────────────────────────


@pytest.mark.parametrize("text,want", [
    ("1000", (1000, None)),
    ("1000:1000", (1000, 1000)),
    ("0", (0, None)),
    ("0:0", (0, 0)),
    ("4294967294:4294967294", (4294967294, 4294967294)),
])
def test_parse_owner_accepts(text, want):
    assert vo.parse_owner(text) == want


@pytest.mark.parametrize("text", [
    "", "abc", "1000:", ":1000", "-1", "1000:-1", " 1000", "1000 ", "1000:1000:1",
    "4294967295", "1000:4294967295", "99999999999", "1e3", "١٢",
])
def test_parse_owner_refuses(text):
    with pytest.raises(vo.VolOwnError):
        vo.parse_owner(text, "mystack_app-data")


# ── fast path, walk, order ──────────────────────────────────────────────


def test_top_match_changes_nothing_and_does_not_walk(tmp_path, monkeypatch):
    data, docker = _volume(tmp_path)
    (data / "sub").mkdir()
    (data / "sub" / "f").write_text("x")
    log = ChownLog(monkeypatch)
    walked = []
    monkeypatch.setattr(vo.os, "fwalk", lambda *a, **k: walked.append(1) or iter(()))
    out = _run({"mystack_app-data": f"{_UID}:{_GID}"}, docker)
    entry = out["mystack_app-data"]
    assert entry["changed"] == 0 and entry["created"] is False
    assert entry["before"] == f"{_UID}:{_GID}"
    assert entry["mountpoint"] == str(data)
    assert log.calls == [] and walked == []


def test_mismatch_walks_every_entry_and_changes_the_top_last(tmp_path, monkeypatch):
    data, docker = _volume(tmp_path)
    (data / "a").write_text("x")
    (data / "sub").mkdir()
    (data / "sub" / "b").write_text("y")
    (data / "sub" / "deep").mkdir()
    (data / "sub" / "deep" / "c").write_text("z")
    log = ChownLog(monkeypatch)
    out = _run({"mystack_app-data": f"{_OTHER}:{_OTHER}"}, docker)
    assert out["mystack_app-data"]["changed"] == 6
    assert log.calls[-1] == ("fchown", str(data), _OTHER, _OTHER)
    assert all(c[0] == "chown" for c in log.calls[:-1])
    changed = {(c[4], c[1]) for c in log.calls[:-1]}
    assert changed == {
        (str(data), "a"), (str(data), "sub"),
        (str(data / "sub"), "b"), (str(data / "sub"), "deep"),
        (str(data / "sub" / "deep"), "c"),
    }


def test_walk_skips_entries_that_already_match(tmp_path, monkeypatch):
    data, docker = _volume(tmp_path)
    (data / "mine").write_text("x")
    (data / "theirs").write_text("y")
    real_stat = vo._entry_stat

    def fake(name, dir_fd):
        st = real_stat(name, dir_fd)
        if name == "theirs":
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                                   _OTHER, _OTHER, st.st_size, 0, 0, 0))
        return st

    monkeypatch.setattr(vo, "_entry_stat", fake)
    log = ChownLog(monkeypatch)
    out = _run({"mystack_app-data": f"{_OTHER}:{_OTHER}"}, docker)
    names = [c[1] for c in log.calls if c[0] == "chown"]
    assert names == ["mine"]
    assert out["mystack_app-data"]["changed"] == 2


def test_symlink_is_changed_as_a_link_and_never_followed(tmp_path, monkeypatch):
    data, docker = _volume(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("root's")
    (data / "dirlink").symlink_to(outside)
    (data / "filelink").symlink_to(outside / "secret")
    log = ChownLog(monkeypatch)
    _run({"mystack_app-data": f"{_OTHER}:{_OTHER}"}, docker)
    chowns = [c for c in log.calls if c[0] == "chown"]
    assert {(c[4], c[1]) for c in chowns} == {(str(data), "dirlink"), (str(data), "filelink")}
    assert not any(str(outside) in c[4] or c[1] == "secret" for c in chowns)


def test_another_device_is_not_entered_or_changed(tmp_path, monkeypatch):
    data, docker = _volume(tmp_path)
    (data / "mnt").mkdir()
    (data / "mnt" / "inner").write_text("x")
    (data / "plain").write_text("y")
    real_stat = vo._entry_stat

    def fake(name, dir_fd):
        st = real_stat(name, dir_fd)
        if name == "mnt":
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, st.st_nlink,
                                   st.st_uid, st.st_gid, st.st_size, 0, 0, 0))
        return st

    monkeypatch.setattr(vo, "_entry_stat", fake)
    log = ChownLog(monkeypatch)
    _run({"mystack_app-data": f"{_OTHER}:{_OTHER}"}, docker)
    names = [c[1] for c in log.calls if c[0] == "chown"]
    assert names == ["plain"]


def test_uid_only_leaves_the_group(tmp_path, monkeypatch):
    data, docker = _volume(tmp_path)
    (data / "f").write_text("x")
    log = ChownLog(monkeypatch)
    out = _run({"mystack_app-data": str(_OTHER)}, docker)
    assert all(c[3] == -1 for c in log.calls)
    assert out["mystack_app-data"]["owner"] == str(_OTHER)
    # uid-only compares only the uid: the test user's uid matches whatever the group.
    log.calls.clear()
    out = _run({"mystack_app-data": str(_UID)}, docker)
    assert out["mystack_app-data"]["changed"] == 0 and log.calls == []


def test_vanished_entry_is_skipped(tmp_path, monkeypatch):
    data, docker = _volume(tmp_path)
    (data / "gone").write_text("x")
    (data / "kept").write_text("y")
    real_stat = vo._entry_stat

    def fake(name, dir_fd):
        if name == "gone":
            raise FileNotFoundError(name)
        return real_stat(name, dir_fd)

    monkeypatch.setattr(vo, "_entry_stat", fake)
    log = ChownLog(monkeypatch)
    out = _run({"mystack_app-data": f"{_OTHER}:{_OTHER}"}, docker)
    assert [c[1] for c in log.calls if c[0] == "chown"] == ["kept"]
    assert out["mystack_app-data"]["changed"] == 2


def test_other_oserror_in_the_walk_is_an_error(tmp_path, monkeypatch):
    data, docker = _volume(tmp_path)
    (data / "bad").write_text("x")

    def fake(name, dir_fd):
        raise PermissionError(name)

    monkeypatch.setattr(vo, "_entry_stat", fake)
    ChownLog(monkeypatch)
    with pytest.raises(vo.VolOwnError, match="mystack_app-data"):
        _run({"mystack_app-data": f"{_OTHER}:{_OTHER}"}, docker)


# ── dry run, create ─────────────────────────────────────────────────────


def test_dry_run_changes_nothing_and_says_would(tmp_path, monkeypatch):
    data, docker = _volume(tmp_path)
    (data / "f").write_text("x")
    log = ChownLog(monkeypatch)
    walked = []
    monkeypatch.setattr(vo.os, "fwalk", lambda *a, **k: walked.append(1) or iter(()))
    out = _run({"mystack_app-data": f"{_OTHER}:{_OTHER}"}, docker, dry=True, root=False)
    assert out["mystack_app-data"]["changed"] == "would"
    assert out["mystack_app-data"]["before"] == f"{_UID}:{_GID}"
    assert log.calls == [] and walked == []
    out = _run({"mystack_app-data": f"{_UID}:{_GID}"}, docker, dry=True, root=False)
    assert out["mystack_app-data"]["changed"] == 0


def test_absent_volume_is_created_then_set(tmp_path, monkeypatch):
    docker = FakeDocker(create_root=tmp_path / "volumes")
    log = ChownLog(monkeypatch)
    out = _run({"mystack_new": f"{_OTHER}:{_OTHER}"}, docker)
    assert ["docker", "volume", "create", "mystack_new"] in docker.calls
    entry = out["mystack_new"]
    assert entry["created"] is True and entry["changed"] == 1
    assert entry["mountpoint"] == str(tmp_path / "volumes" / "mystack_new" / "_data")
    assert log.calls[-1][0] == "fchown"


def test_absent_volume_in_dry_run_is_not_created(tmp_path, monkeypatch):
    docker = FakeDocker(create_root=tmp_path / "volumes")
    log = ChownLog(monkeypatch)
    out = _run({"mystack_new": "1000"}, docker, dry=True)
    assert out == {"mystack_new": {"mountpoint": None, "created": "would", "before": None,
                                   "owner": "1000", "changed": "would"}}
    assert not any(c[:3] == ["docker", "volume", "create"] for c in docker.calls)
    assert log.calls == []


# ── refusals (through main: exit 1, one error document) ─────────────────


def _main(capsys, volumes: dict[str, str], docker, *, dry: bool = False) -> tuple[int, dict]:
    argv = ["ensure", "--spec", json.dumps({"volumes": volumes})]
    if dry:
        argv.insert(1, "--dry-run")
    rc = vo.main(argv, run=docker)
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, out
    return rc, json.loads(lines[0])


@pytest.mark.parametrize("change", [
    {"Driver": "nfs-plugin"},
    {"Options": {"type": "none", "o": "bind", "device": "/srv/x"}},
    {"Options": {"type": "nfs", "o": "addr=203.0.113.1", "device": ":/export"}},
])
def test_refuses_a_volume_that_is_not_plain_local(tmp_path, monkeypatch, capsys, change):
    data, docker = _volume(tmp_path)
    docker.volumes["mystack_app-data"].update(change)
    log = ChownLog(monkeypatch)
    rc, doc = _main(capsys, {"mystack_app-data": "1000"}, docker, dry=True)
    assert rc == 1 and set(doc) == {"error"} and "mystack_app-data" in doc["error"]
    assert log.calls == []


@pytest.mark.parametrize("mountpoint", [
    "relative/mystack_app-data/_data",
    "/var/lib/docker/volumes/mystack_app-data/data",
    "/var/lib/docker/volumes/other/_data",
    "/var/lib/docker/volumes/mystack_app-data",
    "/",
])
def test_refuses_a_mountpoint_that_is_not_name_data(tmp_path, monkeypatch, capsys, mountpoint):
    data, docker = _volume(tmp_path)
    docker.volumes["mystack_app-data"]["Mountpoint"] = mountpoint
    log = ChownLog(monkeypatch)
    rc, doc = _main(capsys, {"mystack_app-data": "1000"}, docker, dry=True)
    assert rc == 1 and "mystack_app-data" in doc["error"]
    want = "not an absolute path" if not mountpoint.startswith("/") else "does not end in"
    assert want in doc["error"]
    assert log.calls == []


def test_refuses_a_symlinked_mountpoint(tmp_path, monkeypatch, capsys):
    real = tmp_path / "elsewhere"
    real.mkdir()
    holder = tmp_path / "volumes" / "mystack_app-data"
    holder.mkdir(parents=True)
    (holder / "_data").symlink_to(real)
    docker = FakeDocker({"mystack_app-data": _attrs("mystack_app-data", holder / "_data")})
    log = ChownLog(monkeypatch)
    rc, doc = _main(capsys, {"mystack_app-data": "1000"}, docker, dry=True)
    assert rc == 1 and "cannot open" in doc["error"]
    assert log.calls == []


@pytest.mark.parametrize("option", ["name=userns", "name=rootless"])
def test_refuses_a_remapped_daemon(tmp_path, monkeypatch, capsys, option):
    data, docker = _volume(tmp_path)
    docker.security = ["name=seccomp,profile=builtin", option]
    rc, doc = _main(capsys, {"mystack_app-data": "1000"}, docker, dry=True)
    assert rc == 1 and option in doc["error"]
    assert not any(c[:2] == ["docker", "volume"] for c in docker.calls)


def test_refuses_non_root_without_dry_run(tmp_path, monkeypatch, capsys):
    data, docker = _volume(tmp_path)
    log = ChownLog(monkeypatch)
    monkeypatch.setattr(vo.os, "geteuid", lambda: 1000)
    rc, doc = _main(capsys, {"mystack_app-data": "1000"}, docker)
    assert rc == 1 and "root" in doc["error"]
    assert docker.calls == [] and log.calls == []


def test_bad_owner_is_refused_before_docker(tmp_path, capsys):
    data, docker = _volume(tmp_path)
    rc, doc = _main(capsys, {"mystack_app-data": "app:app"}, docker, dry=True)
    assert rc == 1 and "mystack_app-data" in doc["error"]
    assert docker.calls == []


def test_success_prints_one_document(tmp_path, capsys):
    data, docker = _volume(tmp_path)
    rc, doc = _main(capsys, {"mystack_app-data": f"{_UID}:{_GID}"}, docker, dry=True)
    assert rc == 0
    assert doc == {"volumes": {"mystack_app-data": {
        "mountpoint": str(data), "created": False, "before": f"{_UID}:{_GID}",
        "owner": f"{_UID}:{_GID}", "changed": 0}}}


def test_module_is_stdlib_only_and_imports_nothing_from_its_package():
    text = (_REPO_ROOT / "src" / "bay_reconcile" / "volown.py").read_text()
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", text, flags=re.M)
    allowed = {"__future__", "argparse", "json", "os", "re", "stat", "subprocess", "sys",
               "collections.abc", "typing"}
    assert set(imports) <= allowed, set(imports) - allowed


# ── the role ────────────────────────────────────────────────────────────


def _walk(tasks):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key))


def _role_tasks() -> list[dict]:
    return list(_walk(yaml.safe_load(_TASKS.read_text())))


def _script_task() -> dict:
    return next(t for t in _role_tasks() if "ansible.builtin.script" in t)


def test_script_runs_as_root_outside_check_mode_with_a_one_line_cmd():
    task = _script_task()
    assert task["become"] is True and task["become_user"] == "root"
    assert task["check_mode"] is False
    body = task["ansible.builtin.script"]
    assert body["executable"] == "python3"
    cmd = body["cmd"]
    assert "\n" not in cmd
    assert cmd.startswith("{{ playbook_dir }}/src/bay_reconcile/volown.py ensure ")
    assert "{{ _volown_dry_flag }}" in cmd
    flag = task["vars"]["_volown_dry_flag"]
    assert "--dry-run" in flag and "ansible_check_mode" in flag
    assert "bay_reconciler_plan_only" in flag
    assert task["vars"]["_volown_ensure_spec"] == {"volumes": "{{ _volown_spec }}"}
    assert "from_json" in task["changed_when"]
    assert task["changed_when"].lstrip().startswith("_volown_run.rc == 0 and")


def test_every_task_has_a_name_and_the_file_starts_with_dashes():
    assert _TASKS.read_text().startswith("---\n")
    assert all(t.get("name") for t in _role_tasks())


def test_script_and_warning_skip_an_empty_spec():
    top = yaml.safe_load(_TASKS.read_text())
    block = next(t for t in top if "block" in t)
    assert block["when"] == "_volown_spec | length > 0"
    assert any("ansible.builtin.script" in t for t in block["block"])


def _render_spec(volume_owners, services, accessories, stack="mystack"):
    from jinja2 import Environment

    task = yaml.safe_load(_TASKS.read_text())[0]
    tmpl = task["ansible.builtin.set_fact"]["_volown_spec"]
    out = Environment().from_string(tmpl).render(
        volume_owners=volume_owners, active_services=services,
        active_accessories=accessories, stack_name=stack)
    import ast

    return ast.literal_eval(out.strip())


def test_spec_holds_only_this_hosts_volumes_with_the_stack_prefix():
    spec = _render_spec(
        {"app-files": {"container": "app", "owner": "1000:1000"},
         "db-data": {"container": "db", "owner": "999"},
         "elsewhere": {"container": "other-box-app", "owner": "1000"}},
        {"app": {}}, {"db": {}})
    assert spec == {"mystack_app-files": "1000:1000", "mystack_db-data": "999"}
    assert _render_spec({}, {"app": {}}, {}) == {}


def test_warning_runs_only_in_the_after_pass():
    warn = next(t for t in _role_tasks() if "ansible.builtin.debug" in t)
    assert warn["when"] == "_volown_pass | default('before') == 'after'"
    msg = warn["ansible.builtin.debug"]["msg"]
    for phrase in ("Docker copied the owner", "again after", "write", "Restart the container",
                   "same owner"):
        assert phrase in msg
    assert "value.changed" in warn["loop"] and "'gt', 0" in warn["loop"]


def test_main_includes_owners_before_and_after_the_reconciler():
    tasks = yaml.safe_load(_MAIN.read_text())
    includes = [t.get("ansible.builtin.include_tasks") for t in tasks]
    rec = includes.index("reconcile.yml")
    owners = [i for i, name in enumerate(includes) if name == "volume_owners.yml"]
    assert len(owners) == 2
    before, after = owners
    assert before < rec < after
    full_pass = "container_lifecycle_only | default([]) | length == 0"
    assert tasks[before]["when"] == full_pass
    assert tasks[before]["vars"] == {"_volown_pass": "before"}
    assert tasks[after]["vars"] == {"_volown_pass": "after"}
    assert tasks[after]["when"] == [
        full_pass, "not ansible_check_mode",
        "not (bay_reconciler_plan_only | default(false) | bool)"]
