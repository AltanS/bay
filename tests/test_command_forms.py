"""Release and job commands: a string runs through /bin/sh -c, a list keeps the ENTRYPOINT.

An image with an exec-form ``ENTRYPOINT ["node"]`` turned the old ``sh -c "<cmd>"``
Cmd into ``node sh -c ...``. A string command now overrides the entrypoint with
``/bin/sh`` and passes ``["-c", cmd]``. A list command (``release = ["bin/migrate",
"up"]``, the same for a job ``command``) leaves the image ENTRYPOINT alone and is
passed as the Cmd. The same rule holds in the reconciler (``run_release``), in
``rebuild.sh`` (``_run_release``) and in the job script, and the job unit gets its
start timeout from the job's ``timeout`` key.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bay_reconcile.bundle import load_bundle
from bay_reconcile.models import ContainerSpec, release_command_kwargs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "filter_plugins"))
sys.path.insert(0, str(Path(__file__).parent))

from test_jobs_role import JOBS, ROLE, SHOP, _env, _render_script  # noqa: E402
from test_observability_contract import _ansible_env, _release_render_context  # noqa: E402
from test_reconcile_sdk_release import _client  # noqa: E402

#: Arguments that break a script that quotes badly.
HOSTILE = ["bin/migrate", "two words", "it's", "$(id)", "`id`", 'say "hi"', "a\nb", "", "$HOME", "x;y"]


def _argv(path: Path) -> list[str]:
    raw = path.read_bytes().decode()
    return raw.split("\0")[:-1] if raw else []


# ── the reconciler ───────────────────────────────────────────────────────────


def _check_reconciler() -> None:
    string = ContainerSpec(name="web", image="w:1", type="service", config_hash="h", release="bin/migrate up")
    client, daemon = _client({})
    assert client.run_release(string, timeout=30)[0] == 0
    run = daemon.containers.started[0]
    assert run["entrypoint"] == ["/bin/sh"]
    assert run["command"] == ["-c", "bin/migrate up"]

    listed = ContainerSpec(
        name="web", image="w:1", type="service", config_hash="h", release=("bin/migrate", "up", "a b"))
    client, daemon = _client({})
    assert client.run_release(listed, timeout=30)[0] == 0
    run = daemon.containers.started[0]
    assert "entrypoint" not in run  # the image ENTRYPOINT stays
    assert run["command"] == ["bin/migrate", "up", "a b"]

    assert release_command_kwargs("x") == {"entrypoint": ["/bin/sh"], "command": ["-c", "x"]}
    assert release_command_kwargs(["a", "b"]) == {"command": ["a", "b"]}


def _check_bundle() -> None:
    def one(release: Any) -> Any:
        return load_bundle({"containers": [{
            "name": "web", "image": "w", "type": "service", "config_hash": "h", "release": release,
        }]}).containers[0].release

    assert one("bin/migrate") == "bin/migrate"
    assert one(["bin/migrate", "up"]) == ("bin/migrate", "up")
    for bad in ("", " ", [], [""], ["a", 1], 5, {"a": "b"}):
        with pytest.raises(ValueError, match="release must be"):
            one(bad)


# ── rebuild.sh ───────────────────────────────────────────────────────────────


def _run_release_argv(tmp_path: Path, release: Any) -> list[str]:
    """Render rebuild.sh, run its real `_run_release` with stub docker and timeout, return docker's argv."""
    ctx = _release_render_context()
    ctx["services"]["svc-local"]["release"] = release
    rendered = _ansible_env().get_template("rebuild.sh.j2").render(**ctx)
    start = rendered.index("if [[ \"${SERVICE}\" == 'svc-local' ]]; then\n  _run_release")
    block = rendered[start: rendered.index("\nfi\n", start) + 4]
    out = tmp_path / "argv"
    script = tmp_path / "harness.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"STACK_DIR=/opt/bay\nSERVICE=svc-local\nOUT={out}\n"
        'timeout() { shift 2; "$@"; }\n'
        'docker() { printf "%s\\0" "$@" >"$OUT"; }\n'
        f"{block}\n"
        "_run_release the-image:abc\n"
    )
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    done = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return _argv(out)


