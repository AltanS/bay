"""M115/S06: the deploy receipt, `bay status --json`, and the missing-secret check.

The receipt is written by `bay_reconcile.receipt` on the box and read back by
`bay_cli.receipts` through an Ansible ad-hoc `cat`. The tests run both halves
against a temp directory, with a fake runner standing in for SSH: it answers
the `cat` the way the `ansible.posix.json` callback does.
"""

from __future__ import annotations

import io
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from bay_cli import receipts, secrets_check
from bay_cli.cli import app
from bay_cli.context import Context
from bay_reconcile import receipt as box_receipt
from bay_reconcile.__main__ import reconcile
from bay_reconcile.bundle import load_bundle
from bay_reconcile.models import ContainerState

_REPO = Path(__file__).resolve().parent.parent
_SCHEMA = json.loads((_REPO / "src/bay_cli/schemas/status.schema.json").read_text())
_RECEIPT_SCHEMA = {"$defs": _SCHEMA["$defs"], "$ref": "#/$defs/receipt"}
_RECONCILE_TASKS = _REPO / "roles/container_lifecycle/tasks/reconcile.yml"


# ── helpers ──────────────────────────────────────────────────────────────


def _fleet(tmp_path: Path, envs: tuple[str, ...] = ("production",)) -> Context:
    root = tmp_path / "fleet"
    (root / "hosts").mkdir(parents=True)
    for env in envs:
        (root / "hosts" / env).write_text(f"[{env}]\napp-1\n")
    (root / "group_vars" / "all").mkdir(parents=True)
    return Context.for_fleet_root(root, _REPO)


_BUNDLE = {
    "stack": "bay",
    "containers": [
        {
            "name": "web",
            "image": "acme/web:1",
            "type": "service",
            "config_hash": "h-web",
            "env": {"SESSION_SECRET": "must-never-reach-a-receipt"},
        },
        {"name": "postgres", "image": "postgres:16", "type": "accessory", "config_hash": "h-pg"},
    ],
}

_REPORT = {
    "plan": {"Recreate": 1, "NoOp": 1, "Remove": 1},
    "ok": True,
    "changed": True,
    "results": [
        {"kind": "NoOp", "name": "postgres", "status": "skipped", "detail": ""},
        {"kind": "CanarySwap", "name": "web", "status": "done", "detail": ""},
        {"kind": "Remove", "name": "old-worker", "status": "done", "detail": ""},
    ],
    "state": {
        "web": {"status": "running", "health": "healthy"},
        "postgres": {"status": "running", "health": None},
    },
}

_META = {
    "env": "production",
    "box": "app-1",
    "framework_version": "2.0.0",
    "framework_commit": "f" * 40,
    "fleet_commit": "a" * 40,
    "fleet_dirty": False,
    "reconcile_rc": 0,
}


