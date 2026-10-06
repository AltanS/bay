# Deploy receipt and `bay status --json`

Bay keeps three truths apart:

- **Wanted**: what `bay.toml` on the main branch asks for.
- **Pinned**: what the fleet lockfile pins.
- **Running**: what the box reports. The deploy receipt is this record.

"The fleet decides. Main suggests. The box reports."

This page documents two formats. Both are stable. A reader can rely on them.

- The **receipt** that each box writes after each deploy (`receipt_version` 1).
- The **status document** that `bay status --json` prints (`status_version` 1).

The JSON Schema for both is `src/bay_cli/schemas/status.schema.json`. The
receipt is its `$defs/receipt`.

## Versioning rules

- A new field can appear without a version bump. Readers must ignore fields
  they do not know.
- A removed field, a renamed field or a changed type bumps the version.
- `projects` is an empty object in version 1. A later release fills it from
  the fleet lockfiles, with no version bump.

## The receipt

### Where it is

`/var/lib/bay/receipts/<env>.json` on every box of the environment. `<env>` is
the environment name that `bay deploy <env>` got, for example `production`.
There is one file per environment, so one box can hold several.

The receipt that the last deploy replaced stays next to it as
`<env>.prev.json`.

The files belong to root, with mode `0644`. A receipt holds no secret, so any
user that SSH logs in as can read it.

### When it is written

The box writes the receipt at the end of the container pass of every full
deploy:

- It is written when the pass succeeds (`"result": "ok"`).
- It is written when the pass fails (`"result": "failed"`). The deploy still
  stops with an error after that.
- It is not written for a dry run (`-- --check`), for `-e bay_reconciler_plan_only=true`,
  or for the single-container webhook pass that runs inside a deploy.
- A deploy that never reaches the container pass (for example
  `--tags traefik`) writes no receipt. The old receipt stays.

If the receipt cannot be written, the deploy prints a warning and goes on.
The containers are already in place at that point.

The reconcile bundle holds every resolved secret. The whole pass runs in one
block, and the bundle is removed in its `always:` step. So the bundle leaves
the box on every path: success, a failed pass, or a failed step before the
pass.

### How it is written

`python3 -m bay_reconcile.receipt` writes it. The deploy ships that module to
the box together with the container engine. The steps are:

1. Copy the current `<env>.json` to a temporary file, then rename it to
   `<env>.prev.json`.
2. Write the new receipt to a temporary file in the same directory.
3. Rename the temporary file to `<env>.json`.

A rename in one directory is atomic. A reader sees the old receipt or the new
one, never half of a file.

The task is "Write the deploy receipt" in
`roles/container_lifecycle/tasks/reconcile.yml`. The directory
`/var/lib/bay/receipts` is fixed. It is not a setting, because the box and
`bay status` must agree on it. The one definition is `RECEIPTS_DIR` in
`src/bay_reconcile/receipt.py`. The task repeats the same literal, and a test
checks that the two match.

### Format, version 1

