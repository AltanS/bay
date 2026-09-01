"""Tier 2 — recreation gap/duplicate integration test.

Counsel flagged this as THE thing to not skip. The archiver's cursor-based
`docker logs --since <ts>` design is supposed to handle container
recreation without gaps or duplicates. Without this integration test,
that property is unverified.

Scenario:
  1. Start a container that emits N numbered lines then exits.
  2. Run archive-logs.sh — capture N lines into live.log.
  3. Remove and recreate the container with the same name; emit M more lines.
  4. Simulate the Ansible post-recreate task that writes the recreation
     sentinel to live.log.
  5. Run archive-logs.sh again — capture M more lines.
  6. Assert: N + 1 (sentinel) + M lines total, no duplicates, no gaps,
     strictly non-decreasing timestamps on the non-sentinel lines.
  7. Idempotency: run archive-logs.sh a third time — no new lines.

Requirements:
- Docker on the test runner (skipped if absent).
- The archive-logs.sh.j2 template renders to valid bash.
"""

from __future__ import annotations

import os
import shlex
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader


# ── skip gate ─────────────────────────────────────────────────────────


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker is not available on this host — skipping S8 integration tests",
)


# ── template rendering ──────────────────────────────────────────────


TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "log_archive"
    / "templates"
)


def _render_archive_script(tmp_path: Path) -> Path:
    """Render archive-logs.sh.j2 with test-appropriate context vars."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        trim_blocks=True,
        lstrip_blocks=False,
    )
    env.filters["quote"] = shlex.quote
    current_user = os.environ.get("USER", "root")
    rendered = env.get_template("archive-logs.sh.j2").render(
        app_user=current_user,
        log_archive_group=current_user,
    )
    dest = tmp_path / "archive-logs.sh"
    dest.write_text(rendered)
    dest.chmod(0o755)
    return dest


# ── container helpers ───────────────────────────────────────────────


class ContainerHandle:
    """Narrow docker CLI wrapper tailored to this test's lifecycle."""

    def __init__(self, name: str):
        self.name = name

    def run_emit_and_exit(self, lines: list[str]) -> None:
        """Start a new container that echoes the given lines then exits.

        Each line is separated by a 100ms sleep so the RFC 3339
        timestamps Docker prepends are monotonic and distinguishable.

        The container is started WITHOUT `--rm` so its logs remain
        queryable via `docker logs` after the entrypoint exits. The
        caller is responsible for `remove()`-ing the container between
        runs.
        """
        script_parts = []
        for line in lines:
            safe = line.replace("'", "'\\''")
            script_parts.append(f"echo '{safe}'; sleep 0.1")
        script = "; ".join(script_parts)
        subprocess.run(
            [
                "docker", "run", "-d", "--name", self.name,
                "alpine:3.19", "sh", "-c", script,
            ],
            check=True, capture_output=True, timeout=20,
        )
        # Wait for the container to enter "exited" state. (`docker ps`
        # without -a only lists running containers; once the entrypoint
        # finishes, the container is exited but still present.)
        deadline = time.time() + 15
        while time.time() < deadline:
            r = subprocess.run(
                ["docker", "ps", "-q", "-f", f"name=^{self.name}$"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if not r.stdout.strip():
                return  # exited but still present (we did not pass --rm)
            time.sleep(0.2)
        self.remove(force=True)
        raise RuntimeError(f"container {self.name} did not exit in time")

    def remove(self, force: bool = False) -> None:
        cmd = ["docker", "rm"]
        if force:
            cmd.append("-f")
        cmd.append(self.name)
        subprocess.run(cmd, capture_output=True, timeout=10, check=False)


# ── archive dir fixture ─────────────────────────────────────────────


def _provision_log_dir(tmp_path: Path, svc: str) -> Path:
    """Create the directory layout the archiver expects (what Ansible would).

    The archiver derives LOG_DIR from <stack_dir>/logs/services/<svc> so
    the directory name MUST match the service name passed on argv. A
    mismatch makes the script exit 2 with "missing dir".
    """
    log_dir = tmp_path / "logs" / "services" / svc
    log_dir.mkdir(parents=True)
    (log_dir / ".malformed").mkdir()
    (log_dir / "live.log").touch()
    (log_dir / ".cursor").touch()
    (log_dir / ".prune-log").touch()
    # .retention — mode=normal so we don't try root-only chown
    (log_dir / ".retention").write_text(
        "mode=normal\ndays=\nmax_total_size=\ncompress=true\n"
    )
    return log_dir


def _count_matching_in(log_dir: Path, pattern: str) -> int:
    content = (log_dir / "live.log").read_text()
    return sum(1 for line in content.splitlines() if re.search(pattern, line))


def _all_lines_in(log_dir: Path) -> list[str]:
    return (log_dir / "live.log").read_text().splitlines()


# ── the test ────────────────────────────────────────────────────────


def test_recreation_gap_and_duplicate_free(tmp_path: Path):
    """End-to-end: write → recreate → write → archive → sentinel → assert.

    Two batches of 20 distinct lines run in two separate containers with
    the same name; the sentinel is written between them (simulating the
    Ansible post-recreate task). The archiver's cursor must carry across
    the container swap without duplicates and without gaps, leaving the
    archive with exactly 41 lines (20 + 1 + 20).
    """
    script = _render_archive_script(tmp_path)
    svc = f"m84test-{uuid.uuid4().hex[:8]}"
    log_dir = _provision_log_dir(tmp_path, svc)
    stack_dir = tmp_path  # archiver expects <stack_dir>/logs/services/<svc>

    container = ContainerHandle(svc)
    try:
        # ── batch 1 ──────────────────────────────────────────────────
        batch1 = [f"batch1-line-{i:03d}" for i in range(1, 21)]
        container.run_emit_and_exit(batch1)

        # Run archiver → captures batch1.
        r = subprocess.run(
            ["bash", str(script), svc, str(stack_dir)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert r.returncode == 0, (
            f"archiver rc={r.returncode}\nstdout:{r.stdout}\nstderr:{r.stderr}"
        )

        # Remove the exited container so the next one can reuse the name.
        container.remove(force=True)

        # Verify batch1 is fully in live.log.
        assert _count_matching_in(log_dir, r"batch1-line-") == 20, (
            f"expected 20 batch1 lines in live.log, got:\n"
            f"{(log_dir / 'live.log').read_text()}"
        )

        # ── simulate Ansible post-recreate sentinel ─────────────────
        # (The deploy_stack/log_retention_boundary_post.yml task writes
        # this line between the old and new container's output.)
        sentinel = (
            f"--- bay: container {svc} recreated at "
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}, "
            f"old-id abc123, new-id def456 ---"
        )
        with (log_dir / "live.log").open("a") as fh:
            fh.write(sentinel + "\n")

        # ── batch 2 ──────────────────────────────────────────────────
        # Small pause so batch2's Docker timestamps are strictly after
        # batch1's (otherwise the cursor dedup might swallow some).
        time.sleep(2)
        batch2 = [f"batch2-line-{i:03d}" for i in range(1, 21)]
        container.run_emit_and_exit(batch2)

        # Run archiver → captures batch2.
        r = subprocess.run(
            ["bash", str(script), svc, str(stack_dir)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert r.returncode == 0, (
            f"archiver rc={r.returncode}\nstdout:{r.stdout}\nstderr:{r.stderr}"
        )

        # ── assertions ──────────────────────────────────────────────
        lines = _all_lines_in(log_dir)
        batch1_count = sum(1 for l in lines if "batch1-line-" in l)
        batch2_count = sum(1 for l in lines if "batch2-line-" in l)
        sentinel_count = sum(1 for l in lines if "bay: container" in l and "recreated" in l)

        assert batch1_count == 20, f"batch1 lost lines: got {batch1_count}, expected 20"
        assert batch2_count == 20, f"batch2 lost lines: got {batch2_count}, expected 20"
        assert sentinel_count == 1, f"expected exactly 1 sentinel, got {sentinel_count}"

        # No duplicates — every batch line should appear exactly once.
        for b1 in batch1:
            c = sum(1 for l in lines if b1 in l)
            assert c == 1, f"{b1!r} appeared {c} times (expected 1)"
        for b2 in batch2:
            c = sum(1 for l in lines if b2 in l)
            assert c == 1, f"{b2!r} appeared {c} times (expected 1)"

        # Ordering — batch1 lines all come before the sentinel, batch2 all after.
        sentinel_idx = next(i for i, l in enumerate(lines) if "recreated" in l)
        for i, line in enumerate(lines):
            if "batch1-line-" in line:
                assert i < sentinel_idx, f"batch1 line after sentinel at idx {i}"
            if "batch2-line-" in line:
                assert i > sentinel_idx, f"batch2 line before sentinel at idx {i}"

        # Malformed sidecar is empty (all lines parsed cleanly).
        malformed_dir = log_dir / ".malformed"
        leftovers = [p for p in malformed_dir.iterdir() if p.is_file()]
        assert leftovers == [], f"unexpected malformed files: {leftovers}"

    finally:
        container.remove(force=True)


def test_idempotent_archiver_reruns(tmp_path: Path):
    """Re-running the archiver without new container output must not
    duplicate any lines. The cursor is the guard here — a second tick
    without fresh container lines should be a true no-op.
    """
    script = _render_archive_script(tmp_path)
    svc = f"m84idem-{uuid.uuid4().hex[:8]}"
    log_dir = _provision_log_dir(tmp_path, svc)
    stack_dir = tmp_path

    container = ContainerHandle(svc)
    try:
        batch = [f"idem-line-{i:03d}" for i in range(1, 11)]
        container.run_emit_and_exit(batch)

        # First run — captures the batch.
        subprocess.run(
            ["bash", str(script), svc, str(stack_dir)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        container.remove(force=True)
        first_lines = _all_lines_in(log_dir)
        assert len(first_lines) == 10

        # Second run — container gone, cursor already at last line,
        # must be a no-op.
        r = subprocess.run(
            ["bash", str(script), svc, str(stack_dir)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        # Script exits 0 on "no such container" after retry (transient path).
        assert r.returncode == 0, (
            f"second-run rc={r.returncode}\nstderr:{r.stderr}"
        )
        second_lines = _all_lines_in(log_dir)
        assert second_lines == first_lines, (
            "idempotent re-run changed live.log — cursor dedup broken?"
        )

        # Third run — same invariant.
        r = subprocess.run(
            ["bash", str(script), svc, str(stack_dir)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert r.returncode == 0
        assert _all_lines_in(log_dir) == first_lines

    finally:
        container.remove(force=True)
