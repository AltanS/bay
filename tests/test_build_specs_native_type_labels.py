"""Guards against native-typed values in container `labels:`/`env:` (v0.3.1).

Context. `build_specs.yml` renders container specs that the Python reconciler
(`roles/container_lifecycle/library/reconcile.py`) feeds straight to the
Docker API. Ansible's native Jinja renders a pure `"{{ ... }}"` template as
its native Python type when the expression itself is not a string — so
`"{{ webhook_rate_limit_average | default(10) }}"` becomes the int `10`, not
the string `"10"`, whenever the operator has not overridden the var (the
common case).

Docker requires `Config.Labels` and `Config.Env` values to be strings. An int
label makes `docker create` fail outright:

    json: cannot unmarshal number into Go struct field
    CreateRequest.Config.Labels of type string

This shipped in v0.3.0: the webhook receiver's rate-limit labels
(`traefik.http.middlewares.bay-webhook-ratelimit.ratelimit.average`/`.burst`)
had numeric defaults (10/30) with no `| string` cast, so every deploy with
the webhook receiver enabled failed to (re)create the container. Fixed by
adding `| string`, matching the existing pattern used a few lines away
(`zot_port | default(5000) | string`, `WEBHOOK_PEER_TIMEOUT`).

This test parses `build_specs.yml` as YAML (no Jinja rendering needed — the
bug is visible in the raw template text) and walks every `labels:` and
`env:`/`environment:` mapping in every task, flagging any value that is a
*pure* template (nothing but `{{ ... }}`, no surrounding literal text) whose
expression ends in `default(<int|float|true|false>)` with no trailing
`| string` filter. A mapping value with literal text around the template
(e.g. `"Host(\\`{{ x }}\\`)"`) is always a string regardless of native
typing and is not flagged.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_BUILD_SPECS = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "container_lifecycle"
    / "tasks"
    / "build_specs.yml"
)

# A value that is ENTIRELY one Jinja expression, e.g. '{{ foo | default(10) }}'
# — no literal text before/after the braces. Ansible only native-types this
# shape; a template embedded in surrounding text always stays a string.
_PURE_TEMPLATE_RE = re.compile(r"^\{\{\s*(?P<expr>.*)\s*\}\}$")

# The expression's filter pipeline ends in default(<literal>) where <literal>
# is an int, float, or bool — the shapes Ansible native-types to a non-str.
_UNSAFE_DEFAULT_RE = re.compile(
    r"default\(\s*(?:-?\d+(?:\.\d+)?|true|false|True|False)\s*\)\s*$"
)


def _find_unsafe_values(mapping: dict, path: str) -> list[str]:
    """Return descriptions of unsafe pure-template values in a labels/env dict."""
    unsafe = []
    for key, value in mapping.items():
        if not isinstance(value, str):
            # Native YAML bool/int literal (e.g. `key: 1`) is just as broken,
            # but none exist today; flag it too if it ever shows up.
            unsafe.append(f"{path}.{key!r} = {value!r} (non-string literal)")
            continue
        m = _PURE_TEMPLATE_RE.match(value.strip())
        if not m:
            continue
        expr = m.group("expr").strip()
        if _UNSAFE_DEFAULT_RE.search(expr):
            unsafe.append(f"{path}.{key!r} = {value!r}")
    return unsafe


def _iter_labels_and_env_blocks(tasks: list[dict]):
    """Yield (path, mapping) for every literal labels:/env:/environment: block.

    Only walks blocks that are plain dicts in the parsed YAML (i.e. authored
    inline in build_specs.yml), not string expressions like
    `labels: "{{ _traefik_labels | combine(_wt_labels) }}"` — those are built
    by the Python filter plugins, which already `str()`-cast every value.
    """
    for task in tasks:
        task_name = task.get("name", "<unnamed task>")
        task_vars = task.get("vars") or {}
        for var_name, var_value in task_vars.items():
            if not isinstance(var_value, dict):
                continue
            for block_key in ("labels", "env", "environment"):
                block = var_value.get(block_key)
                if isinstance(block, dict):
                    yield f"{task_name} / vars.{var_name}.{block_key}", block


def test_no_native_typed_labels_or_env_defaults():
    tasks = yaml.safe_load(_BUILD_SPECS.read_text())
    problems = []
    for path, mapping in _iter_labels_and_env_blocks(tasks):
        problems.extend(_find_unsafe_values(mapping, path))

    assert not problems, (
        "build_specs.yml has labels:/env: values that Ansible will native-type "
        "to a non-string when the var is left at its default, which Docker "
        "rejects on container create (\"cannot unmarshal number into Go "
        "struct field ... of type string\"). Add `| string` to the filter "
        "chain (see zot_port / WEBHOOK_PEER_TIMEOUT for the pattern). "
        "Offending values:\n" + "\n".join(f"  - {p}" for p in problems)
    )


def test_webhook_ratelimit_labels_are_string_cast():
    """The exact v0.3.0 regression: reverting `| string` must fail the guard above."""
    tasks = yaml.safe_load(_BUILD_SPECS.read_text())
    for task in tasks:
        if task.get("name") != "Build webhook receiver container spec":
            continue
        labels = task["vars"]["_webhook_spec"]["labels"]
        avg = labels["traefik.http.middlewares.bay-webhook-ratelimit.ratelimit.average"]
        burst = labels["traefik.http.middlewares.bay-webhook-ratelimit.ratelimit.burst"]
        assert avg.strip().endswith("| string }}"), avg
        assert burst.strip().endswith("| string }}"), burst
        return
    raise AssertionError("webhook receiver spec task not found in build_specs.yml")
