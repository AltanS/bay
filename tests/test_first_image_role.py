"""The first image build of ``bay up`` in the git_deploy role (2.27.0).

``bay up`` runs the playbook with the tag ``first_image`` and the extra var
``bay_first_image`` ({container: commit or null}) when its plan has an
``image``/``create`` step. roles/git_deploy/tasks/main.yml includes
first_image.yml with the tags [never, first_image], so nothing else runs it.

The static tests read the role YAML. The run tests drive the real role on
localhost with ``--tags first_image``: real git (file:// repos), a stub
``docker`` that logs its calls, and the build server on localhost. They prove
the commit pin of remote_build.yml, the local path, the failure messages and
the safety invariant: only the named services are cloned, built or pulled.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "roles" / "git_deploy" / "tasks"
FILES = ("first_image.yml", "first_image_remote.yml", "first_image_local.yml")

sys.path.insert(0, str(ROOT / "filter_plugins"))
from bay_filters import bay_first_image_plan  # noqa: E402


def _load(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load((TASKS / name).read_text())


def _walk(tasks: list[dict[str, Any]] | None):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key))


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _include(task: dict[str, Any]) -> dict[str, Any] | None:
    inc = task.get("ansible.builtin.include_tasks")
    if isinstance(inc, str):
        return {"file": inc}
    return inc if isinstance(inc, dict) else None


# ── static: the include and the tags ─────────────────────────────────────────


def test_main_includes_first_image_only_with_the_tag_and_the_var() -> None:
    main = _load("main.yml")
    found = [t for t in main if (_include(t) or {}).get("file") == "first_image.yml"]
    assert len(found) == 1
    task = found[0]
    # `never`: a full bay deploy (no --tags) and every other selection skip it.
    assert set(_tags(task.get("tags"))) == {"never", "first_image"}
    assert _tags(task["ansible.builtin.include_tasks"]["apply"]["tags"]) == ["first_image"]
    when = [str(w) for w in _tags(task.get("when"))]
    assert "bay_first_image | default({}) | length > 0" in when
    assert "not ansible_check_mode" in when
    # No other task of main.yml carries the tag: nothing else starts under bay up.
    others = [
        t for t in _walk(main) if t is not task and "first_image" in _tags(t.get("tags"))
    ]
    assert others == []


def test_every_nested_include_keeps_the_tag() -> None:
    """Under ``--tags first_image`` a nested include runs only with ``apply.tags``."""
    for name in FILES:
        for task in _walk(_load(name)):
            inc = _include(task)
            if inc is None:
                continue
            assert "first_image" in _tags((inc.get("apply") or {}).get("tags")), (
                name,
                task.get("name"),
            )


def test_first_image_files_use_no_collection_module() -> None:
    """The files run without vendored collections (the run tests rely on it)."""
    for name in FILES:
        for task in _walk(_load(name)):
            modules = [k for k in task if "." in k and not k.startswith("ansible.builtin.")]
            assert modules == [], (name, task.get("name"), modules)


def test_remote_build_pins_before_it_reads_the_commit() -> None:
    names = [t.get("name", "") for t in _load("remote_build.yml")]
    reset_head = names.index("{{ _svc_name }} — Reset to fetched HEAD (force-push safe)")
    pin = names.index("{{ _svc_name }} — Reset to the pinned commit")
    sha = names.index("{{ _svc_name }} — Get current commit SHA")
    assert reset_head < pin < sha
    for task in _load("remote_build.yml"):
        if "pinned commit" in task.get("name", ""):
            assert "_pin_commit | default('') | length > 0" in _tags(task.get("when"))


# ── the filter ───────────────────────────────────────────────────────────────


def _svc(strategy: str | None, image: str | None = None, **build: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"build": {"repo": "https://git.example/o/r", **build}}
    if strategy is not None:
        out["build"]["strategy"] = strategy
    if image:
        out["image"] = image
    return out


def test_filter_names_only_the_targets() -> None:
    services = {
        "new": _svc("remote", "reg.example/new:latest"),
        "other": _svc("remote", "reg.example/other:latest"),
        "loc": _svc(None),
        "oldloc": _svc("local"),
        "web": _svc("registry", "reg.example/web:latest"),
    }
    sha = "a" * 40
    plan = bay_first_image_plan(services, {"new": sha, "loc": None, "web": None})
    assert plan["remote"] == ["new"]
    assert plan["pull"] == ["reg.example/new:latest", f"reg.example/new:{'a' * 12}"]
    assert plan["local"] == ["loc"]
    assert plan["strategy"] == {"new": "remote", "loc": "local", "web": "registry"}
    # Another host: the remote build still runs once, nothing is pulled or built here.
    there = bay_first_image_plan(services, {"new": sha, "loc": None}, active=["other"])
    assert there["remote"] == ["new"] and there["pull"] == [] and there["local"] == []
    # push is the old name of remote; the default strategy fills a missing one.
    old = bay_first_image_plan({"x": _svc("push", "reg.example/x:latest")}, {"x": None})
    assert old["remote"] == ["x"] and old["pull"] == ["reg.example/x:latest"]
    dflt = bay_first_image_plan({"x": _svc(None, "reg.example/x:latest")}, {"x": None}, "remote")
    assert dflt["remote"] == ["x"]


def test_filter_shared_build_is_pulled_or_retagged_not_built() -> None:
    services = {
        "web": _svc("remote", "reg.example/web:latest"),
        "web-de": _svc("remote", "reg.example/web:latest", shared_from="web"),
        "app": _svc("local"),
        "app-de": _svc("local", shared_from="app"),
    }
    plan = bay_first_image_plan(services, {"web-de": None, "web": None, "app-de": None, "app": None})
    assert plan["remote"] == ["web"]
    assert plan["pull"] == ["reg.example/web:latest"]
    # The main container first: the shared one re-tags its image.
    assert plan["local"] == ["app", "app-de"]


# ── run tests: the real role on localhost ────────────────────────────────────

_DOCKER_STUB = """#!/bin/sh
echo "$*" >> "$BAYSCRATCH_DOCKER_LOG"
case "$1 $2" in
  "manifest inspect") exit 1;;
  "image inspect") exit 1;;
  "buildx build") [ -n "$BAYSCRATCH_BUILD_FAILS" ] && exit 1; exit 0;;
  "tag "*) case "$2" in *:latest) exit 1;; esac; exit 0;;
