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

Pull-only accounts (`zot_pull_users`) made the file hold several lines: the
admin from docker_registries plus one per pull account. Verify runs once per
wanted account, and a second probe reads the user names already in the file,
because verify alone cannot see a line that should NOT be there (a removed
pull account, an unknown line added by hand). Either check failing
regenerates the WHOLE file; both passing leaves it alone.

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
from ansible.plugins.filter.core import to_bool

from helpers import make_ansible_env

_REPO_ROOT = Path(__file__).parent.parent
_ZOT_TASKS = _REPO_ROOT / "roles" / "zot" / "tasks" / "main.yml"
_ZOT_HANDLERS = _REPO_ROOT / "roles" / "zot" / "handlers" / "main.yml"

_COLLECT = "Collect the htpasswd accounts (admin plus pull-only)"
_NAMES = "Read user names in the existing htpasswd file"
_VERIFY = "Verify existing htpasswd file matches configured credentials"
_DECIDE = "Decide whether the htpasswd file must be regenerated"
_GENERATE = "Generate htpasswd file"
_WRITE = "Write htpasswd file"

_HAS_HTPASSWD = shutil.which("htpasswd") is not None
_needs_htpasswd = pytest.mark.skipif(
    not _HAS_HTPASSWD,
    reason="htpasswd (apache2-utils) not installed on this test host",
)

_ADMIN = {"domain": "registry.example.com", "username": "bay", "password": "hunter2"}


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


def _env():
    env = make_ansible_env(_ZOT_TASKS.parent)
    env.filters["bool"] = to_bool
    return env


def _render(template: str, **ctx) -> str:
    """Render a role snippet with ansible's Jinja2 settings."""
    return _env().from_string(template).render(**ctx)


def _expr(template: str, **ctx):
    """Evaluate a `"{{ expr }}"` value to its native Python value.

    Ansible keeps the native type of a template that is one expression, so a
    set_fact of a list stays a list. compile_expression gives the same.
    """
    body = template.strip()
    assert body.startswith("{{") and body.endswith("}}"), body
    return _env().compile_expression(body[2:-2].strip())(**ctx)


def _stdin(task: dict, **ctx) -> str | None:
    """Render a task's `stdin:`, the way Ansible feeds it to the command.

    `htpasswd -i` reads the password from stdin, so a behavioural test that
    forgets this hangs rather than fails. Ansible appends a newline by default
    (stdin_add_newline), and htpasswd strips it.
    """
    raw = task["ansible.builtin.command"].get("stdin")
    if raw is None:
        return None
    return _render(raw, **ctx) + "\n"


def _decide(rcs: list[int], names: list[str], users: list[dict]) -> bool:
    """Evaluate the decide task's expression for given probe results."""
    expression = _task(_DECIDE)["ansible.builtin.set_fact"]["_zot_htpasswd_regenerate"]
    value = _expr(
        expression,
        _zot_htpasswd_check={"results": [{"rc": rc} for rc in rcs]},
        _zot_htpasswd_names={"stdout_lines": names},
        _zot_htpasswd_users=users,
    )
    assert isinstance(value, bool), f"decision is not a bool: {value!r}"
    return value


# ── A. Task-shape contract ──────────────────────────────────────────


class TestHtpasswdAccounts:
    def test_collect_task_puts_admin_first_then_pull_users(self) -> None:
        task = _task(_COLLECT)
        users = _expr(
            task["ansible.builtin.set_fact"]["_zot_htpasswd_users"],
            _zot_creds=_ADMIN,
            zot_pull_users=[{"username": "dev", "password": "pw"}],
        )
        assert [u["username"] for u in users] == ["bay", "dev"]

    def test_collect_task_tolerates_a_null_pull_list(self) -> None:
        """`zot_pull_users:` with no value is None in YAML."""
        users = _expr(
            _task(_COLLECT)["ansible.builtin.set_fact"]["_zot_htpasswd_users"],
            _zot_creds=_ADMIN,
            zot_pull_users=None,
        )
        assert [u["username"] for u in users] == ["bay"]

    def test_collect_task_hides_the_passwords(self) -> None:
        assert _task(_COLLECT)["no_log"] is True


