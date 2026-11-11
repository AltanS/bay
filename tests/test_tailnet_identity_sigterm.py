"""The tailnet-identity sidecar must stop on SIGTERM.

In a container the server is PID 1. The kernel does not apply the default
SIGTERM action to PID 1, so without a handler `docker stop` waited 30 s and
then killed the container. The server now shuts down on SIGTERM and SIGINT
and exits 0.

Test 1 runs the script as an ordinary process: before the fix SIGTERM ended
it by signal (return code -15), now it exits 0 after a clean shutdown.
Test 2 runs it as PID 1 of a fresh PID namespace, which is the real case: the
signal is ignored without a handler. It skips where unshare is not allowed.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

APP = (
    Path(__file__).resolve().parent.parent
    / "roles" / "tailnet_identity" / "files" / "tailnet-identity" / "app.py"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start(prefix: list[str]) -> tuple[subprocess.Popen, int]:
    port = _free_port()
    env = {**os.environ, "LISTEN_PORT": str(port), "IDENTITY_SOURCE": "sqlite"}
    proc = subprocess.Popen(
        [*prefix, sys.executable, "-I", str(APP)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                return proc, port
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
    proc.kill()
    out = proc.communicate()[0]
    raise AssertionError(f"server did not start: {out}")


def _stop_with_sigterm(proc: subprocess.Popen, sig: int = 15) -> tuple[int, float, str]:
    started = time.monotonic()
    proc.send_signal(sig)
    try:
        out = proc.communicate(timeout=3)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError("server ignored the signal for 3 s") from None
    return proc.returncode, time.monotonic() - started, out


def test_sigterm_ends_the_server_cleanly_within_three_seconds():
    proc, _ = _start([])
    code, took, out = _stop_with_sigterm(proc)
    assert code == 0, out
    assert took < 3
    assert "received SIGTERM" in out


def test_sigint_ends_the_server_cleanly_too():
    proc, _ = _start([])
    code, _took, _out = _stop_with_sigterm(proc, 2)
    assert code == 0


def test_the_server_still_answers_before_the_signal():
    proc, port = _start([])
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            assert resp.status == 200
    finally:
        _stop_with_sigterm(proc)


def _can_unshare() -> bool:
    if shutil.which("unshare") is None:
        return False
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--pid", "--fork", "true"],
        capture_output=True,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _can_unshare(), reason="needs unshare with user and pid namespaces")
def test_sigterm_ends_the_server_when_it_is_pid_one():
    prefix = ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--kill-child"]
    proc, _ = _start(prefix)
    # `unshare --fork` is the parent. Its child is PID 1 of the namespace. A
    # signal from the outer namespace reaches PID 1 only if it has a handler
    # (pid_namespaces(7)), so this is the case `docker stop` meets.
    children = Path(f"/proc/{proc.pid}/task/{proc.pid}/children").read_text().split()
    assert children, "no child of unshare"
    started = time.monotonic()
    os.kill(int(children[0]), 15)
    try:
        code = proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError("PID 1 ignored SIGTERM for 3 s") from None
    assert time.monotonic() - started < 3
    out = proc.communicate()[0]
    assert "received SIGTERM" in out, out
    assert code == 0, out