def _fake_ssh(box_dir: Path, *, boxes: dict[str, str] | None = None, calls: list | None = None):
    """A runner that answers `ansible <env> -m command -a "cat <path>"` from ``box_dir``.

    ``boxes`` maps box name -> "local" (read ``box_dir``) or "unreachable".
    """
    boxes = boxes or {"app-1": "local"}

    def run(
        argv: list[str], extra_env: dict[str, str], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append((argv, extra_env, cwd))
        cmd = shlex.split(argv[argv.index("-a") + 1])
        assert cmd[0] == "cat" and len(cmd) == 2, "exactly one cat per box"
        remote = Path(cmd[1])
        local = box_dir / remote.relative_to(box_receipt.RECEIPTS_DIR)
        hosts: dict[str, Any] = {}
        for name, mode in boxes.items():
            if mode == "unreachable":
                hosts[name] = {"unreachable": True, "msg": "ssh: connect to host timed out"}
            elif local.is_file():
                hosts[name] = {"rc": 0, "stdout": local.read_text().rstrip("\n"), "stderr": ""}
            else:
                hosts[name] = {
                    "rc": 1,
                    "failed": True,
                    "stdout": "",
                    "stderr": f"cat: {remote}: No such file or directory",
                }
        doc = {
            "plays": [
                {
                    "play": {"name": "Ansible Ad-Hoc"},
                    "tasks": [{"task": {"name": "ansible.builtin.command"}, "hosts": hosts}],
                }
            ],
            "stats": {},
        }
        return subprocess.CompletedProcess(argv, 2, stdout=json.dumps(doc, indent=4), stderr="")

    return run


# ── receipt ──────────────────────────────────────────────────────────────


def test_deploy_receipt_roundtrip(tmp_path: Path) -> None:
    box_dir = tmp_path / "box"
    written = box_receipt.build_receipt(meta=_META, bundle=_BUNDLE, report=_REPORT)
    box_receipt.write_receipt(written, box_receipt.receipt_path("production", box_dir))

    jsonschema.validate(written, _RECEIPT_SCHEMA)
    assert written["result"] == "ok"
    by_name = {c["name"]: c for c in written["containers"]}
    assert by_name["web"] == {
        "name": "web",
        "image": "acme/web:1",
        "config_hash": "h-web",
        "action": "recreate",
        "healthy": True,
    }
    assert by_name["postgres"]["action"] == "noop" and by_name["postgres"]["healthy"] is None
    assert by_name["old-worker"]["action"] == "remove" and by_name["old-worker"]["image"] is None
    assert written["projects"] == {}
    assert "must-never-reach-a-receipt" not in json.dumps(written)

    cx = _fleet(tmp_path)
    calls: list = []
    doc = receipts.status_document(cx, env="production", run=_fake_ssh(box_dir, calls=calls))

    assert doc["boxes"] == [
        {"env": "production", "box": "app-1", "receipt": written, "error": None}
    ]
    assert len(calls) == 1
    argv, extra_env, cwd = calls[0]
    assert argv[argv.index("ansible") + 1] == "production"
    assert "--become" not in argv
    assert any("BatchMode=yes" in a for a in argv)
    assert extra_env["ANSIBLE_STDOUT_CALLBACK"] == "ansible.posix.json"
    assert cwd == cx.fleet_root


def test_receipt_write_is_atomic_and_keeps_the_previous_one(tmp_path: Path) -> None:
    path = box_receipt.receipt_path("production", tmp_path)
    first = box_receipt.build_receipt(
        meta=_META, bundle=_BUNDLE, report=_REPORT, deployed_at="2026-10-06T10:00:00Z"
    )
    second = box_receipt.build_receipt(
        meta={**_META, "reconcile_rc": 1},
        bundle=_BUNDLE,
        report={**_REPORT, "ok": False},
        deployed_at="2026-10-06T11:00:00Z",
    )
    box_receipt.write_receipt(first, path)
    box_receipt.write_receipt(second, path)

    assert json.loads(path.read_text())["result"] == "failed"
    assert json.loads(box_receipt.previous_path(path).read_text()) == first
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["production.json", "production.prev.json"]


def test_receipt_after_a_crash_is_failed_with_unknown_actions() -> None:
    receipt = box_receipt.build_receipt(
        meta={**_META, "reconcile_rc": 1}, bundle=_BUNDLE, report=None
    )
    jsonschema.validate(receipt, _RECEIPT_SCHEMA)
    assert receipt["result"] == "failed"
    assert {c["action"] for c in receipt["containers"]} == {None}


def test_receipt_rejects_an_env_that_would_leave_the_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        box_receipt.receipt_path("../etc/passwd", tmp_path)


def test_receipt_main_reads_the_report_from_stdin(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(_BUNDLE))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_REPORT)))
    assert (
        box_receipt.main(
            ["--meta", json.dumps(_META), "--bundle", str(bundle), "--dir", str(tmp_path / "r")]
        )
        == 0
    )
    stored = json.loads((tmp_path / "r" / "production.json").read_text())
    assert stored["fleet_commit"] == "a" * 40 and stored["result"] == "ok"


def test_reconciler_report_carries_post_pass_state() -> None:
    class Fake:
        def __init__(self) -> None:
            self.state = {
                "web": ContainerState(
                    "web",
                    exists=True,
                    config_hash="h-web",
                    managed=True,
                    status="running",
                    health="healthy",
                )
            }

        def observe(self, managed_label):
            return dict(self.state)

    bundle = load_bundle(
        {
            "containers": [
                {"name": "web", "image": "acme/web:1", "type": "service", "config_hash": "h-web"}
            ]
        }
    )
    code, out = reconcile(bundle, Fake())
    assert code == 0
    assert out["state"] == {"web": {"status": "running", "health": "healthy"}}


# ── the Ansible wiring ───────────────────────────────────────────────────


