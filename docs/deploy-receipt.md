# Deploy receipt and `bay status --json`

Bay keeps three truths apart:

- **Wanted**: what `bay.toml` asks for at the project's current commit: the HEAD of the
  checkout you stand in, or the head of the `[deploy.<env>].branch` branch in the fleet's
  repo cache. For a project in the fleet it is `projects/<name>/` at the fleet's HEAD. The
  full definition is in [plan.md](plan.md#bay-plan).
- **Pinned**: what the fleet lockfile pins.
- **Running**: what the box reports. The deploy receipt is this record.

"The fleet decides. Main suggests. The box reports."

This page documents two formats. Both are stable. A reader can rely on them.

- The **receipt** that each box writes after each deploy (`receipt_version` 1).
- The **status document** that `bay status --json` prints (`status_version` 2).

The JSON Schema for both is `src/bay_cli/schemas/status.schema.json`. The
receipt is its `$defs/receipt`.

## Versioning rules

- A new field can appear without a version bump. Readers must ignore fields
  they do not know.
- A removed field, a renamed field or a changed type bumps the version.
- `status_version` 2 (Bay 2.0.0) removed `framework.pinned`, `framework.checkout` and
  `framework.latest`, because a machine has one Bay install and no per-fleet pin. It added
  `framework.version`, `framework.path` and `fleet.source`. The receipt format did not change.
- `projects` is an empty object in version 1. A later release fills it from
  the fleet lockfiles, with no version bump.

## The receipt

### Where it is

`/var/lib/bay/receipts/<env>.json` on every box of the environment. `<env>` is
the name that the deploy got as its target (`target_host`), for example `production`, the box
environment of the inventory file `hosts/<env>`. A group name works as a target too, and then it
names the file: `bay deploy eu` writes `eu.json`, which `bay status --env production` and
`bay plan` do not read. To deploy one group or host, keep the box env and limit it:
`bay deploy production -- --limit eu`. `bay up` deploys against the box env of the project's box, not the
deploy env (see [layout-scenarios.md](layout-scenarios.md#which-env-does-a-verb-take)).
There is one file per environment, so one box can hold several.

The receipt that the last deploy replaced stays next to it as
`<env>.prev.json`.

The files belong to root, with mode `0644`. A receipt holds no secret, so any
user that SSH logs in as can read it.

### When it is written

The box writes the receipt at the end of the container pass of every deploy
that reaches it:

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

Every deploy that reaches the container pass rotates the file: `bay up`, `bay rollback`, the
apply of a `bay remove` plan, and a `bay deploy <env>` with no `--tags` or with a tag that runs
the container pass (`deploy_stack`). So `<env>.prev.json` is the state just before
the last deploy of that env, whichever project or verb ran it. A `bay up` that changes nothing, and
a `bay up` for another project on the same box env, both overwrite it with the state you had
before. Since 2.2.0, `bay rollback` reads its code target from the lock's
`previous.containers` instead. It reads this file only for a lock written before 2.2.0 (see
[plan.md](plan.md#bay-rollback)). A webhook stamp does not rotate it.

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
    {"name": "web", "image": "registry.example.com/acme/web:1a2b3c4d5e6f", "image_ref": "registry.example.com/acme/web:latest", "commit": "1a2b3c4d5e6f", "commit_source": "label", "config_hash": "a1b2...", "action": "recreate", "healthy": true},
    {"name": "postgres", "image": "postgres:16", "image_ref": "postgres:16", "commit": null, "commit_source": null, "config_hash": "c3d4...", "action": "noop", "healthy": true},
    {"name": "old-worker", "image": null, "image_ref": null, "commit": null, "commit_source": null, "config_hash": null, "action": "remove", "healthy": null}
  ],
  "projects": {},
  "routes": [
    {"name": "notes", "domains": ["notes.ts.example.com"], "upstream": "http://100.64.0.9:8080", "pass_host_header": true, "identity_inject": false, "entrypoint": "websecure_tailnet"}
  ]
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `receipt_version` | integer | Always `1` for this format. |
| `env` | string | The environment name. |
| `box` | string | The `inventory_hostname` of the host: the name at the start of its line in `hosts/<env>`. It is the box name of `bay.fleet.toml` when the host line uses that name (see [layout-scenarios.md](layout-scenarios.md#words-deploy-env-box-env-and-group)). `bay validate` warns when a box name is neither a host nor a group of `hosts/<env>`. |
| `deployed_at` | string | Time the receipt was written, RFC 3339 in UTC (`YYYY-MM-DDTHH:MM:SSZ`). |
| `framework_version` | string or null | `bay_version` from the framework's `version.yml`. |
| `framework_commit` | string or null | Full git SHA of the framework checkout. Null when the deploy did not come from `bay deploy`, or the framework is not a git checkout. |
| `fleet_commit` | string or null | Full git SHA of the fleet repo HEAD at deploy time. Null outside git. |
| `fleet_dirty` | boolean or null | True when the fleet directory had uncommitted changes. Null outside git. |
| `result` | `"ok"` or `"failed"` | `ok` only when the container pass exited 0 and every action succeeded. |
| `containers` | array | One entry per container in the deploy, then one per container it removed. |
| `projects` | object | Empty in version 1. |
| `code_moves` | array | Only when the deploy passed code targets (`bay up`, `bay rollback`): one `{name, status, detail}` per target, the report of `bay_reconcile.codepin`. `status` is `retag`, `noop`, `skipped` (the container keeps its image) or `missing` (a strict target; the deploy stopped). A `noop` with the detail `already running <commit12>` means the box has no `<repo>:<commit12>` tag, but the running container's image already carries that commit (its commit label, or that tag on the same image id). Absent in receipts written before 2.1. |
| `routes` | array | The tailnet routes the box serves, read from the route file the traefik role rendered (`dynamic/tailnet-proxies.yml` in the stack directory). One `{name, domains, upstream, pass_host_header, identity_inject, entrypoint}` per route. Empty on a box with no route file, so only the ingress box lists routes. `bay show --routes` reads it as RUNNING. Absent in receipts written before 2.3.0. |

Each container entry:

| Field | Type | Meaning |
|-------|------|---------|
| `name` | string | Container name. |
| `image` | string or null | The image the container runs. `<repo>:<commit12>` when that commit tag resolves on the box to the running image, else the reference the deploy asked for. Null for a removed container. |
| `image_ref` | string or null | The image reference the deploy asked for (the spec, often `:latest`). Absent in receipts written before 2.1. |
| `commit` | string or null | The 12-character commit the running image was built from: the image label `com.bay.commit`, or `org.opencontainers.image.revision` for older builds. An image with neither label falls back to its tags: when exactly one tag of the running image is a commit tag (exactly 12 lowercase hex characters, in any repo), that tag names the commit. Null when the image has no commit label and no commit tag, or several different commit tags (a pulled third-party image, or a build from before 2.1 that was never tagged by commit). Absent before 2.1. |
| `commit_source` | string or null | Where `commit` came from: `label` (a commit label of the image) or `tag` (the single commit tag of an image with no commit label). Null when `commit` is null. With `tag`, `image` is that tag. Absent in receipts written before 2.5.0; a reader takes a `commit` there as `label`. |
| `config_hash` | string or null | The config hash the deploy compared. Null for a removed container. |
| `action` | string or null | `noop`, `create`, `recreate`, `start` or `remove`. A zero-downtime swap is `recreate`. Null when the pass crashed before it reported. `start` is reserved; version 1 does not write it. |
| `failed` | boolean | True when the container pass reported this container's action as failed. `bay up` reads it to tell the first image apart (exit 40, see [plan.md](plan.md#the-first-image)). Absent in receipts written before 2.2.0. |
| `healthy` | boolean or null | Read once, right after the pass. `true`: running and healthy. `false`: not running, or unhealthy. `null`: running with no health check, still starting, removed, or unknown. |

A webhook build changes the code without a deploy. After it recreates a
container, `rebuild.sh` stamps that container's `commit` and `image` into
`<env>.json` (`python -m bay_reconcile.receipt stamp --env <env> --name <c>
--commit <c12> --image <ref>`). The stamp rewrites the file atomically. It
sets `commit_source` to `label` (null when it names no commit) and moves
nothing else: `image_ref`, `config_hash`, `action`, `deployed_at` and
`<env>.prev.json` stay as the last deploy wrote them. `rebuild.sh` runs as the
app user, so the receipts directory is group `docker`, mode `0775`. A failed
stamp is a log line, never a failed build. `bay plan` hashes `image_ref`, so a
stamp is not drift; it reads `commit` for "code at X, config pinned at Y" (see
[plan.md](plan.md), "Code and config").

These fields, `failed` (2.2.0), `routes` (2.3.0) and `commit_source` (2.5.0) are additive, so the receipt stays `receipt_version` 1
and `bay status --json` stays `status_version` 2. A reader of an older receipt
must treat a missing `commit` or `image_ref` as null, a missing `commit_source` as `label` when `commit` is set, a missing `failed` as unknown,
and a missing `routes` as "this receipt does not say".

`bay up` deploys the whole box environment, so the receipt covers every project on it. It reads `action` back: every container that is not `noop` goes into
the `applied` list of its JSON result, when the receipt's `fleet_commit` is
the commit of that `bay up` (see [plan.md](plan.md)). It reads `code_moves`
the same way: every `skipped` or `missing` move goes into `code_kept` and
prints as `code: kept <container> (<detail>)`. A `noop` move is never kept:
the box already runs the target.

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

The human `bay status` reads no box by default. With `--env <env>` it also
reads the receipt of each box of that environment and prints one line per box:
the result, the deploy time, the container count and, for a receipt that lists
routes, the route count. `--no-remote` skips that read.

### How boxes are read

For each environment, the CLI runs one Ansible ad-hoc call, the same path that
`bay logs` and `bay gateway` use:

```
uv run --project <framework> ansible <env> -m ansible.builtin.command \
  -a "cat /var/lib/bay/receipts/<env>.json" -T 10 --ssh-extra-args="-o BatchMode=yes"
```

- It runs with the fleet as its working directory. Bay passes the fleet's
  `hosts/` inventory, its `.vault_pass` and the SSH settings to Ansible, so no
  `ansible.cfg` in the fleet is needed.
- Each box runs one `cat`. No `sudo` is used. The file is world-readable.
- The `ansible.posix.json` output callback gives one parsed result per box.
- SSH runs in batch mode and stdin is closed, so the command never prompts.
  A missing key or an unknown host key is an error string, not a question.

### Format, version 2

```json
{
  "status_version": 2,
  "framework": {"version": "2.0.0", "path": "/home/me/.local/share/bay/framework"},
  "fleet": {"root": "/home/me/.config/bay/fleets/acme", "source": "~/.config/bay/fleets", "commit": "9fadd62...", "dirty": false},
  "boxes": [
    {"env": "production", "box": "app-1", "receipt": {"receipt_version": 1, "...": "..."}, "error": null},
    {"env": "production", "box": "app-2", "receipt": null, "error": null},
    {"env": "staging", "box": "stage-1", "receipt": null, "error": "unreachable: ssh: connect to host ... timed out"}
  ]
}
```

| Field | Meaning |
|-------|---------|
| `framework.version` | `bay_version` from `version.yml` in the install, or null when the file is missing. |
| `framework.path` | The checkout that the `bay` command runs from. |
| `fleet.root` | The fleet directory. |
| `fleet.source` | How Bay found the fleet: `--fleet`, `BAY_FLEET`, `bay.toml` (the fleet named in the app repo) or `~/.config/bay/fleets` (`BAY_FLEET_NAME`). `bay status` never takes the fleet from the directory you stand in, so it never reports `cwd`. With none of the four, it stops with "no fleet selected". |
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
