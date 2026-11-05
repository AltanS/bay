"""The GitHub PAT must never enter a git URL.

It used to: `https://x-access-token:<PAT>@host/path` was rendered into
rebuild.sh, into three tasks in clone_repos.yml and into three more in
remote_build.yml. Two consequences, neither fixed by `no_log`:

  * rebuild.sh runs those strings through `eval`, so the PAT was an argv
    element of every git child process — any local user could read it out of
    /proc/<pid>/cmdline for the duration of a clone or fetch.
  * `git remote set-url` writes the URL into .git/config, permanently, under a
    build tree that was created 0755.

Authentication now goes through a GIT_ASKPASS helper: git runs it and reads
the answer from stdout. The URL is the plain HTTPS one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GIT_DEPLOY = _REPO_ROOT / "roles" / "git_deploy"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402
from test_rebuild_config import (  # noqa: E402
    _render_rebuild_sh,
    _remote_service_with_token,
)

_SENTINEL_PAT = "ghp_SENTINEL_PAT_0f1e2d"


def _local_service_with_token() -> dict:
    return {
        "api": {
            "build": {
                "repo": "git@github.com:acmecorp/api.git",
                "branch": "main",
                "token": _SENTINEL_PAT,
            },
            "access": "public",
            "domains": ["api.example.com"],
            "ports": {"internal": 3000},
        }
    }


def _remote_service_with_sentinel() -> dict:
    services = _remote_service_with_token()
    services["animals"]["build"]["token"] = _SENTINEL_PAT
    return services


# ── rebuild.sh ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "services,rebuild",
    [
        (_local_service_with_token(), ["api"]),
        (_remote_service_with_sentinel(), ["animals"]),
    ],
    ids=["local", "remote"],
)
def test_rendered_rebuild_never_contains_the_pat(services, rebuild):
    rendered = _render_rebuild_sh(services, rebuild, git_deploy_services=rebuild)
    assert _SENTINEL_PAT not in rendered, (
        "the PAT is rendered into rebuild.sh and reaches git's argv through eval"
    )
    assert "x-access-token:" not in rendered


@pytest.mark.parametrize(
    "services,rebuild",
    [
        (_local_service_with_token(), ["api"]),
        (_remote_service_with_sentinel(), ["animals"]),
    ],
    ids=["local", "remote"],
)
def test_rendered_rebuild_exports_git_askpass(services, rebuild):
    rendered = _render_rebuild_sh(services, rebuild, git_deploy_services=rebuild)
    assert "GIT_ASKPASS=" in rendered
    assert "GIT_TERMINAL_PROMPT=0" in rendered, (
        "without this a missing helper hangs the build on an interactive prompt"
    )


def test_askpass_path_matches_the_task_that_writes_it():
    """rebuild.sh only names the helper; Ansible is what renders it.

    A mismatch here is silent until a build actually needs to authenticate.
    """
    rendered = _render_rebuild_sh(
        _local_service_with_token(), ["api"], git_deploy_services=["api"]
    )
    assert "/builds/.git-askpass-" in rendered, rendered[:0]
    rendered_remote = _render_rebuild_sh(
        _remote_service_with_sentinel(), ["animals"], git_deploy_services=["animals"]
    )
    assert "/push-builds/.git-askpass-animals" in rendered_remote

    clone = (_GIT_DEPLOY / "tasks" / "clone_repos.yml").read_text()
    assert "{{ git_deploy_build_dir }}/.git-askpass-{{ _repo_group.slug }}" in clone
    remote = (_GIT_DEPLOY / "tasks" / "remote_build.yml").read_text()
    assert "{{ git_deploy_remote_build_dir }}/.git-askpass-{{ _svc_name }}" in remote


def test_rendered_rebuild_still_parses():
    rendered = _render_rebuild_sh(
        _local_service_with_token(), ["api"], git_deploy_services=["api"]
    )
    proc = subprocess.run(
        ["bash", "-n"], input=rendered, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


# ── The helper itself ────────────────────────────────────────────────────


def _render_helper(tmp_path: Path, token: str = _SENTINEL_PAT) -> Path:
    env = make_ansible_env(_GIT_DEPLOY / "templates")
    out = env.get_template("git-askpass.sh.j2").render(
        ansible_managed="test render",
        app_user="deploy",
        _askpass_token=token,
    )
    helper = tmp_path / "askpass"
    helper.write_text(out)
    helper.chmod(0o700)
    return helper


def test_helper_answers_both_prompts(tmp_path):
    """git asks for the username first, then the password, on the same script."""
    helper = _render_helper(tmp_path)
    user = subprocess.run(
        [str(helper), "Username for 'https://github.com': "],
        capture_output=True,
        text=True,
        check=True,
    )
    assert user.stdout.strip() == "x-access-token"
    password = subprocess.run(
        [str(helper), "Password for 'https://x-access-token@github.com': "],
        capture_output=True,
        text=True,
        check=True,
    )
    assert password.stdout.strip() == _SENTINEL_PAT


@pytest.mark.parametrize(
    "token",
    [
        "hz'; id; 'hz",
        "hz$(id)hz",
        "hz`id`hz",
        'hz" ; id ; "hz',
        "hz\\hz",
    ],
    ids=["single_quote", "cmdsub", "backtick", "double_quote", "backslash"],
)
def test_a_hostile_token_is_returned_verbatim_and_never_executed(tmp_path, token):
    """The PAT is opaque consumer text, so it is `| quote`d, not hand-quoted.

    It used to be pasted between two hand-written single quotes. A token
    holding a `'` closed the literal and the remainder ran as the build user,
    on every git authentication.
    """
    helper = _render_helper(tmp_path, token=token)
    proc = subprocess.run(
        [str(helper), "Password for 'https://x-access-token@github.com': "],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout == token + "\n", "the token must survive byte for byte"
    assert "uid=" not in proc.stdout, "`id` ran"


def test_helper_still_parses_with_a_hostile_token(tmp_path):
    helper = _render_helper(tmp_path, token="hz'; id; 'hz")
    proc = subprocess.run(
        ["bash", "-n", str(helper)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_helper_is_installed_private():
    """It is the one file that legitimately holds the PAT."""
    clone = yaml.safe_load((_GIT_DEPLOY / "tasks" / "clone_repos.yml").read_text())
    task = next(
        t
        for t in clone
        if t.get("ansible.builtin.template", {}).get("src") == "git-askpass.sh.j2"
    )
    assert task["ansible.builtin.template"]["mode"] == "0700"
    assert task["ansible.builtin.template"]["owner"] == "{{ app_user }}"
    assert task.get("no_log") is True


# ── The Ansible call sites ───────────────────────────────────────────────


@pytest.mark.parametrize("task_file", ["clone_repos.yml", "remote_build.yml"])
def test_no_token_url_survives_in_tasks(task_file):
    src = (_GIT_DEPLOY / "tasks" / task_file).read_text()
    assert "x-access-token:" not in src


@pytest.mark.parametrize("task_file", ["clone_repos.yml", "remote_build.yml"])
def test_every_token_git_task_gets_the_helper(task_file):
    """A git task that authenticates but has no GIT_ASKPASS will just hang."""
    tasks = yaml.safe_load((_GIT_DEPLOY / "tasks" / task_file).read_text())
    offenders = []
    for task in tasks:
        cmd = (task.get("ansible.builtin.command") or {})
        if not isinstance(cmd, dict):
            continue
        line = str(cmd.get("cmd", ""))
        if not line.lstrip().startswith("git"):
            continue
        conditions = [str(c).strip() for c in (task.get("when") or [])]
        authenticates = any(
            c in ("_repo_group.has_token", "_build.token is defined")
            for c in conditions
        )
        if not authenticates:
            continue
        # `remote set-url` only rewrites config; it never contacts the remote.
        if "set-url" in line:
            continue
        env = task.get("environment") or {}
        if "GIT_ASKPASS" not in env or env.get("GIT_TERMINAL_PROMPT") != "0":
            offenders.append(task.get("name"))
    assert not offenders, f"token git tasks without GIT_ASKPASS: {offenders}"


def test_build_directories_are_private():
    """.git/config held the PAT; the tree holding it was created 0755."""
    clone = yaml.safe_load((_GIT_DEPLOY / "tasks" / "clone_repos.yml").read_text())
    repo_dir = next(
        t
        for t in clone
        if t.get("ansible.builtin.file", {}).get("state") == "directory"
    )
    assert repo_dir["ansible.builtin.file"]["mode"] == "0700"

    main = (_GIT_DEPLOY / "tasks" / "main.yml").read_text()
    assert 'path: "{{ git_deploy_build_dir }}"' in main
    build_dir_block = main.split('path: "{{ git_deploy_build_dir }}"')[1][:300]
    assert '"0700"' in build_dir_block


# ── The HMAC key, same class of leak ─────────────────────────────────────


def test_rebuild_hmac_key_comes_from_a_file_not_argv():
    services = _remote_service_with_sentinel()
    services["animals"]["regions"] = ["eu"]
    rendered = _render_rebuild_sh(
        services,
        ["animals"],
        git_deploy_services=["animals"],
        peer_urls={"eu": "https://eu.example.com"},
        webhook_secret="SENTINEL-WEBHOOK-SECRET",
    )
    assert "-hmac '" not in rendered, (
        "the webhook secret is an argv element on every rebuild"
    )
    assert "-macopt hexkey:" in rendered


def test_webhook_hmac_key_file_is_written_private():
    tasks = yaml.safe_load((_GIT_DEPLOY / "tasks" / "render_webhook.yml").read_text())
    flat = [s for t in tasks for s in [t, *(t.get("block") or [])]]
    task = next(
        t
        for t in flat
        if str(t.get("ansible.builtin.copy", {}).get("dest", "")).endswith("hmac.key")
    )
    assert task["ansible.builtin.copy"]["mode"] == "0600"
    assert task["ansible.builtin.copy"]["owner"] == "{{ app_user }}"
    assert task.get("no_log") is True


def test_hexkey_filter_round_trips():
    sys.path.insert(0, str(_REPO_ROOT / "filter_plugins"))
    from bay_filters import bay_hexkey

    assert bay_hexkey("abc") == "616263"
    assert bytes.fromhex(bay_hexkey("s3cr3t")).decode() == "s3cr3t"


def test_openssl_accepts_the_rendered_key(tmp_path):
    """The signature must still match what the receiver computes."""
    import hashlib
    import hmac

    sys.path.insert(0, str(_REPO_ROOT / "filter_plugins"))
    from bay_filters import bay_hexkey

    secret = "SENTINEL-WEBHOOK-SECRET"
    key_file = tmp_path / "hmac.key"
    key_file.write_text(bay_hexkey(secret))
    body = b'{"image":"registry/app:latest"}'
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'printf %s \'{body.decode()}\' | openssl dgst -sha256 -mac HMAC '
            f'-macopt "hexkey:$(cat {key_file})" -binary | xxd -p -c 256',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"openssl unavailable or too old: {proc.stderr}")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert proc.stdout.strip() == expected


# ── Build args, same class of leak ───────────────────────────────────────


def test_build_args_are_passed_by_name_not_by_value():
    """`--build-arg KEY=<vault value>` put the value on buildx's argv.

    `--build-arg KEY` with no `=value` makes buildx read it from its own
    environment instead, and Ansible never echoes `environment:`. The fix has
    to be this rather than no_log, because no_log would also hide the build
    output an operator needs when a build actually breaks.
    """
    tasks = yaml.safe_load((_GIT_DEPLOY / "tasks" / "build.yml").read_text())

    def _walk(node):
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from _walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from _walk(item)

    build = next(
        t
        for t in _walk(tasks)
        if "buildx build" in str(t.get("ansible.builtin.command", {}).get("cmd", ""))
    )
    cmd = build["ansible.builtin.command"]["cmd"]
    assert "--build-arg {{ key }}\n" in cmd or "--build-arg {{ key }} " in cmd, cmd
    assert "--build-arg {{ key }}={{ value }}" not in cmd, (
        "the build arg value is back on the argument list"
    )
    assert build.get("environment") == "{{ _build.args | default({}) }}", (
        "buildx can only read a valueless --build-arg from its environment"
    )
    assert build.get("no_log") is not True, (
        "no_log here hides the build failure output, which is the one thing "
        "this task exists to show"
    )


def test_pinned_host_keys_are_installed_outside_the_local_build_block():
    """rebuild.sh runs on the build server too, and it needs this file.

    Every git-over-ssh pull in rebuild.sh sets StrictHostKeyChecking=yes with
    UserKnownHostsFile pointed here. The copy used to live inside the
    "Clone and build services" block, which is gated on the host having
    LOCAL-strategy services — so a remote-only build server never got the
    file and every deploy-key pull failed closed, at build time.
    """
    tasks = yaml.safe_load((_GIT_DEPLOY / "tasks" / "main.yml").read_text())

    def find(items, depth=0):
        for task in items or []:
            mod = task.get("ansible.builtin.copy", {})
            if isinstance(mod, dict) and mod.get("src") == "github_known_hosts":
                yield depth, task
            for key in ("block", "rescue", "always"):
                if key in task:
                    yield from find(task[key], depth + 1)

    found = list(find(tasks))
    assert len(found) == 1, "exactly one place installs the pinned host keys"
    depth, task = found[0]
    assert depth == 1, (
        "the copy is nested inside a conditional block again; a build server "
        "with no local-strategy services will not get it"
    )
    assert task["ansible.builtin.copy"]["dest"] == "{{ git_deploy_known_hosts }}"

    # The directory it lands in has to be hoisted with it.
    def names(items, depth=0):
        for t in items or []:
            yield depth, t.get("name")
            for key in ("block", "rescue", "always"):
                if key in t:
                    yield from names(t[key], depth + 1)

    build_dir = [d for d, n in names(tasks) if n == "Ensure build directory exists"]
    assert build_dir == [1], "the build directory must be created at the same level"


# ── `bay up` (deploy_stack only) installs the remote helper too ──────────
#
# remote_build.yml renders `.git-askpass-<service>` on the build server, but
# only under [build, git_deploy]. A remote build that `bay up` added got its
# trigger unit and its rebuild.sh block and no helper, so its first clone in
# rebuild.sh failed with exit 128.


def _walk_tasks(tasks):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk_tasks(task.get(key) or [])


def _tag_list(value):
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _deploy_stack_task_files() -> list[str]:
    """The task files main.yml includes with deploy_stack in tags AND apply.tags."""
    main = yaml.safe_load((_GIT_DEPLOY / "tasks" / "main.yml").read_text())
    out = []
    for task in main:
        inc = task.get("ansible.builtin.include_tasks")
        if not isinstance(inc, dict):
            continue
        apply_tags = _tag_list((inc.get("apply") or {}).get("tags"))
        if "deploy_stack" in _tag_list(task.get("tags")) and "deploy_stack" in apply_tags:
            out.append(inc["file"])
    return out


def _remote_askpass_tasks_under_deploy_stack() -> list[dict]:
    found = []
    for name in _deploy_stack_task_files():
        tasks = yaml.safe_load((_GIT_DEPLOY / "tasks" / name).read_text())
        for task in _walk_tasks(tasks):
            tmpl = task.get("ansible.builtin.template") or {}
            if tmpl.get("src") == "git-askpass.sh.j2" and str(tmpl.get("dest", "")).startswith(
                "{{ git_deploy_remote_build_dir }}/.git-askpass-"
            ):
                found.append(task)
    return found


def test_remote_askpass_is_rendered_under_deploy_stack():
    """Every remote `.git-askpass-` path rebuild.sh uses has a deploy_stack task."""
    rendered = _render_rebuild_sh(
        _remote_service_with_sentinel(), ["animals"], git_deploy_services=["animals"]
    )
    import re

    used = set(re.findall(r"/opt/[^/]+/push-builds/\.git-askpass-[A-Za-z0-9_.-]+", rendered))
    assert {p.rsplit("/", 1)[1] for p in used} == {".git-askpass-animals"}
    (task,) = _remote_askpass_tasks_under_deploy_stack()
    env = make_ansible_env(_GIT_DEPLOY / "templates")
    for path in used:
        prefix, svc = path.rsplit("/.git-askpass-", 1)
        dest = env.from_string(task["ansible.builtin.template"]["dest"]).render(
            git_deploy_remote_build_dir=prefix, item=svc
        )
        assert dest == path
    assert task["loop"] == "{{ _global_remote_build_services | default([]) }}"
    when = [str(w) for w in _tag_list(task.get("when"))]
    assert "_is_build_server | bool" in when
    assert "services[item].build.token is defined" in when


def test_remote_askpass_under_deploy_stack_is_private():
    (task,) = _remote_askpass_tasks_under_deploy_stack()
    assert task["ansible.builtin.template"]["mode"] == "0700"
    assert task["ansible.builtin.template"]["owner"] == "{{ app_user }}"
    assert task.get("no_log") is True
    assert task.get("become_user") == "{{ app_user }}"
    assert task["vars"]["_askpass_token"] == "{{ services[item].build.token }}"


def _run_askpass_tasks(tmp_path: Path, *, build_server: bool) -> Path:
    """Run the deploy_stack helper tasks on localhost, become stripped."""
    import json
    import os

    tasks = yaml.safe_load((_GIT_DEPLOY / "tasks" / "render_rebuild_script.yml").read_text())
    block = next(t for t in tasks if "block" in t)["block"]
    wanted = [
        t for t in block
        if t["name"].startswith(("Ensure the remote build directory exists",
                                 "Install the git credential helper of each remote build"))
    ]
    assert len(wanted) == 2
    for task in wanted:
        task.pop("become", None)
        task.pop("become_user", None)
        task["ansible.builtin.template" if "ansible.builtin.template" in task else "ansible.builtin.file"].pop(
            "owner", None
        )
        # The template module needs a src it can find from the play.
        if "ansible.builtin.template" in task:
            task["ansible.builtin.template"]["src"] = str(_GIT_DEPLOY / "templates" / "git-askpass.sh.j2")
    remote_dir = tmp_path / "push-builds"
    extra = {
        "app_user": "deploy",
        "_is_build_server": build_server,
        "_global_remote_build_services": ["animals", "plain"],
        "git_deploy_remote_build_dir": str(remote_dir),
        "services": {
            "animals": {"build": {"repo": "git@github.com:acme/animals.git",
                                  "token": _SENTINEL_PAT, "strategy": "remote"}},
            "plain": {"build": {"repo": "git@github.com:acme/plain.git", "strategy": "remote"}},
        },
    }
    (tmp_path / "vars.json").write_text(json.dumps(extra))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    (tmp_path / "play.yml").write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": wanted,
    }]))
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _SENTINEL_PAT not in proc.stdout + proc.stderr, "no_log leaked the PAT"
    return remote_dir


def test_remote_askpass_written_only_for_services_with_a_token(tmp_path):
    remote_dir = _run_askpass_tasks(tmp_path, build_server=True)
    helper = remote_dir / ".git-askpass-animals"
    assert helper.is_file()
    assert oct(helper.stat().st_mode & 0o777) == "0o700"
    assert _SENTINEL_PAT in helper.read_text()
    assert not (remote_dir / ".git-askpass-plain").exists()
    assert oct(remote_dir.stat().st_mode & 0o777) == "0o755"


def test_remote_askpass_not_written_off_the_build_server(tmp_path):
    remote_dir = _run_askpass_tasks(tmp_path, build_server=False)
    assert not remote_dir.exists()
