"""The webhook receiver's SERVICE_BRANCHES must not depend on services.yml order.

The value is part of the container spec, so it feeds the config hash. Listed
in file order, moving a service in services.yml would recreate the webhook
receiver for no reason. build_specs.yml sorts the pairs by service name.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ansible_template = pytest.importorskip("ansible.template")
from ansible.parsing.dataloader import DataLoader  # noqa: E402

_BUILD_SPECS = (
    Path(__file__).resolve().parent.parent
    / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
)


def _service_branches(build_services: list[dict[str, object]]) -> str:
    tasks = yaml.safe_load(_BUILD_SPECS.read_text())
    task = next(t for t in tasks if t.get("name") == "Build webhook receiver container spec")
    variables = {
        "_is_build_server_for_webhook": False,
        "_has_global_remote_builds": False,
        "_build_services": build_services,
        "services": {},
    }
    templar = ansible_template.Templar(loader=DataLoader(), variables=variables)
    for name in ("_all_build_services_for_webhook", "_webhook_build_services", "_service_branches"):
        templar.available_variables = variables
        variables[name] = templar.template(ansible_template.trust_as_template(task["vars"][name]))
    return str(variables["_service_branches"])


def test_service_branches_are_sorted_by_service_name() -> None:
    zeta = {"key": "zeta", "value": {"build": {"branch": "dev"}}}
    alpha = {"key": "alpha", "value": {"build": {}}}
    mid = {"key": "mid", "value": {"build": {"branch": "release"}}}
    expected = "alpha:main,mid:release,zeta:dev"
    assert _service_branches([zeta, alpha, mid]) == expected
    assert _service_branches([alpha, mid, zeta]) == expected