class TestHtpasswdProbes:
    def test_verify_task_probes_the_deployed_file_per_account(self) -> None:
        task = _task(_VERIFY)
        cmd = _cmd(task)
        assert "-iv" in cmd, (
            "the verify task must use htpasswd's verify mode (-v); without it "
            "there is nothing to compare the stored hash against"
        )
        assert "{{ zot_config_dir }}/htpasswd" in cmd, (
            "verify must run against the deployed htpasswd file"
        )
        assert "{{ item.username }}" in cmd
        assert task["loop"] == "{{ _zot_htpasswd_users }}"
        # The password goes in on stdin (-i), not on the argument list, where
        # `ps` showed it to every local user for the life of the call.
        assert "password" not in cmd
        assert task["ansible.builtin.command"]["stdin"] == "{{ item.password }}"

    def test_verify_task_never_reports_changed_or_fails(self) -> None:
        """A non-zero rc is the *signal*, not an error: htpasswd exits 3 on a
        rotated password and 6/7 on a missing or malformed file. The probe
        must swallow that and never mark the host changed."""
        task = _task(_VERIFY)
        assert task["changed_when"] is False
        assert task["failed_when"] is False
        assert "register" in task

    @pytest.mark.parametrize("name", [_NAMES, _VERIFY, _GENERATE])
    def test_probes_run_in_check_mode(self, name: str) -> None:
        """ansible-core >= 2.19 registers rc 0 for a command task skipped BY
        check mode, so a probe without `check_mode: false` makes `--check`
        silently predict 'no change' even for a real credential rotation.
        The probes are read-only (generate writes to stdout only)."""
        assert _task(name)["check_mode"] is False

    def test_verify_task_hides_the_password(self) -> None:
        task = _task(_VERIFY)
        assert task["no_log"] is True, "the verify loop items carry real passwords"
        assert task["loop_control"]["label"] == "{{ item.username }}"

    def test_names_probe_is_read_only_and_tolerates_a_missing_file(self) -> None:
        task = _task(_NAMES)
        assert task["changed_when"] is False
        assert task["failed_when"] is False
        cmd = _cmd(task)
        assert "{{ zot_config_dir }}/htpasswd" in cmd
        assert "password" not in cmd and "item" not in cmd


class TestHtpasswdDecision:
    _USERS = [_ADMIN, {"username": "dev", "password": "pw"}]

    def test_all_verified_and_same_names_keeps_the_file(self) -> None:
        assert _decide([0, 0], ["bay", "dev"], self._USERS) is False

    def test_order_of_lines_does_not_matter(self) -> None:
        assert _decide([0, 0], ["dev", "bay"], self._USERS) is False

    def test_blank_lines_are_ignored(self) -> None:
        assert _decide([0, 0], ["bay", "", "dev"], self._USERS) is False

    @pytest.mark.parametrize("rc", [1, 2, 3, 4, 5, 6, 7, 32])
    def test_any_non_zero_verify_rc_regenerates(self, rc: int) -> None:
        """rc 1 (missing file), 3 (password changed), 6 (user not found),
        7 (malformed) and anything else non-zero must rewrite."""
        assert _decide([0, rc], ["bay", "dev"], self._USERS) is True
        assert _decide([rc, 0], ["bay", "dev"], self._USERS) is True

    def test_an_extra_line_regenerates(self) -> None:
        """Verify cannot see an unwanted line; the names probe must."""
        assert _decide([0, 0], ["bay", "dev", "intruder"], self._USERS) is True

    def test_a_removed_pull_user_regenerates(self) -> None:
        assert _decide([0], ["bay", "dev"], [_ADMIN]) is True

    def test_a_duplicate_line_regenerates(self) -> None:
        assert _decide([0, 0], ["bay", "dev", "dev"], self._USERS) is True

    def test_missing_file_regenerates(self) -> None:
        assert _decide([1, 1], [], self._USERS) is True