def _check_rebuild_sh(tmp_path: Path) -> None:
    argv = _run_release_argv(tmp_path, "bin/migrate --yes && echo 'done'")
    image = argv.index("the-image:abc")
    assert argv[image - 2:image] == ["--entrypoint", "/bin/sh"]
    assert argv[image + 1:] == ["-c", "bin/migrate --yes && echo 'done'"]

    argv = _run_release_argv(tmp_path, HOSTILE)
    image = argv.index("the-image:abc")
    assert "--entrypoint" not in argv  # the image ENTRYPOINT stays
    assert argv[image + 1:] == HOSTILE


# ── the job script ───────────────────────────────────────────────────────────


def _job_argv(tmp_path: Path, command: Any) -> list[str]:
    JOBS["shop-job-small"]["command"] = command
    try:
        script = _render_script("shop-job-small", SHOP)
    finally:
        JOBS["shop-job-small"]["command"] = "bin/tick"
    assert subprocess.run(["bash", "-n", "/dev/stdin"], input=script, text=True).returncode == 0
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    out = tmp_path / "argv"
    docker = bindir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == inspect ]]; then echo img:1; exit 0; fi\n'
        f'printf "%s\\0" "$@" >{out}\n'
    )
    docker.chmod(0o755)
    path = tmp_path / "job.sh"
    path.write_text(script)
    done = subprocess.run(
        ["bash", str(path)], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    assert done.returncode == 0, done.stderr
    return _argv(out)


def _check_job_script(tmp_path: Path) -> None:
    argv = _job_argv(tmp_path, "bin/tick --now; echo 'x'")
    image = argv.index("img:1")
    assert argv[image - 2:image] == ["--entrypoint", "/bin/sh"]
    assert argv[image + 1:] == ["-c", "bin/tick --now; echo 'x'"]

    argv = _job_argv(tmp_path, HOSTILE)
    assert "--entrypoint" not in argv
    assert argv[argv.index("img:1") + 1:] == HOSTILE


def test_release_and_job_command_forms(tmp_path: Path) -> None:
    """A string runs through /bin/sh -c with the entrypoint overridden; a list keeps the ENTRYPOINT.

    Covers the reconciler, rebuild.sh (rendered and run with a stub docker) and
    the job script (rendered and run with a stub docker).
    """
    _check_reconciler()
    _check_bundle()
    for name, check in (("rebuild", _check_rebuild_sh), ("job", _check_job_script)):
        work = tmp_path / name
        work.mkdir()
        check(work)


# ── the job unit's start timeout ─────────────────────────────────────────────


def test_job_unit_timeout() -> None:
    env = _env()
    service = env.get_template("bay-job@.service.j2").render(
        ansible_managed="managed", app_user="bay", stack_dir="/opt/bay")
    # The shared unit carries the default; the job's own value is a drop-in.
    assert re.search(r"^TimeoutStartSec=3600$", service, re.M)
    assert "Type=oneshot" in service

    template = env.get_template("job-timeout.conf.j2")
    own = template.render(ansible_managed="managed", job_name="shop-job-nightly",
                          job={**JOBS["shop-job-nightly"], "timeout": 120})
    assert re.search(r"^\[Service\]\nTimeoutStartSec=120$", own, re.M)
    default = template.render(ansible_managed="managed", job_name="shop-job-nightly",
                              job=JOBS["shop-job-nightly"])
    assert re.search(r"^TimeoutStartSec=3600$", default, re.M)
    assert "Persistent" not in default  # a missed run is not caught up

    # The tasks install the drop-in as root and remove it with the job.
    import yaml

    tasks = {t["name"]: t for t in yaml.safe_load((ROLE / "tasks" / "jobs.yml").read_text())}
    install = tasks["Install the job start timeouts"]
    assert install["ansible.builtin.template"]["src"] == "job-timeout.conf.j2"
    assert install["ansible.builtin.template"]["dest"] == (
        "/etc/systemd/system/bay-job@{{ item.key }}.service.d/timeout.conf")
    assert install["become"] is True and install["become_user"] == "root"
    gone = tasks["Remove the start timeouts of jobs that are gone"]
    assert gone["loop"] == "{{ _bay_jobs_gone }}"
    assert gone["ansible.builtin.file"]["state"] == "absent"
    reload_when = tasks["Reload systemd for changed job units"]["when"]
    assert "_bay_job_timeouts is changed" in reload_when
    assert "_bay_job_timeouts_removed is changed" in reload_when
