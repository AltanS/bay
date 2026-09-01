"""The debug agent must not be root-equivalent.

Until this landed the role was documented as "read-only" while it put the
account in the `docker` group (which is root) and granted bare `sudo cat`,
`sudo grep` and `sudo journalctl` — so `sudo cat /etc/shadow` worked, and
the pager that `journalctl` and `systemctl status` open accepts `!sh`.

These tests pin the replacement: argument-validating wrapper scripts, an
opt-in flag for the docker group, and a sudoers file that grants no bare
file-reading binary.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from helpers import make_ansible_env

ROOT = Path(__file__).resolve().parent.parent
ROLE = ROOT / "roles" / "debug_agent"


def _defaults() -> dict:
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())


def _render(name: str, **overrides) -> str:
    env = make_ansible_env(ROLE / "templates")
    ctx = {
        "debug_agent_user": "debugbot",
        "debug_agent_readable_paths": ["/var/log", "/opt/bay/logs"],
        "debug_agent_sudoers_commands": _defaults()["debug_agent_sudoers_commands"],
    }
    ctx.update(overrides)
    return env.get_template(f"{name}.j2").render(**ctx)


def _script(tmp_path: Path, name: str, **overrides) -> Path:
    path = tmp_path / name
    path.write_text(_render(name, **overrides))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


# ── defaults ─────────────────────────────────────────────────────────────


def test_docker_group_is_not_a_default():
    """Membership of `docker` is root: a member can bind-mount / anywhere."""
    assert "docker" not in _defaults()["debug_agent_groups"]


def test_docker_access_is_opt_in_and_off():
    d = _defaults()
    assert d["debug_agent_docker_access"] is False


def test_defaults_document_that_docker_is_root():
    text = (ROLE / "defaults" / "main.yml").read_text()
    assert "root" in text.split("debug_agent_docker_access")[0].lower()


@pytest.mark.parametrize("binary", ["/usr/bin/cat", "/usr/bin/tail", "/usr/bin/head",
                                    "/usr/bin/grep", "/usr/bin/journalctl"])
def test_no_bare_file_reader_in_sudoers(binary):
    """sudoers cannot constrain an argument, so a bare reader reads /etc/shadow."""
    commands = _defaults()["debug_agent_sudoers_commands"]
    assert not any(c == binary or c.startswith(binary + " ") for c in commands)


def test_sudoers_grants_the_wrappers():
    commands = " ".join(_defaults()["debug_agent_sudoers_commands"])
    for wrapper in ("bay-readlog", "bay-journal", "bay-systemctl-ro", "bay-docker-ro"):
        assert f"/usr/local/bin/{wrapper}" in commands


def test_sudoers_file_resets_the_environment():
    rendered = _render("sudoers")
    assert "Defaults:debugbot !use_pty" in rendered
    assert "Defaults:debugbot env_reset" in rendered


def test_readable_paths_is_a_list_covering_the_stack_logs():
    paths = _defaults()["debug_agent_readable_paths"]
    assert isinstance(paths, list)
    assert "/var/log" in paths
    assert any("stack_dir" in p and "logs" in p for p in paths)


# ── bay-readlog ──────────────────────────────────────────────────────────


@pytest.fixture()
def readlog(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "app.log").write_text("hello\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("topsecret\n")
    script = _script(tmp_path, "bay-readlog", debug_agent_readable_paths=[str(logs)])
    return script, logs, secret


def test_readlog_serves_an_allowlisted_file(readlog):
    script, logs, _ = readlog
    result = _run(script, "cat", str(logs / "app.log"))
    assert result.returncode == 0
    assert result.stdout == "hello\n"


def test_readlog_refuses_a_path_outside_the_allowlist(readlog):
    script, _, secret = readlog
    result = _run(script, "cat", str(secret))
    assert result.returncode != 0
    assert "topsecret" not in result.stdout


def test_readlog_refuses_etc_shadow(readlog):
    script, _, _ = readlog
    assert _run(script, "cat", "/etc/shadow").returncode != 0


def test_readlog_refuses_a_symlink_that_escapes(readlog):
    """realpath -e resolves the link, so the target is what gets checked."""
    script, logs, secret = readlog
    (logs / "escape.log").symlink_to(secret)
    result = _run(script, "cat", str(logs / "escape.log"))
    assert result.returncode != 0
    assert "topsecret" not in result.stdout


@pytest.mark.parametrize("name", [".env", "app-secrets.log", "vault.log", "id_ed25519"])
def test_readlog_refuses_sensitive_names_even_inside_the_allowlist(readlog, name):
    script, logs, _ = readlog
    (logs / name).write_text("nope\n")
    assert _run(script, "cat", str(logs / name)).returncode != 0


def test_readlog_refuses_an_unknown_operation(readlog):
    script, logs, _ = readlog
    assert _run(script, "vi", str(logs / "app.log")).returncode != 0


def test_readlog_refuses_an_unlisted_flag(readlog):
    """grep -f reads the pattern from a file the allowlist never saw."""
    script, logs, _ = readlog
    assert _run(script, "grep", "-f", "/etc/shadow", str(logs / "app.log")).returncode != 0


def test_readlog_allows_a_tail_count(readlog):
    script, logs, _ = readlog
    result = _run(script, "tail", "-n", "1", str(logs / "app.log"))
    assert result.returncode == 0
    assert result.stdout == "hello\n"


# ── bay-docker-ro ────────────────────────────────────────────────────────


@pytest.fixture()
def docker_ro(tmp_path):
    return _script(tmp_path, "bay-docker-ro")


@pytest.mark.parametrize("args", [
    ["exec", "-it", "web", "sh"],
    ["run", "-v", "/:/host", "alpine", "sh"],
    ["cp", "web:/etc/passwd", "/tmp/p"],
    ["ps", "-v", "/:/host"],
    ["logs", "--volume", "/:/host"],
    ["system", "prune"],
    ["compose", "up"],
    ["compose", "exec", "web", "sh"],
    # `docker inspect` prints Config.Env — every bearer token in the container.
    # A read-only surface that hands those out is not read-only.
    ["inspect", "web"],
    ["inspect", "--format", "{{.Config.Env}}", "web"],
    ["inspect", "--format={{json .Config}}", "web"],
    ["inspect", "--format", "{{range .Config.Env}}{{.}}{{end}}", "web"],
])
def test_docker_ro_refuses_write_paths(docker_ro, args):
    assert _run(docker_ro, *args).returncode != 0


def test_docker_ro_inspect_format_allowlist_is_declared():
    """The allowlist is the guard; an empty one would silently permit nothing.

    Pinned by name so the check in bay-docker-ro.j2 cannot be renamed away.
    """
    src = (Path(__file__).resolve().parent.parent / "roles" / "debug_agent"
           / "templates" / "bay-docker-ro.j2").read_text()
    assert "BAY_INSPECT_FORMAT_ALLOW" in src
    assert ".Config.Env" not in src.split("BAY_INSPECT_FORMAT_ALLOW")[1].split(")")[0]


@pytest.mark.parametrize("args", [
    ["ps", "-a"],
    ["logs", "--tail", "50", "web"],
    ["inspect", "--format", "{{.State.Status}}", "web"],
    ["inspect", "--format={{json .State}}", "web"],
    ["stats", "--no-stream"],
    ["images"],
    ["system", "df"],
    ["compose", "ps"],
    ["compose", "logs", "--tail", "20"],
])
def test_docker_ro_accepts_the_read_only_surface(docker_ro, args, tmp_path):
    """Validation must pass; a fake `docker` on PATH stands in for the daemon."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    body = _render("bay-docker-ro").replace("DOCKER=/usr/bin/docker",
                                            f"DOCKER={fake_bin / 'docker'}")
    (fake_bin / "docker").write_text("#!/bin/bash\necho \"$@\"\n")
    (fake_bin / "docker").chmod(0o755)
    script = tmp_path / "docker-ro-fake"
    script.write_text(body)
    result = _run(script, *args)
    assert result.returncode == 0, result.stderr