class TestHtpasswdWriteIsGated:
    def test_write_task_is_gated_on_the_decision(self) -> None:
        """THE regression guard. Unconditional write + bcrypt re-salting =
        a registry restart on every single deploy."""
        when = _task(_WRITE).get("when")
        assert when is not None, (
            "'Write htpasswd file' must be gated on the verify result — an "
            "unconditional copy always reports changed because bcrypt "
            "re-salts, which fires 'Restart zot' on every deploy."
        )
        assert "_zot_htpasswd_regenerate" in when

    def test_decision_consumes_both_probes(self) -> None:
        expression = _task(_DECIDE)["ansible.builtin.set_fact"]["_zot_htpasswd_regenerate"]
        assert _task(_VERIFY)["register"] in expression
        assert _task(_NAMES)["register"] in expression

    def test_generate_task_is_gated_too(self) -> None:
        """Not correctness-critical (it only writes to stdout) but it keeps
        a bcrypt hash out of the play's memory on the steady-state path."""
        when = _task(_GENERATE).get("when")
        assert when is not None
        assert "_zot_htpasswd_regenerate" in when

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
        task = _task(_GENERATE)
        cmd = _cmd(task)
        assert "-iBn" in cmd or ("-B" in cmd and "-n" in cmd)
        assert "password" not in cmd
        assert task["ansible.builtin.command"]["stdin"] == "{{ item.password }}"
        assert task["loop"] == "{{ _zot_htpasswd_users }}"
        assert task["loop_control"]["label"] == "{{ item.username }}"
        assert task["no_log"] is True

    def test_restart_handler_still_exists(self) -> None:
        """The gate is only meaningful if the handler it protects is real."""
        handlers = yaml.safe_load(_ZOT_HANDLERS.read_text())
        assert any(h.get("name") == "Restart zot" for h in handlers)

    def test_apache2_utils_install_precedes_the_probes(self) -> None:
        """htpasswd comes from apache2-utils, installed earlier in this same
        file. Moving the verify above it would break a fresh host."""
        names = [t.get("name") for t in _tasks()]
        install = names.index("Install apache2-utils for htpasswd")
        assert install < names.index(_VERIFY)
        assert install < names.index(_GENERATE)

    def test_task_order(self) -> None:
        names = [t.get("name") for t in _tasks()]
        order = [names.index(n) for n in (_COLLECT, _NAMES, _VERIFY, _DECIDE, _GENERATE, _WRITE)]
        assert order == sorted(order)


# ── B. Behavioural simulation against the real htpasswd ─────────────