```json
{
  "receipt_version": 1,
  "env": "production",
  "box": "app-1",
  "deployed_at": "2026-10-06T14:03:11Z",
  "framework_version": "2.0.0",
  "framework_commit": "4ec8a00c0ffee...",
  "fleet_commit": "9fadd62c0ffee...",
  "fleet_dirty": false,
  "result": "ok",
  "containers": [
    {"name": "web", "image": "ghcr.io/acme/web:1.4", "config_hash": "a1b2...", "action": "recreate", "healthy": true},
    {"name": "postgres", "image": "postgres:16", "config_hash": "c3d4...", "action": "noop", "healthy": true},
    {"name": "old-worker", "image": null, "config_hash": null, "action": "remove", "healthy": null}
  ],
  "projects": {}
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `receipt_version` | integer | Always `1` for this format. |
| `env` | string | The environment name. |
| `box` | string | The box name in the fleet (`inventory_hostname`). |
| `deployed_at` | string | Time the receipt was written, RFC 3339 in UTC (`YYYY-MM-DDTHH:MM:SSZ`). |
| `framework_version` | string or null | `bay_version` from the framework's `version.yml`. |
| `framework_commit` | string or null | Full git SHA of the framework checkout. Null when the deploy did not come from `bay deploy`, or the framework is not a git checkout. |
| `fleet_commit` | string or null | Full git SHA of the fleet repo HEAD at deploy time. Null outside git. |
| `fleet_dirty` | boolean or null | True when the fleet directory had uncommitted changes. Null outside git. |
| `result` | `"ok"` or `"failed"` | `ok` only when the container pass exited 0 and every action succeeded. |
| `containers` | array | One entry per container in the deploy, then one per container it removed. |
| `projects` | object | Empty in version 1. |

Each container entry:

| Field | Type | Meaning |
|-------|------|---------|
| `name` | string | Container name. |
| `image` | string or null | Image reference. Null for a removed container. |
| `config_hash` | string or null | The config hash the deploy compared. Null for a removed container. |
| `action` | string or null | `noop`, `create`, `recreate`, `start` or `remove`. A zero-downtime swap is `recreate`. Null when the pass crashed before it reported. `start` is reserved; version 1 does not write it. |
| `healthy` | boolean or null | Read once, right after the pass. `true`: running and healthy. `false`: not running, or unhealthy. `null`: running with no health check, still starting, removed, or unknown. |

The fleet and framework commits come from the CLI. `bay deploy` passes them
to the deploy as one JSON extra var:

```
-e '{"bay_receipt_fleet_commit": "...", "bay_receipt_fleet_dirty": false, "bay_receipt_framework_commit": "..."}'
```

## `bay status --json`

```
bay status --json                     # every environment under hosts/
bay status --json --env production    # one environment
bay status --json --no-remote         # no SSH, boxes is []
```

The human `bay status` output does not change. `--env` and `--no-remote`
apply to `--json` only.

### How boxes are read

For each environment, the CLI runs one Ansible ad-hoc call, the same path that
`bay logs` and `bay gateway` use:

```
uv run --project <framework> ansible <env> -m ansible.builtin.command \
  -a "cat /var/lib/bay/receipts/<env>.json" -T 10 --ssh-extra-args="-o BatchMode=yes"
```

- It runs from the fleet root, so the fleet's `ansible.cfg`, `hosts/<env>`
  and SSH settings apply.
- Each box runs one `cat`. No `sudo` is used. The file is world-readable.
- The `ansible.posix.json` output callback gives one parsed result per box.
- SSH runs in batch mode and stdin is closed, so the command never prompts.
  A missing key or an unknown host key is an error string, not a question.

### Format, version 1

```json
{
  "status_version": 1,
  "framework": {"pinned": "v2.0.0", "checkout": "v2.0.0", "latest": "v2.0.1"},
  "fleet": {"root": "/home/me/.config/bay/fleets/acme", "commit": "9fadd62...", "dirty": false},
  "boxes": [
    {"env": "production", "box": "app-1", "receipt": {"receipt_version": 1, "...": "..."}, "error": null},
    {"env": "production", "box": "app-2", "receipt": null, "error": null},
    {"env": "staging", "box": "stage-1", "receipt": null, "error": "unreachable: ssh: connect to host ... timed out"}
  ]
}
```

| Field | Meaning |
|-------|---------|
| `framework.pinned` | The version in the fleet's `.bay-version`, or null. |
| `framework.checkout` | The exact tag at the framework checkout's HEAD, else its short SHA, or null. |
| `framework.latest` | The highest tag the framework checkout knows, or null. |
| `fleet.root` | The fleet directory. |
| `fleet.commit` | Full SHA of the fleet repo HEAD, or null outside git. |
| `fleet.dirty` | True when the fleet directory has uncommitted changes, or null outside git. |
| `boxes[].env` | The environment. |
| `boxes[].box` | The box name. Null when Ansible could not list any box for the environment. |
| `boxes[].receipt` | The receipt object, or null. |
| `boxes[].error` | Null, or a short reason the receipt could not be read. |

How to read a box entry:

| `receipt` | `error` | Meaning |
|-----------|---------|---------|
| object | null | The box reported. |
| null | null | The box answered and has no receipt yet. It was not deployed since receipts shipped. |
| null | string | The box could not be read (unreachable, permission, bad JSON, Ansible failed). |

The global `bay --json status` prints the same document inside the usual
`{"ok": true, "command": "status", "data": ...}` envelope.

## Missing-secret check

`bay validate` and `bay secret missing <env>` compare the secret **names**
that the services need with the names in the environment's secrets file. They
never print, log or pass a value. Details: `src/bay_cli/secrets_check.py`.

```
bay secret missing production          # names only, exit 1 when one is missing
bay secret missing production --json   # {"env", "checked", "warning", "missing": [{"name", "used_by"}]}
```

With no `.vault_pass`, the check cannot run. Both commands then report
`cannot check: no vault password` as a warning, not as a failure.
