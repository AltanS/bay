"""The Traefik logrotate stanza must not run `docker kill`.

`docker kill`, with any signal, sets HasBeenManuallyStopped on the container.
The flag survives a daemon restart, so an `unless-stopped` Traefik that
rotation had signalled stayed down after `systemctl restart docker` or a
reboot. The stanza now sends USR1 to the container's main process from the
host, and only when `docker inspect` gives a real pid.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402

_TEMPLATES = _TESTS_DIR.parent / "roles" / "docker_monitor" / "templates"


def _render(**ctx) -> str:
    env = make_ansible_env(_TEMPLATES)
    base = dict(
        ansible_managed="test",
        traefik_access_log_path="/opt/stack/logs/traefik/access.log",
        traefik_container_name="traefik",
    )
    base.update(ctx)
    return env.get_template("traefik-logrotate.j2").render(**base)


def _script_lines(rendered: str) -> list[str]:
    """Non-comment lines between `postrotate` and `endscript`."""
    body = rendered.split("postrotate", 1)[1].split("endscript", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_postrotate_does_not_use_docker_kill():
    # Comments may name `docker kill` to explain the choice; code must not run it.
    assert not any("docker kill" in ln for ln in _script_lines(_render()))


def test_postrotate_guards_the_pid_before_kill():
    lines = "\n".join(_script_lines(_render()))
    assert "docker inspect" in lines
    assert "{{.State.Pid}}" in lines, "Go template braces must survive Jinja"
    assert 'if [ -n "$pid" ] && [ "$pid" != "0" ]; then kill -USR1 "$pid"' in lines
    # No unguarded kill: every `kill` sits behind the pid test.
    kills = [ln for ln in _script_lines(_render()) if re.search(r"\bkill\b", ln)]
    assert kills and all('"$pid" != "0"' in ln for ln in kills)


def test_container_name_and_log_path_render():
    out = _render(traefik_container_name="edge")
    assert "edge 2>/dev/null" in out
    assert out.startswith("# test\n/opt/stack/logs/traefik/access.log {")
    assert "{{" not in out.replace("{{.State.Pid}}", "") and "{%" not in out