@_needs_htpasswd
class TestHtpasswdBehaviour:
    """Run the role's own command strings and expressions against apache2-utils."""

    @staticmethod
    def _run(task: dict, **ctx) -> subprocess.CompletedProcess:
        return subprocess.run(
            shlex.split(_render(_cmd(task), **ctx)),
            input=_stdin(task, **ctx),
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_role(
        self,
        config_dir: Path,
        username: str,
        password: str,
        pull_users: list[dict] | None = None,
    ) -> bool:
        """Execute the role's htpasswd tasks for real.

        Returns True when the file was (re)written, i.e. when the deploy
        would fire `Restart zot`. Every command string, the decision and the
        written content come from the role file itself.
        """
        creds = {"domain": "registry.example.com", "username": username, "password": password}
        base = {"zot_config_dir": str(config_dir)}
        users = _expr(
            _task(_COLLECT)["ansible.builtin.set_fact"]["_zot_htpasswd_users"],
            _zot_creds=creds,
            zot_pull_users=pull_users or [],
        )
        # Ansible strips the trailing newline of stdout; stdout_lines splits it.
        names = self._run(_task(_NAMES), **base).stdout.rstrip("\r\n").splitlines()
        rcs = [self._run(_task(_VERIFY), item=u, **base).returncode for u in users]
        if not _decide(rcs, names, users):
            return False
        results = []
        for user in users:
            generated = self._run(_task(_GENERATE), item=user, **base)
            assert generated.returncode == 0, generated.stderr
            results.append({"stdout": generated.stdout.rstrip("\r\n")})
        content = _render(
            _task(_WRITE)["ansible.builtin.copy"]["content"],
            _zot_htpasswd={"results": results},
        )
        target = config_dir / "htpasswd"
        target.write_text(content)
        target.chmod(0o600)
        return True

    def test_bcrypt_regenerates_a_different_hash_every_time(self) -> None:
        """The root cause, pinned: this is why content comparison can never
        settle and why a verify step is required."""
        user = {"username": "bay", "password": "hunter2"}
        first = self._run(_task(_GENERATE), item=user)
        second = self._run(_task(_GENERATE), item=user)
        assert first.returncode == second.returncode == 0
        assert first.stdout != second.stdout, (
            "htpasswd -iB is expected to emit a fresh random salt per run; "
            "if this ever stops being true the gate is still correct, but the "
            "premise of this test file has changed"
        )

    def test_fresh_host_creates_the_file(self, tmp_path: Path) -> None:
        assert self._run_role(tmp_path, "bay", "hunter2") is True
        written = (tmp_path / "htpasswd").read_text()
        assert written.startswith("bay:$2")
        assert written.endswith("\n")
        assert written.count("\n") == 1

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

    # ── pull-only accounts ──

    _PULL = [
        {"username": "dev-laptop", "password": "pull-1"},
        {"username": "ci-reader", "password": "pull-2"},
    ]

    def test_pull_users_get_one_line_each(self, tmp_path: Path) -> None:
        assert self._run_role(tmp_path, "bay", "hunter2", self._PULL) is True
        lines = (tmp_path / "htpasswd").read_text().splitlines()
        assert [line.split(":")[0] for line in lines] == ["bay", "dev-laptop", "ci-reader"]
        assert all(line.split(":", 1)[1].startswith("$2") for line in lines)

    def test_pull_users_second_run_is_a_no_op(self, tmp_path: Path) -> None:
        self._run_role(tmp_path, "bay", "hunter2", self._PULL)
        before = (tmp_path / "htpasswd").read_bytes()
        assert self._run_role(tmp_path, "bay", "hunter2", self._PULL) is False
        assert (tmp_path / "htpasswd").read_bytes() == before

    def test_pull_user_password_change_rewrites(self, tmp_path: Path) -> None:
        self._run_role(tmp_path, "bay", "hunter2", self._PULL)
        rotated = [self._PULL[0], {"username": "ci-reader", "password": "rotated"}]
        assert self._run_role(tmp_path, "bay", "hunter2", rotated) is True
        assert self._run_role(tmp_path, "bay", "hunter2", rotated) is False

    def test_removed_pull_user_disappears(self, tmp_path: Path) -> None:
        """Every remaining account still verifies, so only the names probe
        can catch this. Without it the removed account keeps pulling."""
        self._run_role(tmp_path, "bay", "hunter2", self._PULL)
        assert self._run_role(tmp_path, "bay", "hunter2", self._PULL[:1]) is True
        names = [line.split(":")[0] for line in (tmp_path / "htpasswd").read_text().splitlines()]
        assert names == ["bay", "dev-laptop"]

    def test_unknown_extra_line_is_removed(self, tmp_path: Path) -> None:
        self._run_role(tmp_path, "bay", "hunter2", self._PULL)
        target = tmp_path / "htpasswd"
        stray = subprocess.run(
            ["htpasswd", "-iBn", "intruder"], input="pw\n",
            capture_output=True, text=True, check=True,
        ).stdout.rstrip("\r\n")
        target.write_text(target.read_text() + stray + "\n")
        assert self._run_role(tmp_path, "bay", "hunter2", self._PULL) is True
        assert "intruder" not in target.read_text()
        assert self._run_role(tmp_path, "bay", "hunter2", self._PULL) is False

    def test_dropping_all_pull_users_leaves_the_admin_only(self, tmp_path: Path) -> None:
        self._run_role(tmp_path, "bay", "hunter2", self._PULL)
        assert self._run_role(tmp_path, "bay", "hunter2", []) is True
        assert (tmp_path / "htpasswd").read_text().count("\n") == 1
        assert self._run_role(tmp_path, "bay", "hunter2", []) is False
