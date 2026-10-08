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
from .images import commit_from_labels, commit_from_tags, resolved_image
from .planner import describe, plan


def reconcile(
    bundle: Bundle, client: DockerClient, *, plan_only: bool = False
) -> tuple[int, dict[str, object]]:
    """Observe -> plan -> (execute). Returns (exit_code, json-able report)."""
    observed = client.observe(bundle.managed_label)
    the_plan = plan(bundle.containers, observed, remove_orphans=bundle.remove_orphans)

    if plan_only:
        # `containers` is what `bay plan --remote` turns into steps: one entry
        # per container, {name, action, reasons}. Reasons name keys, never an
        # env value (planner.describe).
        return 0, {
            "ok": True,
            "plan_only": True,
            "plan": the_plan.summary(),
            "actions": [type(a).__name__ for a in the_plan.actions],
            "containers": describe(the_plan, bundle.containers, observed),
        }

    report = execute(the_plan, client, config=bundle.config)
    return (0 if report.ok else 1), {
        "plan": the_plan.summary(),
        **report.to_dict(),
        "state": _state_after(bundle, client),
    }


def _state_after(bundle: Bundle, client: DockerClient) -> dict[str, dict[str, str | None]]:
    """Status, health, commit and image of every desired container once the pass is done.

    One more batched observe, read only. The deploy receipt
    (``bay_reconcile.receipt``) turns it into each container's ``healthy``,
    ``commit``, ``commit_source`` and ``image`` fields. ``commit`` comes from
    the container's labels (``images.commit_from_labels``, source ``label``).
    A container with no commit label falls back to the repo tags of the image
    it runs: exactly one commit tag (12 hex characters) names its commit
    (``images.commit_from_tags``, source ``tag``); none or several name none.
    ``image`` is ``<repo>:<commit12>`` when that tag resolves to the image the
    container runs, else the spec's reference. A failed read is reported as
    no state, never as a failed deploy: the containers are already in place
    by now.
    """
    try:
        observed = client.observe(bundle.managed_label)
    except Exception:  # noqa: BLE001 - a status read must not fail the deploy
        return {}
    lookup = getattr(client, "image_id", None)
    tags_of = getattr(client, "image_tags", None)
    out: dict[str, dict[str, str | None]] = {}
    for spec in bundle.containers:
        current = observed.get(spec.name)
        if current is None or not current.exists:
            continue
        commit = commit_from_labels(current.labels)
        source: str | None = "label" if commit else None
        image = resolved_image(spec.image, commit, current.image_id, lookup)
        if commit is None and tags_of is not None and current.image_id:
            try:
                tagged = commit_from_tags(tags_of(current.image_id), prefer=spec.image)
            except Exception:  # noqa: BLE001 - a status read never fails the deploy
                tagged = None
            if tagged is not None:
                # The tag is on the very image the container runs.
                commit, image = tagged
                source = "tag"
        out[spec.name] = {
            "status": current.status,
            "health": current.health,
            "commit": commit,
            "commit_source": source,
            "image": image,
        }
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    plan_only = "--plan-only" in args
    positionals = [a for a in args if not a.startswith("--")]
    if not positionals:
        usage = "usage: bay_reconcile <bundle.json> [--plan-only]"
        print(json.dumps({"ok": False, "error": usage}))
        return 2

    # Loading validates as well as parses (container types, healthcheck
    # durations). A bad bundle must fail the whole run right here: no client is
    # built yet, so nothing has been observed, planned, removed or created.
    try:
        with open(positionals[0], encoding="utf-8") as fh:
            bundle = load_bundle(json.load(fh))
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": f"invalid bundle: {exc}"}))
        return 2

    # Local import: the docker SDK is only needed at run time, on the host.
    from .sdk_client import SdkDockerClient

    client = SdkDockerClient(managed_label=bundle.managed_label, stack=bundle.stack)
    code, out = reconcile(bundle, client, plan_only=plan_only)
    print(json.dumps(out))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