_PASS_BLOCK = "Reconcile the containers and record the result"


def _top() -> list[dict]:
    return yaml.safe_load(_RECONCILE_TASKS.read_text())


def _tasks() -> list[dict]:
    """The pass block's children, in order: where every receipt task lives."""
    return next(t for t in _top() if t.get("name") == _PASS_BLOCK)["block"]


def _task(name: str) -> dict:
    for task in _tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(name)


def test_receipt_task_runs_after_the_pass_as_root_and_never_in_check_mode() -> None:
    names = [t.get("name") for t in _tasks()]
    block = _task("Write the deploy receipt")
    assert names.index("Write the deploy receipt") > names.index("Run server-side reconciler")
    assert block["become"] is True and block["become_user"] == "root"
    when = " ".join(block["when"])
    assert "_reconcile_result is not skipped" in when
    assert "rc is defined" not in when
    assert "container_lifecycle_only" in when and "bay_reconciler_plan_only" in when
    write = block["block"][1]["ansible.builtin.command"]["argv"]
    assert write[:3] == ["python3", "-m", "bay_reconcile.receipt"]
    assert "--dir" not in write, "the helper's RECEIPTS_DIR is the one definition"
    assert block["rescue"], "an unwritable receipt must warn, not fail a finished deploy"


def test_the_role_and_the_cli_use_one_receipts_path() -> None:
    mkdir = _task("Write the deploy receipt")["block"][0]["ansible.builtin.file"]
    assert mkdir["path"] == str(box_receipt.RECEIPTS_DIR) == "/var/lib/bay/receipts"
    assert "receipts_dir" not in (_REPO / "roles/container_lifecycle/defaults/main.yml").read_text()


def test_the_bundle_is_removed_on_every_path() -> None:
    """The bundle holds resolved secrets: its removal must sit in `always:`."""
    top = _top()
    names = [t.get("name") for t in top]
    assert "Remove reconcile bundle (contains resolved env)" not in names, "not a plain task"
    wrapper = top[names.index(_PASS_BLOCK)]
    assert wrapper["block"][0]["name"].startswith("Write reconcile bundle"), (
        "the block must open with the bundle write, so any later failure is covered"
    )
    always = wrapper["always"]
    assert always[0]["name"] == "Remove reconcile bundle (contains resolved env)"
    assert always[0]["ansible.builtin.file"] == {
        "path": "{{ stack_dir }}/.reconcile-bundle.json",
        "state": "absent",
    }
    inner = [t.get("name") for t in wrapper["block"]]
    # The deliberate failure is inside the block, so always: still runs after it.
    assert "Stop the deploy when the reconciler failed" in inner


def test_a_failed_pass_writes_the_receipt_then_still_fails_the_deploy() -> None:
    run = _task("Run server-side reconciler")
    assert run["failed_when"] is False
    names = [t.get("name") for t in _tasks()]
    stop = _task("Stop the deploy when the reconciler failed")
    assert names.index("Stop the deploy when the reconciler failed") == (
        names.index("Write the deploy receipt") + 1
    )
    assert "_reconcile_result.rc != 0" in stop["when"]
    assert "_reconcile_result is not skipped" in stop["when"]


# ── bay status --json ────────────────────────────────────────────────────


