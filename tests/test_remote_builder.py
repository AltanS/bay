"""Optional remote BuildKit builder over the tailnet, with automatic fallback.

git_deploy can register a second buildx builder (`remote` driver, mTLS) on the
build server. Before each build, `select-builder.sh` probes it and prints the
builder to use. rebuild.sh and remote_build.yml both call that one script.
rebuild.sh also retries ONCE on the local builder when the remote drops out
mid-build.

These tests pin:
  * the feature is fully off by default: no cert tasks, no remote builder,
    and the helper prints the local builder without touching docker;
  * an endpoint without mTLS material fails the role's assert;
  * the helper's contract: one line on stdout, one on stderr, exit 0 always,
    bounded by the probe timeout (run against a fake `docker`);
  * rebuild.sh's retry policy, run for real against stubs: fallback only when
    the re-probe says the remote is gone, one retry, one alert, and nothing
    from the fallback reaches the circuit breaker.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import jinja2
import pytest
import yaml
from helpers import make_ansible_env
from test_observability_contract import _ansible_env, _minimal_render_context

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLE = _REPO_ROOT / "roles" / "git_deploy"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_MAIN = _ROLE / "tasks" / "main.yml"
_SETUP_BUILDER = _ROLE / "tasks" / "setup_builder.yml"
_REMOTE_BUILDER = _ROLE / "tasks" / "remote_builder.yml"
_REMOTE_BUILD = _ROLE / "tasks" / "remote_build.yml"
_SYSTEMD = _ROLE / "tasks" / "systemd.yml"
_HELPER = _ROLE / "templates" / "select-builder.sh.j2"
_REBUILD = _ROLE / "templates" / "rebuild.sh.j2"
_REGISTRY = _REPO_ROOT / "alerts" / "registry.yml"

_LOCAL = "bay-local-test"
_REMOTE = "bay-remote"
_ENDPOINT = "tcp://100.64.0.8:1234"


def _defaults() -> dict:
    return yaml.safe_load(_DEFAULTS.read_text())


def _tasks(path: Path) -> list[dict]:
    """Every task in a task file, blocks flattened."""
    out: list[dict] = []

    def walk(items: list[dict]) -> None:
        for item in items or []:
            out.append(item)
            for key in ("block", "rescue", "always"):
                walk(item.get(key, []))

    walk(yaml.safe_load(path.read_text()))
    return out


def _expr_env() -> jinja2.Environment:
    env = jinja2.Environment()
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    return env


def _holds(conditions: str | list[str], **ctx) -> bool:
    """Evaluate an Ansible `when:`/`that:` the way Ansible does: all must hold."""
    env = _expr_env()
    conds = [conditions] if isinstance(conditions, str) else conditions
    return all(bool(env.compile_expression(c.strip())(**ctx)) for c in conds)


def _role_ctx(**overrides) -> dict:
    ctx = {**_defaults(), "bay_buildx_builder": _LOCAL}
    ctx.update(overrides)
    return ctx


# ── Defaults ─────────────────────────────────────────────────────────────


def test_defaults_ship_the_feature_off():
    d = _defaults()
    assert d["git_deploy_remote_builder_endpoint"] == ""
    assert d["git_deploy_remote_builder_name"] == "bay-remote"
    assert d["git_deploy_remote_builder_probe_timeout"] == 5
    for key in ("ca", "cert", "key"):
        assert d[f"git_deploy_remote_builder_{key}"] == "", key


def test_defaults_document_the_vault_convention():
    text = _DEFAULTS.read_text()
    idx = text.index("\ngit_deploy_remote_builder_ca:")
    comment = text[max(0, idx - 700) : idx]
    assert "LOWERCASE" in comment
    assert ".buildkit" in comment


# ── Feature off: nothing remote is provisioned ───────────────────────────


def _remote_builder_includes() -> list[dict]:
    found = []
    for path in sorted((_ROLE / "tasks").glob("*.yml")):
        for task in _tasks(path):
            if task.get("ansible.builtin.include_tasks") == "remote_builder.yml":
                found.append(task)
    return found


def test_remote_builder_tasks_are_only_reached_through_gated_includes():
    includes = _remote_builder_includes()
    assert len(includes) == 2, "expected one include in main.yml and one in setup_builder.yml"
    for path in (_MAIN, _SETUP_BUILDER):
        assert "include_tasks: remote_builder.yml" in path.read_text(), path.name


@pytest.mark.parametrize("is_build_server", [True, False])
def test_feature_off_skips_cert_and_builder_tasks(is_build_server: bool):
    """With defaults, neither include fires, on any host."""
    for task in _remote_builder_includes():
        ctx = _role_ctx(_is_build_server=is_build_server)
        assert not _holds(task["when"], **ctx), task["name"]


def test_endpoint_set_provisions_on_the_build_server_only():
    for task in _remote_builder_includes():
        on = _role_ctx(_is_build_server=True, git_deploy_remote_builder_endpoint=_ENDPOINT)
        off = _role_ctx(_is_build_server=False, git_deploy_remote_builder_endpoint=_ENDPOINT)
        done = {**on, "_bay_remote_builder_done": True}
        assert _holds(task["when"], **on), task["name"]
        assert not _holds(task["when"], **off), task["name"]
        assert not _holds(task["when"], **done), f"{task['name']} must run at most once per play"


def test_only_remote_builder_yml_creates_a_remote_driver_builder():
    for path in (_ROLE / "tasks").glob("*.yml"):
        if path == _REMOTE_BUILDER:
            continue
        assert "--driver remote" not in path.read_text(), path.name


# ── Validation ───────────────────────────────────────────────────────────


def _assert_task() -> dict:
    tasks = [t for t in _tasks(_MAIN) if "ansible.builtin.assert" in t]
    matches = [t for t in tasks if "remote" in t["name"].lower()]
    assert len(matches) == 1, [t["name"] for t in tasks]
    return matches[0]


def test_assert_is_skipped_when_the_feature_is_off():
    task = _assert_task()
    assert not _holds(task["when"], **_role_ctx())


def test_endpoint_without_certs_fails_the_assert():
    task = _assert_task()
    ctx = _role_ctx(git_deploy_remote_builder_endpoint=_ENDPOINT)
    assert _holds(task["when"], **ctx)
    assert not _holds(task["ansible.builtin.assert"]["that"], **ctx)
    msg = task["ansible.builtin.assert"]["fail_msg"]
    for var in ("git_deploy_remote_builder_ca", "git_deploy_remote_builder_cert", "git_deploy_remote_builder_key"):
        assert var in msg


@pytest.mark.parametrize("missing", ["ca", "cert", "key"])
def test_each_missing_pem_fails_the_assert(missing: str):
    pems = {f"git_deploy_remote_builder_{k}": "-----BEGIN X-----" for k in ("ca", "cert", "key")}
    pems[f"git_deploy_remote_builder_{missing}"] = ""
    ctx = _role_ctx(git_deploy_remote_builder_endpoint=_ENDPOINT, **pems)
    assert not _holds(_assert_task()["ansible.builtin.assert"]["that"], **ctx)


def test_complete_settings_pass_the_assert_and_names_must_differ():
    pems = {f"git_deploy_remote_builder_{k}": "-----BEGIN X-----" for k in ("ca", "cert", "key")}
    that = _assert_task()["ansible.builtin.assert"]["that"]
    ctx = _role_ctx(git_deploy_remote_builder_endpoint=_ENDPOINT, **pems)
    assert _holds(that, **ctx)
    # Same name as the local builder: an endpoint change would `buildx rm` it.
    assert not _holds(that, **{**ctx, "git_deploy_remote_builder_name": _LOCAL})


# ── remote_builder.yml shape ─────────────────────────────────────────────


def test_cert_files_are_private_and_not_logged():
    task = next(t for t in _tasks(_REMOTE_BUILDER) if "ansible.builtin.copy" in t)
    assert task["no_log"] is True
    assert task["become_user"] == "{{ app_user }}"
    assert task["ansible.builtin.copy"]["mode"] == "0600"
    assert task["ansible.builtin.copy"]["dest"] == "{{ stack_dir }}/.buildkit/{{ item.name }}.pem"
    assert sorted(i["name"] for i in task["loop"]) == ["ca", "cert", "key"]


def test_builder_create_is_remote_mtls_without_bootstrap_or_entitlements():
    create = next(t for t in _tasks(_REMOTE_BUILDER) if "buildx create" in str(t.get("ansible.builtin.command", "")))
    cmd = " ".join(create["ansible.builtin.command"]["cmd"].split())
    assert "--driver remote {{ git_deploy_remote_builder_endpoint }}" in cmd
    for opt in ("cacert={{ stack_dir }}/.buildkit/ca.pem", "cert={{ stack_dir }}/.buildkit/cert.pem",
                "key={{ stack_dir }}/.buildkit/key.pem"):
        assert opt in cmd
    assert "--bootstrap" not in cmd, "the remote may be offline at deploy time"
    assert "entitlement" not in cmd
    assert create["become_user"] == "{{ app_user }}"


def test_builder_is_recreated_only_when_the_endpoint_changes():
    tasks = _tasks(_REMOTE_BUILDER)
    rm = next(t for t in tasks if "buildx rm" in str(t.get("ansible.builtin.command", "")))
    create = next(t for t in tasks if "buildx create" in str(t.get("ansible.builtin.command", "")))
    base = {"git_deploy_remote_builder_endpoint": _ENDPOINT}

    def state(rc: int, registered: str) -> dict:
        return {**base, "_remote_builder_inspect": {"rc": rc}, "_remote_builder_registered_endpoint": registered}

    same, moved = state(0, _ENDPOINT), state(0, "tcp://100.64.0.9:1234")
    absent, timed_out = state(1, ""), state(124, "")
    assert not _holds(rm["when"], **same) and not _holds(create["when"], **same)
    assert _holds(rm["when"], **moved) and _holds(create["when"], **moved)
    assert not _holds(rm["when"], **absent) and _holds(create["when"], **absent)
    assert not _holds(rm["when"], **timed_out) and not _holds(create["when"], **timed_out)


# ── select-builder.sh ────────────────────────────────────────────────────


def _render_helper(endpoint: str = "", timeout: int = 5) -> str:
    return make_ansible_env(_HELPER.parent).get_template(_HELPER.name).render(
        ansible_managed="Ansible managed - test",
        bay_buildx_builder=_LOCAL,
        git_deploy_remote_builder_name=_REMOTE,
        git_deploy_remote_builder_probe_timeout=timeout,
        _select_builder_endpoint=endpoint,
    )


def _run_helper(tmp_path: Path, *, endpoint: str, docker_body: str | None, timeout: int = 5):
    """Run the rendered helper with a fake `docker` first on PATH."""
    script = tmp_path / "select-builder.sh"
    script.write_text(_render_helper(endpoint, timeout))
    script.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "docker.calls"
    if docker_body is not None:
        fake = bindir / "docker"
        fake.write_text(f'#!/usr/bin/env bash\necho "$*" >> {calls}\n{docker_body}\n')
        fake.chmod(0o755)
    start = time.monotonic()
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True, text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
    )
    elapsed = time.monotonic() - start
    return result, (calls.read_text() if calls.exists() else ""), elapsed


def test_helper_prints_local_when_the_feature_is_off(tmp_path):
    result, calls, _ = _run_helper(tmp_path, endpoint="", docker_body="exit 0")
    assert result.returncode == 0
    assert result.stdout == f"{_LOCAL}\n"
    assert len(result.stderr.splitlines()) == 1
    assert calls == "", "feature off must not touch docker at all"


def test_helper_prints_remote_when_the_probe_answers(tmp_path):
    result, calls, _ = _run_helper(tmp_path, endpoint=_ENDPOINT, docker_body="exit 0")
    assert result.returncode == 0
    assert result.stdout == f"{_REMOTE}\n"
    assert len(result.stderr.splitlines()) == 1
    assert calls.strip() == f"buildx inspect --bootstrap {_REMOTE}"


def test_helper_falls_back_when_the_probe_fails(tmp_path):
    body = 'echo "#1 [internal] waiting for connection" >&2\necho "ERROR: no builder \\"bay-remote\\" found" >&2\nexit 1'
    result, _, _ = _run_helper(tmp_path, endpoint=_ENDPOINT, docker_body=body)
    assert result.returncode == 0
    assert result.stdout == f"{_LOCAL}\n"
    lines = result.stderr.splitlines()
    assert len(lines) == 1, result.stderr
    assert "rc=1" in lines[0] and "no builder" in lines[0]


def test_helper_probe_is_bounded_by_the_timeout(tmp_path):
    result, _, elapsed = _run_helper(tmp_path, endpoint=_ENDPOINT, docker_body="sleep 30", timeout=1)
    assert result.returncode == 0
    assert result.stdout == f"{_LOCAL}\n"
    assert "no answer within 1s" in result.stderr
    assert elapsed < 10, f"probe took {elapsed:.1f}s with a 1s timeout"


def test_helper_without_docker_still_exits_zero(tmp_path):
    result, _, _ = _run_helper(tmp_path, endpoint=_ENDPOINT, docker_body=None)
    assert result.returncode == 0
    assert result.stdout == f"{_LOCAL}\n"


@pytest.mark.parametrize("endpoint", ["", _ENDPOINT])
def test_helper_renders_valid_bash(tmp_path, endpoint: str):
    script = tmp_path / "select-builder.sh"
    script.write_text(_render_helper(endpoint))
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize("endpoint", ["", _ENDPOINT])
def test_helper_is_shellcheck_clean(tmp_path, endpoint: str):
    script = tmp_path / "select-builder.sh"
    script.write_text(_render_helper(endpoint))
    result = subprocess.run(["shellcheck", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_systemd_render_blanks_the_endpoint_off_the_build_server():
    """Only the build server registers the remote builder; elsewhere the
    helper must not probe a builder that does not exist."""
    task = next(t for t in _tasks(_SYSTEMD) if t.get("ansible.builtin.template", {}).get("src") == "select-builder.sh.j2")
    expr = task["vars"]["_select_builder_endpoint"].strip()
    env = _expr_env()
    render = lambda **c: env.from_string(expr).render(**c)  # noqa: E731
    assert render(_is_build_server=True, git_deploy_remote_builder_endpoint=_ENDPOINT) == _ENDPOINT
    assert render(_is_build_server=False, git_deploy_remote_builder_endpoint=_ENDPOINT) == ""
    assert render(_is_build_server=True, git_deploy_remote_builder_endpoint="") == ""


# ── remote_build.yml (deploy path) ───────────────────────────────────────


def test_deploy_path_selects_the_builder_before_building():
    tasks = _tasks(_REMOTE_BUILD)
    names = [t.get("name", "") for t in tasks]
    select = next(i for i, n in enumerate(names) if n.endswith("Select buildx builder"))
    build = next(i for i, n in enumerate(names) if n.endswith("Build and push Docker image on build server"))
    assert select < build
    task = tasks[select]
    assert task["ansible.builtin.command"]["cmd"] == "{{ stack_dir }}/bin/select-builder.sh"
    assert task["register"] == "_selected_builder"
    # Delegation and become come from the enclosing block.
    block = next(t for t in tasks if any(c is task for c in t.get("block", [])))
    assert block["delegate_to"] == "{{ build_server }}"
    assert block["become_user"] == "{{ app_user }}"
    assert "_selected_builder.stdout" in tasks[build]["ansible.builtin.command"]["cmd"]


def test_deploy_path_has_no_retry_logic():
    text = _REMOTE_BUILD.read_text()
    assert "remote_fallback" not in text
    assert "rescue:" not in text


# ── rebuild.sh retry semantics ───────────────────────────────────────────


def _rendered_rebuild() -> str:
    ctx = _minimal_render_context()
    ctx["bay_buildx_builder"] = _LOCAL
    ctx["git_deploy_remote_builder_name"] = _REMOTE
    return _ansible_env().get_template(_REBUILD.name).render(**ctx)


def _function(rendered: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", rendered, flags=re.S | re.M)
    assert match, f"{name} not found in rendered rebuild.sh"
    return match.group(0)


def test_rebuild_renders_the_builder_names():
    rendered = _rendered_rebuild()
    assert f"LOCAL_BUILDER={_LOCAL}" in rendered
    assert f"REMOTE_BUILDER={_REMOTE}" in rendered
    assert 'SELECT_BUILDER="${STACK_DIR}/bin/select-builder.sh"' in rendered


def test_rebuild_contains_the_fallback_branch_with_a_literal_alert_id():
    body = _function(_rendered_rebuild(), "_build_with_fallback")
    assert '_log "builder=${builder}"' in body
    assert "reprobe=$(_select_builder)" in body
    assert re.search(r"^\s*notify_build build\.remote_fallback ", body, flags=re.M)
    assert "remote builder lost mid-build, retrying on ${LOCAL_BUILDER}" in body
    # Only the caller records failures; the fallback must never count one.
    assert "_record_failure" not in body


def test_both_strategies_build_through_the_fallback():
    rendered = _rendered_rebuild()
    assert "if ! _build_with_fallback _remote_buildx; then" in rendered
    assert "if ! _build_with_fallback _local_buildx; then" in rendered
    assert "--builder {{" not in _REBUILD.read_text(), "a hardcoded builder bypasses the selection"


def _harness(tmp_path: Path, *, picks: list[str], results: dict[str, int], helper: bool = True):
    """Run _select_builder + _build_with_fallback against stubs.

    picks    successive builder names the helper prints, one per call
    results  exit code of the build per builder name
    """
    rendered = _rendered_rebuild()
    helper_path = tmp_path / "select-builder.sh"
    if helper:
        (tmp_path / "picks").write_text("\n".join(picks) + "\n")
        helper_path.write_text(
            "#!/usr/bin/env bash\n"
            f"n=$(cat {tmp_path}/n 2>/dev/null || echo 0); n=$((n + 1)); echo $n > {tmp_path}/n\n"
            f'sed -n "${{n}}p" {tmp_path}/picks\n'
            'echo "probe reason" >&2\n'
        )
        helper_path.chmod(0o755)
    cases = "\n".join(f'    {name}) return {rc} ;;' for name, rc in results.items())
    script = f"""set -euo pipefail
