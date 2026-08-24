"""Unit tests for Zot htpasswd idempotency (registry restart on every deploy).

Observed in production: the self-hosted registry container went from
`Up 36 hours` to `Up 55 seconds` after a deploy that changed nothing about
its credentials.

Root cause: `roles/zot/tasks/main.yml` generated the htpasswd line with
`htpasswd -bBn`, and bcrypt mixes in a RANDOM SALT — so the generated hash
differs on every invocation even when username and password are unchanged.
The `Write htpasswd file` copy compared *content*, therefore always reported
`changed`, therefore always fired the `Restart zot` handler.

Why it matters beyond noise: in a multi-region rollout the other regions pull
images FROM this registry. The documented deploy order (infra → eu → na)
happens to dodge the race, but a re-ordered or `--limit` deploy can hit the
registry mid-bounce.

The fix verifies before regenerating: `htpasswd -bvB <file> <user> <pass>`
exits 0 only when the stored hash matches, so both the generate and the write
tasks are gated on `rc != 0`. Credential rotation still rewrites (the verify
fails), and so does a missing / empty / malformed file.

Two layers of coverage here, matching the conventions in
`test_zot_storage.py` / `test_zot_tailnet.py`:

  A. Task-shape contract parsed out of the role YAML — the gate exists, is
     wired to the verify task's register, and the security properties
     (`no_log`, owner, mode 0600) survive.
  B. Behavioural simulation that actually shells out to the real `htpasswd`
     binary using the command strings *parsed from the role* and the `when`
     expression *evaluated with Jinja* — so the exit-code contract the gate
     depends on is pinned against apache2-utils itself, not against a
     restatement of it. Skipped when `htpasswd` is unavailable.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from helpers import make_ansible_env

_REPO_ROOT = Path(__file__).parent.parent
_ZOT_TASKS = _REPO_ROOT / "roles" / "zot" / "tasks" / "main.yml"
_ZOT_HANDLERS = _REPO_ROOT / "roles" / "zot" / "handlers" / "main.yml"

_VERIFY = "Verify existing htpasswd file matches configured credentials"
_GENERATE = "Generate htpasswd file"
_WRITE = "Write htpasswd file"

_HAS_HTPASSWD = shutil.which("htpasswd") is not None
_needs_htpasswd = pytest.mark.skipif(
    not _HAS_HTPASSWD,
    reason="htpasswd (apache2-utils) not installed on this test host",
)


def _tasks() -> list[dict]:
    return yaml.safe_load(_ZOT_TASKS.read_text())


def _optional_task(name: str) -> dict | None:
    for task in _tasks():
        if task.get("name") == name:
            return task
    return None


def _task(name: str) -> dict:
    task = _optional_task(name)
    if task is not None:
        return task
    raise AssertionError(
        f"task {name!r} not found in {_ZOT_TASKS} — the htpasswd write must be "
        f"gated on a verify step, or the registry restarts on every deploy "
        f"(bcrypt re-salts, so the generated content never matches)."
    )


def _cmd(task: dict) -> str:
    return task["ansible.builtin.command"]["cmd"]


def _render(template: str, **ctx) -> str:
    """Render a role snippet with ansible's Jinja2 settings."""
    env = make_ansible_env(_ZOT_TASKS.parent)
    return env.from_string(template).render(**ctx)


def _when_holds(expression: str, rc: int) -> bool:
    """Evaluate a task's `when:` the way Ansible would, for a given rc."""
    rendered = _render(
        "{{ " + expression + " }}",
        _zot_htpasswd_check={"rc": rc},
    )
    assert rendered in ("True", "False"), (
        f"when expression {expression!r} did not evaluate to a bool for "
        f"rc={rc}: {rendered!r}"
    )
    return rendered == "True"


# ── A. Task-shape contract ──────────────────────────────────────────


class TestHtpasswdVerifyTask:
    def test_verify_task_probes_the_deployed_file(self) -> None:
        cmd = _cmd(_task(_VERIFY))
        assert " -bvB " in f" {cmd} " or "-bv" in cmd, (
            "the verify task must use htpasswd's verify mode (-v); without it "
            "there is nothing to compare the stored hash against"
        )
        assert "{{ zot_config_dir }}/htpasswd" in cmd, (
            "verify must run against the deployed htpasswd file"
        )
        assert "{{ _zot_creds.username }}" in cmd
        assert "{{ _zot_creds.password }}" in cmd

    def test_verify_task_never_reports_changed_or_fails(self) -> None:
        """A non-zero rc is the *signal*, not an error: htpasswd exits 3 on a
        rotated password and 6/7 on a missing or malformed file. The probe
        must swallow that and never mark the host changed."""
        task = _task(_VERIFY)
        assert task["changed_when"] is False
        assert task["failed_when"] is False
        assert "register" in task

    def test_verify_task_runs_in_check_mode(self) -> None:
        """ansible-core >= 2.19 registers rc 0 for a command task skipped BY
        check mode, so a probe without `check_mode: false` makes `--check`
        silently predict 'no change' even for a real credential rotation.
        The probe is read-only, so running it under --check is safe."""
        assert _task(_VERIFY)["check_mode"] is False

    def test_verify_task_hides_the_password(self) -> None:
        assert _task(_VERIFY)["no_log"] is True, (
            "the verify command line carries the real registry password"
        )