def test_docker_ro_forces_no_stream_on_stats(docker_ro):
    assert "stats --no-stream" in docker_ro.read_text()


# ── bay-journal / bay-systemctl-ro ───────────────────────────────────────


def test_journal_forces_no_pager(tmp_path):
    body = _render("bay-journal")
    assert "journalctl --no-pager" in body


@pytest.mark.parametrize("args", [
    ["--vacuum-size=1M"],
    ["--rotate"],
    ["--flush"],
    ["--root=/"],
    ["--file=/etc/shadow"],
])
def test_journal_refuses_mutating_or_redirecting_flags(tmp_path, args):
    script = _script(tmp_path, "bay-journal")
    assert _run(script, *args).returncode != 0


def test_systemctl_ro_forces_no_pager(tmp_path):
    assert "systemctl --no-pager" in _render("bay-systemctl-ro")


@pytest.mark.parametrize("verb", ["start", "stop", "restart", "enable", "mask",
                                  "daemon-reload", "edit", "kill"])
def test_systemctl_ro_refuses_mutating_verbs(tmp_path, verb):
    script = _script(tmp_path, "bay-systemctl-ro")
    assert _run(script, verb, "sshd").returncode != 0


# ── tasks + docs ─────────────────────────────────────────────────────────


def test_tasks_install_every_wrapper():
    tasks = (ROLE / "tasks" / "main.yml").read_text()
    for wrapper in ("bay-readlog", "bay-journal", "bay-systemctl-ro", "bay-docker-ro"):
        assert wrapper in tasks


def test_tasks_add_the_docker_group_only_when_opted_in():
    tasks = (ROLE / "tasks" / "main.yml").read_text()
    assert "debug_agent_docker_access" in tasks


def test_docs_do_not_claim_a_read_only_account():
    doc = (ROOT / "docs" / "debug-agent.md").read_text()
    assert "cannot connect as any other user" not in doc
    assert "debug_agent_docker_access" in doc


def test_validate_ssh_hook_suite_passes():
    result = subprocess.run(
        ["bash", str(ROOT / "tests" / "test_validate_ssh.sh")],
        capture_output=True, text=True, check=False,
        env={**os.environ, "TERM": "dumb"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_validate_ssh_hook_needs_jq():
    assert shutil.which("jq"), "the hook and its tests shell out to jq"
