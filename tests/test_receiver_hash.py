"""The webhook receiver carries a content hash, so `bay plan --remote` sees a receiver change.

Gap 31 (M118 gates): the receiver image `bay-webhook:latest` is rebuilt in
the run (git_deploy render_webhook.yml). Check mode skips the build, so the
reconciler saw the same image ID and predicted `noop`, and the real up then
recreated `bay-webhook` on every box.

Since 2.4.0 the hash of the receiver's build context (`bay_tree_hash` over
roles/git_deploy/files/webhook, computed on the control node) labels the image
and the container (`com.bay.receiver-hash`). The label is part of the
container spec, so a changed receiver changes the spec's config hash, also in
check mode.

`test_plan_predicts_receiver_recreate` runs the real check path: the
container_lifecycle role in `ansible-playbook --check` (as git_deploy's
receiver pass includes it), the plan-only reconciler that the role ships and
runs, with the docker SDK against a fake Docker Engine API on a unix socket,
and the CLI's reading of the per-box report into plan steps.
"""

from __future__ import annotations

import getpass
import grp
import http.server
import importlib.util
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
RECEIVER = ROOT / "roles" / "git_deploy" / "files" / "webhook"
LABEL = "com.bay.receiver-hash"
EXPR = "'roles/git_deploy/files/webhook' | bay_tree_hash"

# Loaded under its own name: a role keeps another `bay_filters` module.
_spec = importlib.util.spec_from_file_location(
    "bay_filters_receiver_hash", ROOT / "filter_plugins" / "bay_filters.py"
)
assert _spec is not None and _spec.loader is not None
_filters = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_filters)
bay_tree_hash = _filters.bay_tree_hash


def _receiver_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "webhook"
    shutil.copytree(RECEIVER, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


# ── The hash ────────────────────────────────────────────────────────────


def test_receiver_hash_label_same_files_same_hash(tmp_path: Path) -> None:
    a = _receiver_copy(tmp_path / "a")
    b = _receiver_copy(tmp_path / "b")
    assert bay_tree_hash(a) == bay_tree_hash(b) == bay_tree_hash(RECEIVER)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", bay_tree_hash(a))


def test_receiver_hash_label_one_byte_changes_it(tmp_path: Path) -> None:
    tree = _receiver_copy(tmp_path)
    before = bay_tree_hash(tree)
    app = tree / "app.py"
    data = bytearray(app.read_bytes())
    data[-1] ^= 0x01
    app.write_bytes(bytes(data))
    assert bay_tree_hash(tree) != before
    # The Dockerfile is part of the build context too.
    tree2 = _receiver_copy(tmp_path / "two")
    (tree2 / "Dockerfile").write_text((tree2 / "Dockerfile").read_text() + "\n")
    assert bay_tree_hash(tree2) != before
    # A renamed file is another context, even with the same bytes.
    tree3 = _receiver_copy(tmp_path / "three")
    (tree3 / "requirements.txt").rename(tree3 / "requirements2.txt")
    assert bay_tree_hash(tree3) != before


def test_receiver_hash_label_ignores_mtimes_modes_and_bytecode(tmp_path: Path) -> None:
    tree = _receiver_copy(tmp_path)
    before = bay_tree_hash(tree)
    for path in tree.rglob("*"):
        os.utime(path, (1_000_000, 1_000_000))
    (tree / "app.py").chmod(0o600)
    cache = tree / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-312.pyc").write_bytes(b"\x00bytecode")
    (tree / "stray.pyc").write_bytes(b"\x00")
    assert bay_tree_hash(tree) == before


def test_receiver_hash_label_relative_path_is_the_framework_checkout() -> None:
    assert bay_tree_hash("roles/git_deploy/files/webhook") == bay_tree_hash(RECEIVER)
    with pytest.raises(ValueError, match="not a directory"):
        bay_tree_hash("roles/git_deploy/files/no-such-dir")


def _walk(tasks: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key) or [])


def _named(path: Path, name: str) -> dict[str, Any]:
    for task in _walk(yaml.safe_load(path.read_text())):
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task {name!r} in {path}")


def test_receiver_hash_label_on_the_image_and_the_container() -> None:
    render = ROOT / "roles" / "git_deploy" / "tasks" / "render_webhook.yml"
    specs = ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"

    # One expression, in both places, so the image and the container agree.
    fact = _named(render, "Compute the webhook receiver content hash")
    assert fact["ansible.builtin.set_fact"]["_webhook_receiver_hash"] == "{{ " + EXPR + " }}"
    spec = _named(specs, "Build webhook receiver container spec")
    assert spec["vars"]["_webhook_spec"]["labels"][LABEL] == "{{ " + EXPR + " }}"

    # The image is built with the label, and rebuilt when its label differs.
    build = _named(render, "Build webhook receiver image")
    cmd = build["ansible.builtin.command"]["cmd"]
    assert f"--label {LABEL}={{{{ _webhook_receiver_hash }}}}" in cmd
    when = build["when"]
    assert "_webhook_files.changed" in when
    assert "_webhook_image_inspect.rc != 0" in when
    assert "!= _webhook_receiver_hash" in when
    assert when.count("_webhook_image_inspect is not skipped") == 2, "check-mode guard"
    inspect = _named(render, "Check whether the webhook receiver image exists")
    assert LABEL in " ".join(inspect["ansible.builtin.command"]["argv"])
    assert inspect["check_mode"] is False

    # The fact is set before the build reads it.
    names = [t.get("name") for t in _walk(yaml.safe_load(render.read_text()))]
    assert names.index("Compute the webhook receiver content hash") < names.index(
        "Build webhook receiver image"
    )