def test_status_json_schema(tmp_path: Path, monkeypatch) -> None:
    cx = _fleet(tmp_path, envs=("production", "staging"))
    box_dir = tmp_path / "box"
    box_receipt.write_receipt(
        box_receipt.build_receipt(meta=_META, bundle=_BUNDLE, report=_REPORT),
        box_receipt.receipt_path("production", box_dir),
    )
    fake = _fake_ssh(box_dir, boxes={"app-1": "local", "app-2": "unreachable"})
    monkeypatch.setattr(receipts, "_default_runner", fake)

    result = CliRunner().invoke(app, ["--fleet", str(cx.fleet_root), "status", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    jsonschema.validate(doc, _SCHEMA)

    assert doc["status_version"] == 1
    assert doc["fleet"]["root"] == str(cx.fleet_root)
    boxes = {(b["env"], b["box"]): b for b in doc["boxes"]}
    assert boxes[("production", "app-1")]["receipt"]["result"] == "ok"
    assert boxes[("production", "app-2")]["error"].startswith("unreachable")
    # staging has no receipt file: no receipt, and that is not an error
    assert boxes[("staging", "app-1")] == {
        "env": "staging",
        "box": "app-1",
        "receipt": None,
        "error": None,
    }


def test_status_json_env_and_no_remote(tmp_path: Path, monkeypatch) -> None:
    cx = _fleet(tmp_path, envs=("production", "staging"))
    calls: list = []
    monkeypatch.setattr(receipts, "_default_runner", _fake_ssh(tmp_path / "box", calls=calls))

    one = CliRunner().invoke(
        app, ["--fleet", str(cx.fleet_root), "status", "--json", "--env", "staging"]
    )
    assert one.exit_code == 0, one.output
    assert {b["env"] for b in json.loads(one.stdout)["boxes"]} == {"staging"}
    assert len(calls) == 1

    none = CliRunner().invoke(
        app, ["--fleet", str(cx.fleet_root), "status", "--json", "--no-remote"]
    )
    assert none.exit_code == 0, none.output
    doc = json.loads(none.stdout)
    jsonschema.validate(doc, _SCHEMA)
    assert doc["boxes"] == [] and len(calls) == 1


def test_status_json_survives_an_ansible_failure(tmp_path: Path) -> None:
    cx = _fleet(tmp_path)

    def broken(argv, extra_env, cwd):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ERROR! no inventory")

    doc = receipts.status_document(cx, run=broken)
    jsonschema.validate(doc, _SCHEMA)
    assert doc["boxes"][0]["box"] is None
    assert "no inventory" in doc["boxes"][0]["error"]


def test_deploy_extra_vars_are_typed_json(tmp_path: Path) -> None:
    cx = _fleet(tmp_path)  # not a git repo of its own? it sits in tmp, so no git
    flag, payload = receipts.deploy_extra_vars(cx)
    assert flag == "-e"
    data = json.loads(payload)
    assert set(data) == {
        "bay_receipt_fleet_commit",
        "bay_receipt_fleet_dirty",
        "bay_receipt_framework_commit",
    }
    assert data["bay_receipt_fleet_commit"] is None and data["bay_receipt_fleet_dirty"] is None


# ── missing-secret check ─────────────────────────────────────────────────

_SENTINEL = "sentinel_value_that_must_never_be_printed"

_SERVICES = {
    "services": {
        "my-app": {
            "image": "x:1",
            "env": {"secret": ["SESSION_SECRET", "API_KEY"]},
            "database": {"accessory": "postgres"},
            "middleware": {
                "basic_auth": {
                    "credentials": [
                        {"username": "ops", "password": "{{ secrets.MYAPP_BASIC_AUTH }}"}
                    ]
                }
            },
        },
        "builder": {
            "build": {
                "repo": "git@example.com:a/b.git",
                "secrets": {"npmrc": "{{ secrets.NPMRC }}"},
                "token": "{{ secrets.GIT_TOKEN }}",
            },
        },
    },
    "accessories": {
        "postgres": {
            "image": "postgres:16",
            "env": {"secret": {"POSTGRES_PASSWORD": "POSTGRES_PASSWORD"}},
        },
    },
}

# Present: MY_APP_SESSION_SECRET and POSTGRES_PASSWORD, both holding the sentinel.
# Missing: GIT_TOKEN, MY_APP_API_KEY, MY_APP_POSTGRES_PASSWORD, MYAPP_BASIC_AUTH, NPMRC.
_VAULT = {"secrets": {"MY_APP_SESSION_SECRET": _SENTINEL, "POSTGRES_PASSWORD": _SENTINEL}}
_EXPECTED_MISSING = [
    "GIT_TOKEN",
    "MYAPP_BASIC_AUTH",
    "MY_APP_API_KEY",
    "MY_APP_POSTGRES_PASSWORD",
    "NPMRC",
]
# `bay validate` leaves build.token to _validate_build_tokens, so one missing
# name fails validation once.
_VALIDATE_MISSING = 4


def _secret_fleet(tmp_path: Path, *, password: bool = True) -> Context:
    cx = _fleet(tmp_path)
    cx.services_file.write_text(yaml.safe_dump(_SERVICES))
    (cx.group_vars / "production").mkdir()
    cx.secrets_file("production").write_text("$ANSIBLE_VAULT;1.1;AES256\n6162636465\n")
    if password:
        cx.vault_pass.write_text("not-the-sentinel\n")
    return cx


def _fake_decrypt(monkeypatch) -> list:
    seen: list = []

    def run(argv, **kwargs):
        seen.append(argv)
        assert argv[:2] == ["ansible-vault", "decrypt"] and "--output=-" in argv
        return subprocess.CompletedProcess(argv, 0, stdout=yaml.safe_dump(_VAULT), stderr="")

    monkeypatch.setattr(secrets_check.subprocess, "run", run)
    return seen


def test_missing_secret_check_prints_names_only(tmp_path: Path, monkeypatch, capsys) -> None:
    cx = _secret_fleet(tmp_path)
    seen = _fake_decrypt(monkeypatch)

    found = secrets_check.missing_secrets(cx, "production")
    assert [m.name for m in found] == _EXPECTED_MISSING
    assert _SENTINEL not in repr(found)
    assert all(_SENTINEL not in a for argv in seen for a in argv), "never on argv"

    human = CliRunner().invoke(
        app, ["--fleet", str(cx.fleet_root), "secret", "missing", "production"]
    )
    as_json = CliRunner().invoke(
        app, ["--fleet", str(cx.fleet_root), "secret", "missing", "production", "--json"]
    )
    assert human.exit_code == 1 and as_json.exit_code == 1
    for result in (human, as_json):
        assert _SENTINEL not in result.stdout
        assert _SENTINEL not in result.stderr
        assert _SENTINEL not in result.output
    for name in _EXPECTED_MISSING:
        assert name in human.stdout
    doc = json.loads(as_json.stdout)
    assert doc["checked"] is True
    assert [m["name"] for m in doc["missing"]] == _EXPECTED_MISSING
    assert _SENTINEL not in json.dumps(doc)

    from bay_cli.commands.validate import ValidationResult, _validate_secret_names

    result = ValidationResult()
    _validate_secret_names(cx.fleet_root, _REPO, "production", _SERVICES, {}, result)
    messages = " ".join(result.failed + result.passed + result.warnings)
    assert _SENTINEL not in messages
    assert len(result.failed) == _VALIDATE_MISSING
    assert "NPMRC" in messages and "MY_APP_API_KEY" in messages
    assert "GIT_TOKEN" not in messages
    captured = capsys.readouterr()
    assert _SENTINEL not in captured.out and _SENTINEL not in captured.err


def test_missing_secret_check_without_a_vault_password_warns(tmp_path: Path) -> None:
    cx = _secret_fleet(tmp_path, password=False)
    result = CliRunner().invoke(
        app, ["--fleet", str(cx.fleet_root), "secret", "missing", "production", "--json"]
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc == {
        "env": "production",
        "checked": False,
        "warning": "cannot check: no vault password",
        "missing": [],
    }

    from bay_cli.commands.validate import ValidationResult, _validate_secret_names

    vr = ValidationResult()
    _validate_secret_names(cx.fleet_root, _REPO, "production", _SERVICES, {}, vr)
    assert vr.failed == []
    assert any("cannot check: no vault password" in w for w in vr.warnings)


def test_validate_reuses_the_vault_the_yaml_pass_decrypted(tmp_path: Path, monkeypatch) -> None:
    cx = _secret_fleet(tmp_path)

    def no_decrypt(*a, **k):
        raise AssertionError("must not decrypt twice")

    monkeypatch.setattr(secrets_check.subprocess, "run", no_decrypt)
    from bay_cli.commands.validate import ValidationResult, _validate_secret_names

    vr = ValidationResult()
    _validate_secret_names(
        cx.fleet_root,
        _REPO,
        "production",
        _SERVICES,
        {"group_vars/production/secrets.yml": _VAULT},
        vr,
    )
    assert len(vr.failed) == _VALIDATE_MISSING


def test_a_yaml_error_in_the_vault_never_quotes_it(tmp_path: Path, monkeypatch) -> None:
    cx = _secret_fleet(tmp_path)

    def bad(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"secrets: [{_SENTINEL}\n  : :", stderr=""
        )

    monkeypatch.setattr(secrets_check.subprocess, "run", bad)
    with pytest.raises(secrets_check.SecretsUncheckable) as exc:
        secrets_check.vault_names(cx, "production")
    assert _SENTINEL not in str(exc.value)
    assert exc.value.__cause__ is None