class TestHtpasswdWriteIsGated:
    def test_write_task_is_gated_on_the_verify_result(self) -> None:
        """THE regression guard. Unconditional write + bcrypt re-salting =
        a registry restart on every single deploy."""
        task = _task(_WRITE)
        when = task.get("when")
        assert when is not None, (
            "'Write htpasswd file' must be gated on the verify result — an "
            "unconditional copy always reports changed because bcrypt "
            "re-salts, which fires 'Restart zot' on every deploy."
        )
        register = _task(_VERIFY)["register"]
        assert register in when, (
            f"the write gate must consume {register!r} from the verify task, "
            f"got when: {when!r}"
        )

    def test_generate_task_is_gated_too(self) -> None:
        """Not correctness-critical (it only writes to stdout) but it keeps
        a bcrypt hash out of the play's memory on the steady-state path."""
        when = _task(_GENERATE).get("when")
        assert when is not None
        assert _task(_VERIFY)["register"] in when

    def test_gate_fires_on_every_non_zero_rc(self) -> None:
        """rc 1 (missing file), 3 (password changed), 6 (username changed /
        empty file), 7 (malformed file) must all rewrite; only rc 0 skips."""
        when = _task(_WRITE)["when"]
        assert not _when_holds(when, 0), (
            "a matching stored hash must NOT rewrite the file"
        )
        for rc in (1, 2, 3, 4, 5, 6, 7):
            assert _when_holds(when, rc), (
                f"htpasswd rc={rc} means the stored hash does not match the "
                f"configured credentials — the file must be rewritten"
            )

    def test_write_task_keeps_ownership_mode_and_handler(self) -> None:
        task = _task(_WRITE)
        copy = task["ansible.builtin.copy"]
        assert copy["dest"] == "{{ zot_config_dir }}/htpasswd"
        assert copy["owner"] == "{{ app_user }}"
        assert copy["mode"] == "0600"
        assert task["notify"] == "Restart zot"
        assert task["no_log"] is True

    def test_generate_task_still_uses_bcrypt(self) -> None:
        """Idempotency must not be bought by downgrading the hash to an
        unsalted/weak algorithm — bcrypt (-B) stays, gating is what changed."""
        cmd = _cmd(_task(_GENERATE))
        assert "-bBn" in cmd or ("-B" in cmd and "-n" in cmd)

    def test_restart_handler_still_exists(self) -> None:
        """The gate is only meaningful if the handler it protects is real."""
        handlers = yaml.safe_load(_ZOT_HANDLERS.read_text())
        assert any(h.get("name") == "Restart zot" for h in handlers)

    def test_apache2_utils_install_precedes_the_verify(self) -> None:
        """htpasswd comes from apache2-utils, installed earlier in this same
        file. Moving the verify above it would break a fresh host."""
        names = [t.get("name") for t in _tasks()]
        assert names.index("Install apache2-utils for htpasswd") < names.index(
            _VERIFY
        )


# ── B. Behavioural simulation against the real htpasswd ─────────────


