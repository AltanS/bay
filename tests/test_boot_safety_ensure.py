"""bay-infra-ensure.sh never replaces a container the Bay reconciler made.

A container with the label com.bay.config-hash is reconciler-made. After a
cold boot the script once removed Traefik ("running but port 80 not bound") and
made a compose copy. The cause was a missing tailnet address, so the recreate
could not help, and the next rig deploy planned a recreate. These tests run the
rendered script with a fake `docker`, `ss` and `sleep`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from helpers import make_ansible_env

_ROLE = Path(__file__).parent.parent / "roles" / "boot_safety" / "templates"

_FAKE_DOCKER = r"""#!/bin/bash
# Fake docker. State lives in files under $FAKE.
echo "docker $*" >> "$FAKE/calls"
case "$1" in
  compose)
    if [ "$4" = "config" ]; then
      printf '{"services": {"traefik": {"container_name": "traefik"}}}'
      exit 0
    fi
    # compose up -d NAME: the container now exists, unlabelled, running
    echo running > "$FAKE/state-${@: -1}"
    exit 0
    ;;
  inspect)
    if [[ "$*" == *State.Status* ]]; then
      [ -f "$FAKE/state-$2" ] || exit 1
      cat "$FAKE/state-$2"
      exit 0
    fi
    [ -f "$FAKE/label-$2" ] && cat "$FAKE/label-$2"
    exit 0
    ;;
  restart)
    echo running > "$FAKE/state-$2"
    [ -f "$FAKE/restart_binds" ] && touch "$FAKE/port80"
    exit 0
    ;;
  rm)
    rm -f "$FAKE/state-$3"
    exit 0
    ;;
esac
exit 0
"""

_FAKE_SS = """#!/bin/bash
[ -f "$FAKE/port80" ] && echo "LISTEN 0 4096 0.0.0.0:80 0.0.0.0:*"
exit 0
"""


def _setup(tmp_path: Path, *, state: str | None, label: str, port80: bool, restart_binds: bool = False) -> dict:
    fake = tmp_path / "fake"
    bin_dir = tmp_path / "bin"
    fake.mkdir()
    bin_dir.mkdir()
    for name, body in (("docker", _FAKE_DOCKER), ("ss", _FAKE_SS), ("sleep", "#!/bin/bash\nexit 0\n")):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    if state is not None:
        (fake / "state-traefik").write_text(state)
    if label:
        (fake / "label-traefik").write_text(label)
    if port80:
        (fake / "port80").touch()
    if restart_binds:
        (fake / "restart_binds").touch()
    script = make_ansible_env(_ROLE).get_template("bay-infra-ensure.sh.j2").render(
        ansible_managed="test",
        boot_safety_compose_file="/x/docker-compose.infra.yml",
        traefik_container_name="traefik",
    )
    # The script calls compose by absolute path; point it at the fake.
    script = script.replace("/usr/bin/docker", str(bin_dir / "docker"))
    script_path = tmp_path / "ensure.sh"
    script_path.write_text(script)
    return {"script": script_path, "fake": fake, "bin": bin_dir}


def _run(ctx: dict) -> tuple[subprocess.CompletedProcess, list[str]]:
    env = {"PATH": f"{ctx['bin']}:{os.environ['PATH']}", "FAKE": str(ctx["fake"])}
    result = subprocess.run(
        ["bash", str(ctx["script"])], env=env, capture_output=True, text=True, timeout=60
    )
    calls_file = ctx["fake"] / "calls"
    calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return result, calls


def _verbs(calls: list[str]) -> list[str]:
    return [c.split()[1] for c in calls]


def test_labelled_port_not_bound_restarts_and_never_removes(tmp_path: Path) -> None:
    ctx = _setup(tmp_path, state="running", label="abc123", port80=False)
    result, calls = _run(ctx)
    verbs = _verbs(calls)
    assert result.returncode == 0, result.stderr
    assert verbs.count("restart") == 1
    assert "rm" not in verbs
    assert not [c for c in calls if c.startswith("docker compose") and " up " in c]
    assert "WARNING" in result.stdout
    assert "net.ipv4.ip_nonlocal_bind" in result.stdout


def test_labelled_port_bound_after_restart_is_ok(tmp_path: Path) -> None:
    ctx = _setup(tmp_path, state="running", label="abc123", port80=False, restart_binds=True)
    result, calls = _run(ctx)
    assert result.returncode == 0, result.stderr
    assert _verbs(calls).count("restart") == 1
    assert "WARNING" not in result.stdout
    assert "port 80 bound after restart" in result.stdout


def test_labelled_exited_is_restarted_once_not_replaced(tmp_path: Path) -> None:
    ctx = _setup(tmp_path, state="exited", label="abc123", port80=False)
    result, calls = _run(ctx)
    verbs = _verbs(calls)
    assert result.returncode == 0, result.stderr
    assert verbs.count("restart") == 1
    assert "rm" not in verbs
    assert not [c for c in calls if c.startswith("docker compose") and " up " in c]


def test_labelled_healthy_does_nothing(tmp_path: Path) -> None:
    ctx = _setup(tmp_path, state="running", label="abc123", port80=True)
    result, calls = _run(ctx)
    assert result.returncode == 0, result.stderr
    assert set(_verbs(calls)) <= {"compose", "inspect"}
    assert not [c for c in calls if c.startswith("docker compose") and " up " in c]
    assert "port 80 bound" in result.stdout


def test_missing_container_is_created_via_compose(tmp_path: Path) -> None:
    ctx = _setup(tmp_path, state=None, label="", port80=True)
    result, calls = _run(ctx)
    assert result.returncode == 0, result.stderr
    assert [c for c in calls if c.startswith("docker compose") and " up -d traefik" in c]
    assert "bay deploy --rig" in result.stdout


def test_unlabelled_port_not_bound_keeps_the_old_recreate(tmp_path: Path) -> None:
    ctx = _setup(tmp_path, state="running", label="", port80=False)
    result, calls = _run(ctx)
    verbs = _verbs(calls)
    assert result.returncode == 0, result.stderr
    assert "rm" in verbs
    assert [c for c in calls if c.startswith("docker compose") and " up -d traefik" in c]
    assert "restart" not in verbs
