"""The plan-only report names every container, its action and why.

`bay plan --remote` turns `containers` ({name, action, reasons}) into plan
steps, so the reasons must say what differs, and must never carry an env
value: only key names.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bay_reconcile import ContainerSpec, ContainerState, load_bundle, plan  # noqa: E402
from bay_reconcile.__main__ import reconcile  # noqa: E402
from bay_reconcile.observe import parse_state  # noqa: E402
from bay_reconcile.planner import REASON_CODES, describe  # noqa: E402

SECRET = "value-that-must-never-be-printed"


def _spec(name: str = "web", *, config_hash: str = "h2", **kw: object) -> ContainerSpec:
    base: dict[str, object] = {
        "name": name,
        "image": f"{name}:latest",
        "type": "service",
        "config_hash": config_hash,
        "env": {"A": "1", "TOKEN": SECRET},
        "labels": {"traefik.enable": "true"},
    }
    base.update(kw)
    return ContainerSpec(**base)  # type: ignore[arg-type]


def _state(name: str = "web", *, config_hash: str | None = "h1", **kw: object) -> ContainerState:
    base: dict[str, object] = {
        "name": name,
        "exists": True,
        "image": f"{name}:latest",
        "config_hash": config_hash,
        "status": "running",
        "managed": True,
        "env": {"A": "1", "TOKEN": SECRET, "PATH": "/usr/bin"},
        "labels": {"traefik.enable": "true", "bay.managed": "true"},
        "volumes": (),
    }
    base.update(kw)
    return ContainerState(**base)  # type: ignore[arg-type]


def _one(spec: ContainerSpec, state: ContainerState | None) -> dict[str, object]:
    observed = {} if state is None else {state.name: state}
    out = describe(plan([spec], observed), [spec], observed)
    assert len(out) == 1
    return out[0]


def _codes(entry: dict[str, object]) -> set[str]:
    return {str(r).split(":", 1)[0] for r in entry["reasons"]}  # type: ignore[union-attr]


def test_missing_container_is_a_create() -> None:
    entry = _one(_spec(), None)
    assert entry["action"] == "create" and _codes(entry) == {"missing"}


def test_matching_container_is_a_noop_with_no_reason() -> None:
    entry = _one(_spec(config_hash="h1"), _state())
    assert entry == {"name": "web", "action": "noop", "reasons": []}


def test_stopped_noop_says_it_stays_stopped() -> None:
    entry = _one(_spec(config_hash="h1"), _state(status="exited"))
    assert entry["action"] == "noop" and _codes(entry) == {"stopped"}


def test_env_value_change_names_the_key_never_the_value() -> None:
    state = _state(env={"A": "1", "TOKEN": "old-" + SECRET})
    entry = _one(_spec(), state)
    assert entry["action"] == "recreate"
    assert _codes(entry) == {"config_hash", "env"}
    assert any("TOKEN" in r for r in entry["reasons"])  # type: ignore[union-attr]
    assert SECRET not in json.dumps(entry)


def test_hash_change_with_nothing_visible_is_config_hash_alone() -> None:
    # The env file bytes are not on the box, so env_order cannot be claimed.
    entry = _one(_spec(), _state())
    assert entry["action"] == "recreate"
    assert _codes(entry) == {"config_hash"}
    assert "not readable from docker inspect" in str(entry["reasons"])


_G = 1024**3
_M = 1024**2


def test_swap_cap_added_is_a_memory_reason() -> None:
    # Running: mem_limit 512m, no memswap_limit (docker doubles it). Wanted: both 512m.
    spec = _spec(mem_limit="512m", memswap_limit="512m")
    entry = _one(spec, _state(memory=512 * _M, memory_swap=1024 * _M))
    assert entry["action"] == "recreate"
    assert _codes(entry) == {"config_hash", "memory"}
    assert any("memswap_limit 1g -> 512m" in r for r in entry["reasons"])  # type: ignore[union-attr]
    assert not any("mem_limit" in r for r in entry["reasons"] if r.startswith("memory"))  # type: ignore[union-attr]


def test_memory_cap_change_is_a_memory_reason() -> None:
    spec = _spec(mem_limit="1g", memswap_limit="1g")
    entry = _one(spec, _state(memory=512 * _M, memory_swap=512 * _M))
    assert _codes(entry) == {"config_hash", "memory"}
    assert any("mem_limit 512m -> 1g" in r for r in entry["reasons"])  # type: ignore[union-attr]


def test_matching_memory_is_not_named() -> None:
    spec = _spec(mem_limit="512m", memswap_limit="512m")
    entry = _one(spec, _state(memory=512 * _M, memory_swap=512 * _M))
    assert _codes(entry) == {"config_hash"}
    # With no memswap_limit wanted, docker's doubled swap cap is a match.
    spec = _spec(mem_limit="512m")
    entry = _one(spec, _state(memory=512 * _M, memory_swap=1024 * _M))
    assert _codes(entry) == {"config_hash"}


def test_unreported_memory_is_not_named() -> None:
    entry = _one(_spec(mem_limit="512m", memswap_limit="512m"), _state())
    assert "memory" not in _codes(entry)


def test_parse_state_reads_the_memory_caps() -> None:
    state = parse_state(
        {"Name": "/web", "Config": {}, "State": {}, "HostConfig": {"Memory": 5, "MemorySwap": 10}},
        managed_label="bay.managed",
    )
    assert (state.memory, state.memory_swap) == (5, 10)


def test_image_label_port_and_volume_changes_are_named() -> None:
    state = _state(
        image="web:old",
        labels={},
        port_bindings=("0.0.0.0:80",),
        volumes=("data:/data",),
    )
    entry = _one(_spec(), state)
    assert _codes(entry) == {"config_hash", "image", "labels", "ports", "volumes"}


def test_image_drift_under_the_same_hash() -> None:
    state = _state(config_hash="h2", image_id="sha256:aaa", local_image_id="sha256:bbb")
    entry = _one(_spec(), state)
    assert entry["action"] == "recreate" and _codes(entry) == {"image"}


def test_no_hash_label_is_named() -> None:
    entry = _one(_spec(), _state(config_hash=None))
    assert "config_hash" in _codes(entry)


def test_canary_swap_is_a_recreate() -> None:
    entry = _one(_spec(zero_downtime=True), _state())
    assert entry["action"] == "recreate" and "zero_downtime" in _codes(entry)


def test_orphan_is_a_remove() -> None:
    observed = {"old": _state("old")}
    out = describe(plan([], observed, remove_orphans=True), [], observed)
    assert out == [{"name": "old", "action": "remove", "reasons": [out[0]["reasons"][0]]}]
    assert _codes(out[0]) == {"orphan"}


def test_env_file_reordered_is_named_as_order() -> None:
    change = {"live": True, "added": [], "removed": [], "changed": [], "reordered": True}
    entry = _one(_spec(env_file_change=change), _state())
    assert entry["action"] == "recreate"
    assert "env_file: the env file bytes differ (same variables, different order)" in (
        entry["reasons"]  # type: ignore[operator]
    )
    # A named cause: no "not readable from docker inspect" guess any more.
    assert not any("not readable" in str(r) for r in entry["reasons"])  # type: ignore[union-attr]


def test_env_file_change_names_keys_never_values() -> None:
    change = {
        "live": True,
        "added": ["NEW_KEY"],
        "removed": ["OLD_KEY"],
        "changed": ["TOKEN"],
        "reordered": False,
    }
    entry = _one(_spec(env_file_change=change), _state())
    (reason,) = [r for r in entry["reasons"] if str(r).startswith("env_file:")]  # type: ignore[union-attr]
    assert "NEW_KEY" in reason and "OLD_KEY" in reason and "TOKEN" in reason
    assert "different order" not in reason and SECRET not in json.dumps(entry)


def test_env_file_without_a_live_copy_and_comment_only_changes() -> None:
    new = _one(_spec(env_file_change={"live": False}), _state())
    assert "env_file: the box has no env file for it yet" in new["reasons"]  # type: ignore[operator]
    same = {"live": True, "added": [], "removed": [], "changed": [], "reordered": False}
    entry = _one(_spec(env_file_change=same), _state())
    assert any("only comments or blank lines differ" in str(r) for r in entry["reasons"])  # type: ignore[union-attr]


def test_bundle_carries_the_env_file_change() -> None:
    change = {"live": True, "added": [], "removed": [], "changed": [], "reordered": True}
    bundle = load_bundle(
        {
            "containers": [
                {"name": "a", "image": "a:1", "type": "service", "config_hash": "h",
                 "env_file_change": change},
                {"name": "b", "image": "b:1", "type": "service", "config_hash": "h",
                 "env_file_change": None},
            ]
        }
    )
    assert bundle.containers[0].env_file_change == change
    assert bundle.containers[1].env_file_change is None


def test_every_reason_code_is_known() -> None:
    entries = [
        _one(_spec(), None),
        _one(_spec(), _state(image="x", labels={}, port_bindings=("0.0.0.0:1",))),
        _one(_spec(), _state()),
        _one(_spec(config_hash="h1"), _state(status="exited")),
        _one(_spec(env_file_change={"live": False}), _state()),
    ]
    codes = set().union(*(_codes(e) for e in entries))
    assert codes <= set(REASON_CODES) | {"zero_downtime"}


def test_plan_only_report_lists_every_container() -> None:
    bundle = load_bundle(
        {
            "remove_orphans": True,
            "containers": [
                {"name": "web", "image": "web:latest", "type": "service", "config_hash": "h2"},
                {"name": "new", "image": "new:latest", "type": "service", "config_hash": "h1"},
            ],
        }
    )

    class Client:
        def observe(self, managed_label: str) -> dict[str, ContainerState]:
            return {"web": _state(env={}), "gone": _state("gone")}

    code, out = reconcile(bundle, Client(), plan_only=True)  # type: ignore[arg-type]
    assert code == 0
    by_name = {c["name"]: c for c in out["containers"]}  # type: ignore[union-attr]
    assert {n: c["action"] for n, c in by_name.items()} == {
        "web": "recreate",
        "new": "create",
        "gone": "remove",
    }
    assert out["actions"] and out["plan"]
    json.dumps(out)  # plain JSON for the CLI


def test_parse_state_reads_env_labels_and_binds() -> None:
    state = parse_state(
        {
            "Name": "/web",
            "Config": {
                "Image": "web:latest",
                "Env": ["A=1", "B=x=y"],
                "Labels": {"bay.managed": "true"},
            },
            "HostConfig": {"Binds": ["data:/data", "/etc/x:/x:ro"]},
            "State": {"Status": "running"},
        },
        managed_label="bay.managed",
    )
    assert state.env == {"A": "1", "B": "x=y"}
    assert state.labels == {"bay.managed": "true"}
    assert state.volumes == ("/etc/x:/x:ro", "data:/data")
    assert "x=y" not in repr(state)  # env stays out of repr


def test_parse_state_without_env_reports_none() -> None:
    state = parse_state({"Name": "/web", "Config": {}}, managed_label="bay.managed")
    assert state.env is None and state.volumes is None


def _walk(tasks: list[dict[str, object]]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for task in tasks:
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(_walk(task.get(key) or []))  # type: ignore[arg-type]
    return out


def test_check_mode_plan_is_handed_to_the_cli() -> None:
    import yaml

    path = Path(__file__).parent.parent / "roles/container_lifecycle/tasks/reconcile.yml"
    tasks = _walk(yaml.safe_load(path.read_text()))
    task = next(t for t in tasks if t.get("name") == "Hand the check-mode plan to the CLI")
    when = " ".join(task["when"])  # type: ignore[arg-type]
    assert "bay_reconciler_plan_report_dir is defined" in when
    assert "_reconcile_check_result is not skipped" in when
    assert task["delegate_to"] == "localhost" and task["check_mode"] is False
    copy = task["ansible.builtin.copy"]
    assert "_reconcile_check_result.stdout" in copy["content"]  # type: ignore[index]
    assert copy["dest"].startswith("{{ bay_reconciler_plan_report_dir }}/")  # type: ignore[index]