# ── The check path ──────────────────────────────────────────────────────

_IMAGE_ID = "sha256:" + "ab" * 32


class _Docker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    containers: dict[str, dict[str, Any]]
    calls: list[str]


class _Api(http.server.BaseHTTPRequestHandler):
    """The few read calls the reconciler's plan-only pass makes. Nothing else."""

    protocol_version = "HTTP/1.1"
    server: _Docker

    def log_message(self, *args: Any) -> None:  # unix socket: no client address
        pass

    def _send(self, code: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = re.sub(r"^/v[0-9.]+", "", urlparse(self.path).path)
        self.server.calls.append(f"GET {path}")
        if path == "/_ping":
            self._send(200, "OK")
        elif path == "/version":
            self._send(200, {"ApiVersion": "1.44", "MinAPIVersion": "1.24", "Version": "25.0"})
        elif path == "/containers/json":
            self._send(200, [{"Id": cid} for cid in self.server.containers])
        elif m := re.fullmatch(r"/containers/([^/]+)/json", path):
            attrs = self.server.containers.get(m.group(1))
            self._send(200 if attrs else 404, attrs or {"message": "No such container"})
        elif m := re.fullmatch(r"/images/(.+)/json", path):
            ref = unquote(m.group(1))
            if ref == "bay-webhook:latest":
                self._send(200, {"Id": _IMAGE_ID, "RepoTags": [ref]})
            else:
                self._send(404, {"message": f"No such image: {ref}"})
        else:
            self._send(404, {"message": "not in the fake"})

    def _refuse(self) -> None:
        self.server.calls.append(f"{self.command} {self.path}")
        self._send(500, {"message": "a plan-only pass must not change anything"})

    do_POST = do_PUT = do_DELETE = _refuse  # noqa: N815


@pytest.fixture
def docker_api() -> Iterator[_Docker]:
    # A short directory: a unix socket path is limited to about 100 bytes.
    sock_dir = tempfile.mkdtemp(prefix="bay-d-")
    server = _Docker(os.path.join(sock_dir, "d.sock"), _Api)
    server.containers, server.calls = {}, []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(sock_dir, ignore_errors=True)


def _framework(tmp_path: Path) -> Path:
    """A framework copy whose receiver files the test may change.

    The roles the pass reads (alert_channel for the symlinked receiver
    module) and filter_plugins are copied; `bay_tree_hash` reads a relative
    path from the checkout that holds the filter, which is this copy.
    """
    fw = tmp_path / "fw"
    skip = shutil.ignore_patterns("__pycache__", "*.pyc")
    for role in ("container_lifecycle", "git_deploy", "alert_channel"):
        shutil.copytree(ROOT / "roles" / role, fw / "roles" / role, symlinks=True, ignore=skip)
    shutil.copytree(ROOT / "filter_plugins", fw / "filter_plugins", ignore=skip)
    (fw / "src").symlink_to(ROOT / "src")
    return fw


_SHOP = {
    "domains": ["shop.example.com"],
    "ports": {"internal": 3000},
    "build": {"repo": "https://example.com/acme/shop.git"},
}

_PLAY = """\
- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: Plan the receiver as the git_deploy receiver pass does
      ansible.builtin.include_role:
        name: container_lifecycle
      vars:
        container_lifecycle_only: [bay-webhook]
        bay_reconciler_remove_orphans: false
    - name: Keep the bundle entries the plan used
      ansible.builtin.copy:
        content: "{{{{ _reconcile_entries | to_json }}}}"
        dest: {entries}
        mode: "0600"
      check_mode: false
"""


def _check(fw: Path, docker: _Docker, tmp_path: Path, run: str) -> tuple[dict[str, Any], dict]:
    """One `ansible-playbook --check` of the receiver pass. Returns (report, entry)."""
    work = tmp_path / run
    report_dir = work / "report"
    report_dir.mkdir(parents=True)
    stack = tmp_path / "stack"
    (stack / "env").mkdir(parents=True, exist_ok=True)
    (stack / ".reconcile").mkdir(exist_ok=True)
    (stack / "env" / "bay-webhook.env").write_text("WEBHOOK_SECRET=placeholder\n")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    # The pass runs `python3 -m bay_reconcile`; this python has the docker SDK.
    python3 = bindir / "python3"
    python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python3.chmod(0o755)
    entries = work / "entries.json"
    play = fw / f"play-{run}.yml"
    play.write_text(_PLAY.format(entries=entries))
    variables = {
        "stack_dir": str(stack),
        "stack_name": "testfleet",
        "app_user": getpass.getuser(),
        "bay_alert_host": "box-1",
        "target_host": "production",
        "webhook": {"domain": "hooks.example.com", "secret": "placeholder"},
        "services": {"shop": _SHOP},
        "active_services": {"shop": _SHOP},
        "bay_reconciler_plan_only": True,
        "bay_reconciler_plan_report_dir": str(report_dir),
        "ansible_python_interpreter": sys.executable,
    }
    (work / "vars.json").write_text(json.dumps(variables))
    (work / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "DOCKER_HOST": f"unix://{docker.server_address}",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
        "ANSIBLE_REMOTE_TEMP": str(tmp_path / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
    }
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         "-e", f"@{work / 'vars.json'}", str(play), "--check"],
        cwd=fw, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    (entry,) = [e for e in json.loads(entries.read_text()) if e["name"] == "bay-webhook"]
    return json.loads((report_dir / "localhost.json").read_text()), entry


def _deployed(entry: dict[str, Any]) -> dict[str, Any]:
    """The container a real deploy creates from this bundle entry (sdk_client.create)."""
    labels = {
        **entry["labels"],
        "com.bay.config-hash": entry["config_hash"],
        "bay.managed": "true",
        "bay.stack": "testfleet",
    }
    return {
        "Id": "c0ffee",
        "Name": "/bay-webhook",
        "Image": _IMAGE_ID,
        "Config": {
            "Image": entry["image"],
            "Labels": labels,
            "Env": [f"{k}={v}" for k, v in entry["env"].items()],
        },
        "State": {"Status": "running", "Running": True},
        "HostConfig": {"Binds": list(entry["volumes"]), "PortBindings": {}},
        "RestartCount": 0,
    }


def _receiver(report: dict[str, Any]) -> dict[str, Any]:
    (row,) = [c for c in report["containers"] if c["name"] == "bay-webhook"]
    return row


def _plan_steps(report_dir: Path) -> list[dict[str, Any]]:
    from bay_cli import plan as planmod

    prediction = planmod.box_prediction(planmod.read_box_predictions(report_dir))
    assert prediction["checked"] and not prediction["errors"], prediction
    return planmod.box_steps(prediction, set(), project=None, mine=set())


@pytest.mark.skipif(
    not any(g.gr_name == "docker" for g in grp.getgrall()),
    reason="the reconcile role sets group docker on the files it would write",
)
def test_plan_predicts_receiver_recreate(
    tmp_path: Path, docker_api: _Docker
) -> None:
    fw = _framework(tmp_path)
    app = fw / "roles" / "git_deploy" / "files" / "webhook" / "app.py"

    # A box with no receiver yet: the plan creates it. The entry is what a
    # real deploy would stamp on the container.
    report, entry = _check(fw, docker_api, tmp_path, "first")
    assert _receiver(report)["action"] == "create"
    assert entry["labels"][LABEL] == bay_tree_hash(app.parent)
    docker_api.containers = {"c0ffee": _deployed(entry)}

    # Same receiver files (new mtimes only): noop, and no plan step.
    later = time.time() + 3600
    for path in app.parent.iterdir():
        os.utime(path, (later, later), follow_symlinks=False)
    report, again = _check(fw, docker_api, tmp_path, "same")
    assert _receiver(report)["action"] == "noop", _receiver(report)
    assert again["config_hash"] == entry["config_hash"]
    assert _plan_steps(tmp_path / "same" / "report") == []

    # One byte of the receiver changes. The image is not built in check
    # mode, and the image ID still matches, but the plan predicts the recreate.
    data = bytearray(app.read_bytes())
    data[-1] ^= 0x01
    app.write_bytes(bytes(data))
    report, changed = _check(fw, docker_api, tmp_path, "changed")
    row = _receiver(report)
    assert row["action"] == "recreate", row
    assert any(r.startswith("labels: differ for") and LABEL in r for r in row["reasons"]), row
    assert not any(r.startswith("image:") for r in row["reasons"]), "the image did not move"
    assert changed["labels"][LABEL] != entry["labels"][LABEL]
    steps = _plan_steps(tmp_path / "changed" / "report")
    assert [(s["container"], s["action"], s["source"]) for s in steps] == [
        ("bay-webhook", "recreate", "box")
    ]
    assert LABEL in steps[0]["reason"]

    # A container from before 2.4.0 has no receiver label: one recreate.
    old = _deployed(entry)
    del old["Config"]["Labels"][LABEL]
    old["Config"]["Labels"]["com.bay.config-hash"] = "0" * 64
    docker_api.containers = {"c0ffee": old}
    report, _ = _check(fw, docker_api, tmp_path, "unlabelled")
    row = _receiver(report)
    assert row["action"] == "recreate" and any(LABEL in r for r in row["reasons"]), row

    # The fake saw reads only.
    assert all(c.startswith("GET ") for c in docker_api.calls), docker_api.calls