@_needs_htpasswd
class TestHtpasswdBehaviour:
    """Run the role's own command strings against apache2-utils."""

    def _run_role(self, config_dir: Path, username: str, password: str) -> bool:
        """Execute the role's htpasswd tasks for real.

        Returns True when the file was (re)written — i.e. when the deploy
        would fire `Restart zot`.

        Deliberately tolerant of a MISSING verify task or a MISSING `when`:
        this class must reproduce the *behaviour* of whatever the role
        currently says, so it fails with a semantic message ("unchanged
        credentials rewrote the file") rather than a name lookup error.
        Task shape is pinned separately, above.
        """
        ctx = {
            "zot_config_dir": str(config_dir),
            "_zot_creds": {"username": username, "password": password},
        }
        verify_task = _optional_task(_VERIFY)
        rc = 0
        if verify_task is not None:
            rc = subprocess.run(
                shlex.split(_render(_cmd(verify_task), **ctx)),
                capture_output=True,
                text=True,
                check=False,
            ).returncode
        when = _task(_WRITE).get("when")
        if when is not None and not _when_holds(when, rc):
            return False
        generated = subprocess.run(
            shlex.split(_render(_cmd(_task(_GENERATE)), **ctx)),
            capture_output=True,
            text=True,
            check=True,
        )
        # Mirrors `content: "{{ _zot_htpasswd.stdout }}\n"` — Ansible strips
        # trailing newlines off .stdout, the template adds exactly one back.
        target = config_dir / "htpasswd"
        target.write_text(generated.stdout.rstrip("\r\n") + "\n")
        target.chmod(0o600)
        return True

    def test_bcrypt_regenerates_a_different_hash_every_time(self) -> None:
        """The root cause, pinned: this is why content comparison can never
        settle and why a verify step is required."""
        cmd = shlex.split(
            _render(
                _cmd(_task(_GENERATE)),
                _zot_creds={"username": "bay", "password": "hunter2"},
            )
        )
        first = subprocess.run(cmd, capture_output=True, text=True, check=True)
        second = subprocess.run(cmd, capture_output=True, text=True, check=True)
        assert first.stdout != second.stdout, (
            "htpasswd -bB is expected to emit a fresh random salt per run; "
            "if this ever stops being true the gate is still correct, but the "
            "premise of this test file has changed"
        )

    def test_fresh_host_creates_the_file(self, tmp_path: Path) -> None:
        assert self._run_role(tmp_path, "bay", "hunter2") is True
        written = (tmp_path / "htpasswd").read_text()
        assert written.startswith("bay:$2")
        assert written.endswith("\n")

    def test_second_run_is_a_no_op(self, tmp_path: Path) -> None:
        """The bug, end to end: unchanged credentials must not rewrite."""
        assert self._run_role(tmp_path, "bay", "hunter2") is True
        assert self._run_role(tmp_path, "bay", "hunter2") is False, (
            "unchanged credentials rewrote the htpasswd file — the registry "
            "would restart on every deploy"
        )

    def test_no_op_run_leaves_the_file_byte_identical(self, tmp_path: Path) -> None:
        """`htpasswd -v` must not mutate the file it probes."""
        self._run_role(tmp_path, "bay", "hunter2")
        before = (tmp_path / "htpasswd").read_bytes()
        self._run_role(tmp_path, "bay", "hunter2")
        assert (tmp_path / "htpasswd").read_bytes() == before

    def test_password_rotation_rewrites(self, tmp_path: Path) -> None:
        self._run_role(tmp_path, "bay", "hunter2")
        before = (tmp_path / "htpasswd").read_text()
        assert self._run_role(tmp_path, "bay", "new-password") is True, (
            "a rotated password MUST rewrite the file and restart zot"
        )
        after = (tmp_path / "htpasswd").read_text()
        assert after != before
        # ...and the new credentials must actually verify afterwards.
        assert self._run_role(tmp_path, "bay", "new-password") is False

    def test_username_rotation_rewrites(self, tmp_path: Path) -> None:
        self._run_role(tmp_path, "bay", "hunter2")
        assert self._run_role(tmp_path, "bay-2", "hunter2") is True, (
            "a rotated username MUST rewrite the file and restart zot"
        )
        written = (tmp_path / "htpasswd").read_text()
        assert written.startswith("bay-2:")
        assert "bay:" not in written, (
            "the copy replaces the whole file, so the retired user must be "
            "gone — a stale line would keep the old credentials valid"
        )

    @pytest.mark.parametrize(
        "corruption",
        ["", "\n", "garbage-not-a-htpasswd-line\n", "bay:\n"],
        ids=["empty", "blank-line", "malformed", "empty-hash"],
    )
    def test_corrupt_file_is_regenerated(
        self, tmp_path: Path, corruption: str
    ) -> None:
        self._run_role(tmp_path, "bay", "hunter2")
        (tmp_path / "htpasswd").write_text(corruption)
        assert self._run_role(tmp_path, "bay", "hunter2") is True, (
            f"a {corruption!r} htpasswd file must be regenerated"
        )
        assert self._run_role(tmp_path, "bay", "hunter2") is False

    def test_missing_file_is_regenerated(self, tmp_path: Path) -> None:
        self._run_role(tmp_path, "bay", "hunter2")
        (tmp_path / "htpasswd").unlink()
        assert self._run_role(tmp_path, "bay", "hunter2") is True
