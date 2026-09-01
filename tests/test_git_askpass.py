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


def _render_helper(tmp_path: Path) -> Path:
    env = make_ansible_env(_GIT_DEPLOY / "templates")
    out = env.get_template("git-askpass.sh.j2").render(
        ansible_managed="test render",
        app_user="deploy",
        _askpass_token=_SENTINEL_PAT,
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
    tasks = yaml.safe_load((_GIT_DEPLOY / "tasks" / "webhook.yml").read_text())
    task = next(
        t
        for t in tasks
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