SERVICE=svc-remote
SELECT_BUILDER={helper_path}
LOCAL_BUILDER={_LOCAL}
REMOTE_BUILDER={_REMOTE}
_log() {{ echo "[rebuild] $*"; }}
notify_build() {{ echo "NOTIFY $1 $2"; }}
_fake_build() {{
  echo "BUILD $1"
  case "$1" in
{cases}
  esac
}}
{_function(rendered, "_select_builder")}
{_function(rendered, "_build_with_fallback")}
if _build_with_fallback _fake_build; then echo "FINAL 0"; else echo "FINAL 1"; fi
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    probes = int((tmp_path / "n").read_text()) if (tmp_path / "n").exists() else 0
    return result.stdout, probes


def test_remote_lost_mid_build_retries_once_on_local(tmp_path):
    out, probes = _harness(tmp_path, picks=[_REMOTE, _LOCAL], results={_REMOTE: 1, _LOCAL: 0})
    assert probes == 2
    assert [ln for ln in out.splitlines() if ln.startswith("BUILD")] == [f"BUILD {_REMOTE}", f"BUILD {_LOCAL}"]
    notes = [ln for ln in out.splitlines() if ln.startswith("NOTIFY")]
    assert notes == [f"NOTIFY build.remote_fallback svc-remote: remote builder lost mid-build, retrying on {_LOCAL}"]
    assert f"[rebuild] builder={_REMOTE}" in out and f"[rebuild] builder={_LOCAL}" in out
    assert out.rstrip().endswith("FINAL 0"), "a fallback that succeeds is a success"


