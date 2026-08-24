"""``python -m bay_reconcile <bundle.json> [--plan-only]`` — server entrypoint.

Reads a desired-state bundle, observes the running fleet in one batched call,
plans the diff, and (unless --plan-only) executes it. Prints a single JSON
report to stdout (observability contract) and exits non-zero if any action
failed. The whole package is stdlib + docker SDK, so the CLI ships it to the
host and runs this in one pass — no per-task round-trips.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from .bundle import Bundle, load_bundle
from .docker_client import DockerClient
from .executor import execute
from .planner import plan


def reconcile(
    bundle: Bundle, client: DockerClient, *, plan_only: bool = False
) -> tuple[int, dict[str, object]]:
    """Observe -> plan -> (execute). Returns (exit_code, json-able report)."""
    observed = client.observe(bundle.managed_label)
    the_plan = plan(bundle.containers, observed, remove_orphans=bundle.remove_orphans)

    if plan_only:
        return 0, {
            "ok": True,
            "plan_only": True,
            "plan": the_plan.summary(),
            "actions": [type(a).__name__ for a in the_plan.actions],
        }

    report = execute(the_plan, client)
    return (0 if report.ok else 1), {"plan": the_plan.summary(), **report.to_dict()}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    plan_only = "--plan-only" in args
    positionals = [a for a in args if not a.startswith("--")]
    if not positionals:
        usage = "usage: bay_reconcile <bundle.json> [--plan-only]"
        print(json.dumps({"ok": False, "error": usage}))
        return 2

    with open(positionals[0], encoding="utf-8") as fh:
        bundle = load_bundle(json.load(fh))

    # Local import: the docker SDK is only needed at run time, on the host.
    from .sdk_client import SdkDockerClient

    client = SdkDockerClient(managed_label=bundle.managed_label, stack=bundle.stack)
    code, out = reconcile(bundle, client, plan_only=plan_only)
    print(json.dumps(out))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