esac
exit 0
"""


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


class World:
    """Four origins (two named, two not), a stub docker, and the role vars."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.commits: dict[str, list[str]] = {}
        for name in ("new", "other", "newlocal", "otherlocal"):
            src = tmp / "src" / name
            src.mkdir(parents=True)
            _git(src, "init", "-q", "-b", "main")
            (src / "Dockerfile").write_text("FROM scratch\n")
            (src / "f").write_text("one\n")
            _git(src, "add", "-A")
            _git(src, "commit", "-q", "-m", "c1")
            (src / "f").write_text("two\n")
            _git(src, "commit", "-qam", "c2")
            self.commits[name] = [_git(src, "rev-parse", "HEAD~1"), _git(src, "rev-parse", "HEAD")]
            _git(tmp, "clone", "-q", "--bare", str(src), str(tmp / "origin" / f"{name}.git"))
        bindir = tmp / "bin"
        bindir.mkdir()
        stub = bindir / "docker"
        stub.write_text(_DOCKER_STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        helper = tmp / "stack" / "bin" / "select-builder.sh"
        helper.parent.mkdir(parents=True)
        helper.write_text("#!/bin/sh\necho argo-builder\n")  # kept-argo: live builder name
        helper.chmod(0o755)
        self.log = tmp / "docker.log"

    def origin(self, name: str) -> str:
        return f"file://{self.tmp / 'origin' / name}.git"

    def services(self) -> dict[str, Any]:
        def svc(repo: str, strategy: str, image: str | None = None) -> dict[str, Any]:
            out: dict[str, Any] = {
                "build": {"repo": self.origin(repo), "branch": "main", "strategy": strategy}
            }
            if image:
                out["image"] = image
            return out

        return {
            "newapp": svc("new", "remote", "reg.example/bayscratch-newapp:latest"),
            "otherapp": svc("other", "remote", "reg.example/bayscratch-otherapp:latest"),
            "newlocal": svc("newlocal", "local"),
            "otherlocal": svc("otherlocal", "local"),
        }

    def run(
        self,
        first: dict[str, str | None],
        *,
        tags: str = "first_image",
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        user = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        stack = self.tmp / "stack"
        extra = {
            "services": self.services(),
            "bay_first_image": first,
            "stack_dir": str(stack),
            "stack_name": "bayscratch",
            "app_user": user,
            "app_user_group": group,
            "build_server": "localhost",
            "git_deploy_buildkit_memory_max": "",
            "git_deploy_buildkit_image": "",
            "git_deploy_remote_build_dir": str(stack / "push-builds"),
            "git_deploy_build_keep_tags": 0,
            # Task keywords say become; a connection var overrides them here.
            "ansible_become": False,
        }
        (self.tmp / "vars.json").write_text(json.dumps(extra))
        (self.tmp / "ansible.cfg").write_text("[defaults]\n")
        (self.tmp / "play.yml").write_text(
            "- hosts: localhost\n"
            "  connection: local\n"
            "  gather_facts: false\n"
            "  roles:\n"
            "    - {role: git_deploy, tags: [build, git_deploy]}\n"
        )
        self.log.write_text("")
        run_env = {
            **os.environ,
            "PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            "BAYSCRATCH_DOCKER_LOG": str(self.log),
            "ANSIBLE_CONFIG": str(self.tmp / "ansible.cfg"),
            "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
            "ANSIBLE_FILTER_PLUGINS": str(ROOT / "filter_plugins"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_LOCALHOST_WARNING": "0",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            **(env or {}),
        }
        cmd = [
            sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
            "-e", f"@{self.tmp / 'vars.json'}", "--tags", tags, str(self.tmp / "play.yml"),
        ]
        if check:
            cmd.append("--check")
        return subprocess.run(
            cmd, cwd=self.tmp, env=run_env, capture_output=True, text=True, timeout=300
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _out(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout[-4000:] + proc.stderr[-2000:]


def test_builds_only_the_named_services_at_the_pin(world: World) -> None:
    """The safety invariant and the commit pin, on both strategies."""
    remote_pin = world.commits["new"][0]
    local_pin = world.commits["newlocal"][0]
    proc = world.run({"newapp": remote_pin, "newlocal": local_pin})
    assert proc.returncode == 0, _out(proc)
    calls = world.calls()
    joined = "\n".join(calls)
    # Nothing of the other two services: no clone, no build, no pull.
    assert "otherapp" not in joined and "otherlocal" not in joined, joined
    stack = world.tmp / "stack"
    assert sorted(p.name for p in (stack / "push-builds").iterdir()) == ["newapp"]
    shared = [p.name for p in (stack / "builds" / "shared").iterdir()]
    assert len(shared) == 1 and shared[0].startswith("newlocal-"), shared
    assert not (stack / "builds" / "otherlocal").exists()
    # The remote image is the pin's, with its label, and is pulled here.
    r12 = remote_pin[:12]
    build = next(c for c in calls if c.startswith("buildx build") and "--push" in c)
    assert f"-t reg.example/bayscratch-newapp:{r12}" in build
    assert f"--label com.bay.commit={r12}" in build
    assert f"pull reg.example/bayscratch-newapp:{r12}" in calls
    assert "pull reg.example/bayscratch-newapp:latest" in calls
    checkout = stack / "push-builds" / "newapp" / "repo"
    assert _git(checkout, "rev-parse", "HEAD") == remote_pin
    # The local image is the pin's too.
    l12 = local_pin[:12]
    local = next(c for c in calls if c.startswith("buildx build") and "--load" in c)
    assert f"-t argo-bayscratch-newlocal:{l12}" in local  # kept-argo: live build tag
    assert f"--label com.bay.commit={l12}" in local
    assert len([c for c in calls if c.startswith("buildx build")]) == 2


def test_second_run_fetches_then_pins(world: World) -> None:
    """An existing checkout: fetch, reset to FETCH_HEAD, then reset to the pin."""
    pin = world.commits["new"][0]
    assert world.run({"newapp": None}).returncode == 0
    checkout = world.tmp / "stack" / "push-builds" / "newapp" / "repo"
    # No commit: the head of the branch, as the full deploy builds.
    assert _git(checkout, "rev-parse", "HEAD") == world.commits["new"][1]
    proc = world.run({"newapp": pin})
    assert proc.returncode == 0, _out(proc)
    assert _git(checkout, "rev-parse", "HEAD") == pin


def test_pin_off_the_branch_is_fetched_by_itself(world: World) -> None:
    """A pin the branch fetch does not hold is fetched by its id."""
    src = world.tmp / "src" / "new"
    _git(src, "checkout", "-q", "-b", "side")
    (src / "f").write_text("side\n")
    _git(src, "commit", "-qam", "side")
    side = _git(src, "rev-parse", "HEAD")
    _git(src, "push", "-q", str(world.tmp / "origin" / "new.git"), "side")
    proc = world.run({"newapp": side})
    assert proc.returncode == 0, _out(proc)
    checkout = world.tmp / "stack" / "push-builds" / "newapp" / "repo"
    assert _git(checkout, "rev-parse", "HEAD") == side
    assert f"--label com.bay.commit={side[:12]}" in "\n".join(world.calls())


def test_a_failed_build_names_the_service_and_the_commit(world: World) -> None:
    pin = world.commits["new"][0]
    proc = world.run({"newapp": pin}, env={"BAYSCRATCH_BUILD_FAILS": "1"})
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert f"first image of newapp: the build of {pin[:12]} on the build server" in out
    assert "bay up stops before the container pass" in out
    # It stopped: no pull after the failed push.
    assert not any(c.startswith("pull ") for c in world.calls())

    local = world.commits["newlocal"][0]
    proc = world.run({"newlocal": local}, env={"BAYSCRATCH_BUILD_FAILS": "1"})
    assert proc.returncode != 0
    assert f"first image of newlocal: the build of {local[:12]} on this box failed" in (
        proc.stdout + proc.stderr
    )


def test_a_refused_clone_skips_the_build_and_prints_the_key(world: World) -> None:
    """A local repo the box cannot clone: no build, the key, and the run goes on (exit 40)."""
    services = world.services()
    world.services = lambda: {  # type: ignore[method-assign]
        **services,
        "newlocal": {
            "build": {
                "repo": f"file://{world.tmp / 'origin' / 'missing'}.git",
                "branch": "main",
                "strategy": "local",
            }
        },
    }
    proc = world.run({"newlocal": None})
    assert proc.returncode == 0, _out(proc)
    out = proc.stdout
    assert "FIRST IMAGE OF newlocal NOT BUILT" in out
    assert "Add this public key as a read-only deploy key" in out
    assert "ssh-ed25519 " in out
    assert "FIRST IMAGE NOT BUILT: newlocal" in out
    assert not any(c.startswith("buildx build") for c in world.calls())


def test_nothing_runs_without_the_var_the_tag_or_in_check_mode(world: World) -> None:
    pin = world.commits["new"][0]
    for proc in (
        world.run({}),
        world.run({"newapp": pin}, check=True),
        # bay up without a first image step: the tag is not asked for.
        world.run({"newapp": pin}, tags="first_image_other"),
    ):
        assert proc.returncode == 0, _out(proc)
        assert "First image" not in proc.stdout
        assert world.calls() == []
    assert not (world.tmp / "stack" / "push-builds").exists()


def test_without_a_set_up_build_server_it_stops_with_the_way_out(world: World) -> None:
    (world.tmp / "stack" / "bin" / "select-builder.sh").unlink()
    proc = world.run({"newapp": world.commits["new"][0]})
    assert proc.returncode != 0
    assert "was never" in proc.stdout and "set up for remote builds" in proc.stdout
    assert world.calls() == []