def test_fallback_retry_is_single_and_its_failure_is_final(tmp_path):
    out, _ = _harness(tmp_path, picks=[_REMOTE, _LOCAL], results={_REMOTE: 1, _LOCAL: 1})
    assert out.count("BUILD ") == 2
    assert out.rstrip().endswith("FINAL 1")


def test_remote_still_answering_means_a_real_failure(tmp_path):
    out, probes = _harness(tmp_path, picks=[_REMOTE, _REMOTE], results={_REMOTE: 1, _LOCAL: 0})
    assert probes == 2
    assert out.count("BUILD ") == 1
    assert "NOTIFY" not in out
    assert out.rstrip().endswith("FINAL 1")


def test_local_failure_is_never_retried_or_reprobed(tmp_path):
    out, probes = _harness(tmp_path, picks=[_LOCAL], results={_LOCAL: 1})
    assert probes == 1
    assert out.count("BUILD ") == 1
    assert "NOTIFY" not in out
    assert out.rstrip().endswith("FINAL 1")


def test_probe_choosing_local_up_front_is_silent(tmp_path):
    out, _ = _harness(tmp_path, picks=[_LOCAL], results={_LOCAL: 0})
    assert "NOTIFY" not in out
    assert [ln for ln in out.splitlines() if "builder=" in ln] == [f"[rebuild] builder={_LOCAL}"]
    assert out.rstrip().endswith("FINAL 0")


def test_missing_helper_builds_on_local(tmp_path):
    out, _ = _harness(tmp_path, picks=[], results={_LOCAL: 0}, helper=False)
    assert f"BUILD {_LOCAL}" in out
    assert out.rstrip().endswith("FINAL 0")


# ── Alert registry ───────────────────────────────────────────────────────


def test_fallback_alert_sits_at_the_bottom_of_the_ladder():
    entry = yaml.safe_load(_REGISTRY.read_text())["build.remote_fallback"]
    assert entry["level"] == "debug"
    assert entry["source"] == "git_deploy/rebuild.sh.j2"
