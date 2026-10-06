# Plan, approve, up, show and rollback

These are the daily verbs of Bay v2. They work on one project of one fleet.

Bay keeps three truths apart:

- **WANTED**: `bay.toml` at the project's current commit.
- **PINNED**: the fleet lockfile `projects/<name>.lock`, and the services file
  that the fleet last compiled from it.
- **RUNNING**: the receipt that the box wrote after its last deploy
  (see [deploy-receipt.md](deploy-receipt.md)).

"The fleet decides. Main suggests. The box reports."

No verb asks a question. No verb takes a secret on the command line.

## Which project, which fleet

- Inside an app repo, Bay reads the `bay.toml` in the working directory or
  above it. `name` is the project. `fleet` names the fleet.
- The fleet is `~/.config/bay/fleets/<fleet>`. `bay --fleet <path> <verb>` or
  `BAY_FLEET=<path>` uses another directory.
- `--project <name>` works from any directory. The fleet then comes from
  `--fleet`, `BAY_FLEET` or `BAY_FLEET_NAME`.
- Bay reads the project from the checkout that the lock names
  (`local_path`). When you run a verb in another clone, the plan says so in a
  note.

### Projects with no repo

Some projects have no repo of their own. Their `bay.toml` (and the files it
mounts) live in the fleet as `projects/<name>/`. Use `--project <name>` (or
`bay show <name>`) for them. plan, up, show and rollback work the same way,
with these differences:

- WANTED is `projects/<name>/` at the fleet's HEAD. Commit an edit in the
  fleet repo before you plan it.
- The lock has `repo: null` and `local_path: null`. Its `commit` is the fleet
  commit that last changed `projects/<name>/`.
- `bay show` reports WANTED as dirty when the working tree of
  `projects/<name>/` differs from HEAD.
- A project in the fleet with no lock, or with no pinned commit, is read at
  the fleet's HEAD when other projects are planned, with a note. The working
  tree is never read.

## The verbs

```bash
bay init [--name N] [--fleet F] [--box B] [--domain D]   # draft bay.toml, register the project
bay plan [env] [--json] [--log PATH] [--at SHA] [--plan-id ID] [--remote] [--no-remote]
bay approve <plan-id> --reason "<why>"
bay up [env] [--at SHA] [--plan-id ID] [--force --reason "<why>"] [--json] [--log PATH] [--no-push]
bay show [name] [--json] [--no-remote]
bay rollback [env] [--force --reason "<why>"] [--json] [--log PATH] [--no-push]
```

`env` is the `[deploy.<env>]` name. The default is the fleet's primary
environment (`production`).

With `--json`, stdout holds exactly one JSON document, also for an error
(`{"error", "hint", "code"}`). Progress lines and everything the deploy
prints, Ansible included, go to stderr. With `--log <path>`, they are
appended to that file instead, with or without `--json`.

The pin is a config pin. Bay compiles `bay.toml` at the pinned commit, but a
container that builds from source still builds the head of its `branch` on
the box. The `image` in the box receipt shows what really runs. A code pin
(build exactly the pinned commit) is 2.x work.

### bay init

Run it in an app repo that has no `bay.toml`.

1. Bay writes a draft `bay.toml`. It reads three hints: `EXPOSE` in the
   `Dockerfile` gives the port, `package.json` gives port 3000 and a commented
   `npm start`, `pyproject.toml` gives port 8000. `access.mode` is `public`.
   There is one `[deploy.<primary env>]` table with the fleet's default box and
   the domain `<name>.<default_domain>`.
2. Bay checks the draft with the `bay.toml` validator.
3. Bay writes `projects/<name>.lock`: `repo` from `git remote get-url origin`,
   `commit: null`, and `local_path` set to the repo path.
4. Bay commits the fleet repo with the message `bay: init <name>`.

Bay refuses when `bay.toml` exists or the name is taken. A name never
changes. Check the draft, commit it in the app repo, then run `bay plan`.

A project whose lock pins no commit is not deployed. Other projects' plans
leave it out.

### bay plan

1. **WANTED**: Bay reads `bay.toml` at HEAD of the checkout (`--at` picks
   another commit). Uncommitted edits are not part of the plan. The plan
   records them as `wanted.dirty`.
2. Bay copies the fleet inputs to a temporary directory. Every project is
   read at its pinned commit. This project is read at the WANTED commit.
   Bay compiles the whole fleet from that copy, the same way `bay up` will.
3. **PINNED**: Bay compares the result with the fleet's services file, entry
   by entry. Each difference gives one or more steps.
4. **RUNNING**: Bay reads the box receipt over SSH. `--no-remote` skips this.
5. With `--remote`, Bay also runs today's deploy in check mode on the box
   (`--tags deploy_stack`, `-e bay_reconciler_plan_only=true`, `--check`),
   with the compiled file given as extra variables. Without `--remote`, the
   steps come from the compiled files alone and `box_checked` is `false`.

### What the check-mode run touches

Check mode changes nothing live. It writes only to temporary places:

- On each box, the deploy renders every env file it would write (services,
  resources, the webhook receiver, the update watcher) into a scratch
  directory (`/tmp/bay-env-check.*`, mode 0700, files 0600). The live env
  files stay as they are. The scratch directory is removed at the end of the
  run, also when the run fails.
- On each box, the plan bundle and the reconciler package go to a second
  temporary directory, which is also removed.
- On this machine, each box writes its report into a temporary directory
  outside every working tree. Bay reads it and removes it.

So the plan hashes the env files that a real deploy would write. An env file
that a fleet-level role writes outside the deploy (for example the proxy's
DNS credentials) is read as it is on the box.

### The box prediction (`--remote`)

The check-mode run asks each box what it would do to every container. The
box compares the compiled settings with the containers it runs, and writes
one report per box back to this machine. Bay reads the reports into
`box_prediction.containers`: one entry per container with `box`, `name`,
`action` (`noop`, `create`, `recreate`, `start` or `remove`) and `reasons`.
A canary swap counts as `recreate`. `start` is reserved: today the box
leaves a stopped container stopped, so it reports `noop` with the reason
`stopped`.

Each reason is `<code>: <detail>`:

| Code | Meaning |
|---|---|
| `missing` | No container with this name runs on the box. |
| `orphan` | Bay manages the container, but the deploy no longer lists it. |
| `config_hash` | The settings hash differs, or the container has none. |
| `image` | The image name changed, or a new image arrived under the same name. |
| `env` | Env values differ. The reason names the keys, never a value. |
| `env_file` | The env file that the deploy would write differs from the one on the box. The detail names added, removed and changed keys, never a value. When only the line order moved, the detail is "same variables, different order". |
| `labels`, `ports`, `volumes` | That part of the settings differs. |
| `memory` | `mem_limit` or `memswap_limit` differs. Bay sets both to the `memory` value, so swap is never allowed. |
| `stopped` | The container does not run. The deploy leaves it as it is. |
| `zero_downtime` | The new container takes over before the old one stops. |

Bay merges the prediction into `steps`:

- A container that already has a step from the compiled diff gets no second
  step. The compiled diff names the cause.
- Every other `create`, `recreate`, `start` or `remove` becomes a step with
  `source: "box"`. Its reason is the box name and the box's reasons.
- Risk: `remove` is destructive. `create`, `recreate` and `start` are safe. A
  recreate with a volume or database change always has a compiled step, so
  the compiled step sets the risk.
- `noop` adds no step.

A changed env file recreates the container, also when only the line order
moved: the hash covers the env file bytes. The box compares the new file with
the live one and gives the `env_file` reason. When the hash changed and no
reason above applies, the reason is `config_hash` alone: the cause is another
setting that `docker inspect` does not report (command, health check,
networks, log options).

When the check runs but a box returns no report, or a report with no
container list (an older Bay on the box), the plan is blocked.

Bay saves the plan to `<fleet>/plans/<plan-id>.json`. `--json` prints it.
Without `--json`, Bay prints a short table.

The verb exits with the verdict's code:

| Verdict | Exit | Meaning |
|---|---|---|
| `auto` | 0 | `bay up` may apply the plan. |
| `approve` | 10 | A step is destructive or shared. Run `bay approve` first. |
| `blocked` | 20 | Something must be fixed first. `blockers` says what. |
| `stale` | 30 | With `--plan-id`: PINNED or RUNNING moved since the plan was made. |

`blocked` wins over `stale`, and `stale` wins over `approve`.

A plan is **blocked** when:

- `bay.toml` at the commit is missing or not valid, or the fleet does not
  compile.
- The commit is not in the checkout.
- `bay.toml` has no `[deploy.<env>]`, or names a box the fleet does not have.
- A secret that this project's containers need is missing for the box's
  environment (names only, see [deploy-receipt.md](deploy-receipt.md)).
- The fleet repo is behind its remote. Bay runs `git fetch` first. A fleet
  with no remote is never behind. A failed fetch also blocks.
- The fleet's services file was not written by Bay (run `bay import`), or was
  edited by hand.
- `bay.toml` uses a feature Bay cannot deploy yet, unless
  `--allow-unsupported` is given.
- The fleet repo has uncommitted changes and a step is destructive. Without a
  destructive step, the plan only records `fleet.dirty`.

### Risk

Risk is set by the data that a step touches.

| Step | Risk |
|---|---|
| New container, changed container (image, env, memory, domains, routes and so on) | safe |
| Memory lower than the current use | safe |
| New volume, a volume mounted at a new path | safe |
| New database | safe |
| Container removed | destructive |
| Volume removed, volume renamed (a rename is a delete in disguise) | destructive |
| Database removed, renamed or moved to another postgres; database user renamed | destructive |
| Secret removed from a container that runs (or when the box was not read) | destructive |
| Secret removed from a container that does not run | safe |
| Any change to a `[resources.*]` entry (shared postgres, redis and so on) | shared |
| Shared resource removed | destructive |
| Tailnet allowlist in `bay.fleet.toml` changed since the last fleet commit | shared |
| Deploy webhook changed | shared |
| Container of another project changed | shared |
| Container of another project removed | destructive |
| Box predicts a create, recreate or start (`source: box`) | safe |
| Box predicts a remove (`source: box`) | destructive |

`bay up` writes the whole compiled file, so a change to another project's
container is part of this plan too.

`bay up` also refreshes the shared proxy, the gateway and the update watcher
on the boxes. That work comes from the fleet, not from `bay.toml`. The plan
says so once, in `notes`. It is not a step.

### bay approve

```bash
bay approve 3f2a9c0d1e2b --reason "the old volume holds test data only"
```

Bay writes `<fleet>/plans/<plan-id>.approved` with the plan hash, the reason
and the time. `bay plan` then shows the verdict `auto` with the approval.
An approval matches one plan hash. When anything in the plan changes, the
plan id changes and the approval no longer applies. Bay refuses to approve a
`blocked` or `stale` plan.

### bay up

1. Bay plans again. With `--plan-id`, Bay checks the saved plan: it is
   `stale` when the lock, the box receipt or any other plan input moved.
2. Bay refuses `blocked` (exit 20) and `stale` (exit 30). Bay refuses
   `approve` (exit 10) unless an approval matches. `--force --reason "<why>"`
   overrides `approve` only, never `blocked` or `stale`. The reason goes into
   the lock as `previous.force_reason`.
3. Bay writes the lock: the project pin moves to the planned commit. The
   environment record gets `result: pending` and `previous` (the pin it
   replaces).
4. Bay compiles the fleet into its services file, with the hash header.
5. Bay commits the fleet repo: `bay: up <name> <env> <short sha>`, and
   prints the fleet commit.
6. Bay runs today's deploy for the box's environment, limited to
   `--tags deploy_stack` (the same work as `bay deploy <env> --tags deploy_stack`).
7. Bay reads the receipt back and pins every project that the deploy
   covered (see below). Bay commits all those locks once:
   `bay: receipt <box env> (<n> projects)`.
8. Bay reports what the deploy did. `applied` in the JSON result lists, per
   box, every container whose receipt `action` is not `noop`:
   `{"box", "container", "action", "healthy"}`. `steps` stays the plan; when
   the two differ, `applied` is the truth. A receipt whose `fleet_commit` is
   not this deploy's commit is an older one (the deploy stopped before the
   box wrote a new one). Bay leaves it out of `applied` and says so in
   `notes`.
9. When the fleet repo has a remote, Bay pushes it. `--no-push` skips this.
   A failed push is a warning, never a failed deploy. The JSON result says
   `pushed: true|false` and `push_error`. Bay pushes only when the fleet
   directory is the root of its git repo. A fleet inside a larger repo is
   committed but not pushed: the push would publish the other work in that
   repo too. Bay prints a warning, and `push_skipped` says why.

When the deploy fails, the lock keeps the new pin and records
`result: failed`. Bay still commits and pushes that record. `bay show` then
says `HALF` until a deploy succeeds.

#### Every deployed project is pinned

`bay up` deploys the whole box environment, not only one project. So after
the deploy, OK or failed, Bay updates the lock of every project that has a
`[deploy.<env>]` on the same box environment:

| Project | `commit` |
|---|---|
| The project you ran `bay up` for | The planned commit. |
| A project in the fleet (`repo: null`) | The commit the compile read it at: its pin, or, with no pin, the last fleet commit that changed `projects/<name>/` at the fleet's HEAD. |
| A repo project with a pinned commit | Its pin. It does not move. |
| A repo project with no pinned commit | Not deployed. Its lock does not change. A note in the result names it. |

Each pinned environment gets `result`, `deployed_at`, `plan_id` and its own
`last_receipt_sha256`. When the commit replaces a different earlier pin,
`previous` holds the earlier one. `bay show` then says `ok` for each of these
projects, or `HALF` after a failed deploy.

A project in the fleet that is pinned moves only with its own `bay up`. A
deploy through another project reads it at its pin.

The JSON result lists the pinned environments in `pinned`
(`project`, `env`, `commit`, `result`) and the left-out projects in `notes`.

### bay rollback

Bay takes the environment's `previous` commit and runs `bay up` with it. The
two pins swap, so a second rollback undoes the first. Bay refuses when there
is no `previous`. The output names the old and the new commit and the steps.

### bay show

Bay prints WANTED (the checkout's HEAD, the `bay.toml` hash, uncommitted
changes), PINNED (the lock) and RUNNING (this project's containers in each
box receipt), and one status word per environment:

| Status | Meaning |
|---|---|
| `ok` | WANTED, PINNED and RUNNING agree. |
| `behind` | The project's HEAD is ahead of the pin. Run `bay plan`. |
| `drift` | The box runs something else than the pin: the receipt differs from the one `bay up` recorded, or the fleet pins a commit this environment never got. |
| `unknown` | The box was not read, has no receipt, or `bay up` never ran here. |
| `HALF` | The last `bay up` failed or never reported back. |

## bay compile

`bay compile` reads every project at the commit its lock pins, through the
same temporary copy of the fleet that `bay plan` and `bay up` compile. A
project with no pinned commit is left out, with a note on stderr.
`bay compile --working-tree` reads the local checkouts as they are instead.
Use it while you develop, never to deploy.

## The plan JSON

The schema is `src/bay_cli/schemas/plan.schema.json` (`plan_version` 1). A new
field may appear without a version bump. Readers ignore fields they do not
know.

```json
{
  "plan_version": 1,
  "plan_id": "3f2a9c0d1e2b",
  "plan_sha256": "<64 hex>",
  "created_at": "2026-10-06T12:00:00Z",
  "project": "webapp",
  "env": "production",
  "box": "box-1",
  "box_env": "production",
  "fleet": {"name": "myfleet", "commit": "<sha>", "dirty": false, "behind": false},
  "wanted": {"commit": "<sha>", "toml_sha256": "<64 hex>", "dirty": false},
  "pinned": {"commit": "<sha>", "lock_sha256": "<64 hex>"},
  "running": {
    "checked": true,
    "receipt_sha256": "<64 hex>",
    "boxes": [{"box": "box-1", "error": null,
               "containers": [{"name": "webapp", "image": "...", "config_hash": "..."}]}]
  },
  "box_checked": true,
  "box_prediction": {
    "checked": true,
    "containers": [
      {"box": "box-1", "name": "webapp", "action": "recreate",
       "reasons": ["config_hash: changed (1a2b3c4d5e6f -> 6f5e4d3c2b1a)",
                   "env_file: the env file bytes differ (same variables, different order)"]},
      {"box": "box-1", "name": "postgres", "action": "recreate",
       "reasons": ["config_hash: changed (...)", "memory: memswap_limit 1g -> 512m"]}
    ],
    "errors": []
  },
  "steps": [
    {"id": "s1", "kind": "container", "container": "webapp", "resource": null,
     "project": "webapp", "action": "update", "risk": "safe",
     "reason": "changed: volumes", "source": "compile"},
    {"id": "s2", "kind": "volume", "container": "webapp", "resource": "webapp-data",
     "project": "webapp", "action": "rename", "risk": "destructive",
     "reason": "volume webapp-data becomes webapp-files; a rename is a delete in disguise: ...",
     "source": "compile"},
    {"id": "s3", "kind": "container", "container": "postgres", "resource": null,
     "project": null, "action": "recreate", "risk": "safe",
     "reason": "box box-1 predicts recreate: config_hash: changed (...); memory: memswap_limit 1g -> 512m",
     "source": "box"}
  ],
  "unsupported": [],
  "missing_secrets": [],
  "blockers": [],
  "notes": ["..."],
  "verdict": "approve",
  "exit_code": 10,
  "approval": null,
  "stale": []
}
```

- `box_env` is the box's `env` in `bay.fleet.toml`: the group that the
  deploy targets and the receipt file name.
- `pinned.commit` is the commit this environment runs per the lock.
- `running.receipt_sha256` hashes only the box, name, image and config hash
  of this project's containers. A deploy of another project rewrites the
  receipt file, but this hash stays the same, so the plan does not go stale.
- `kind` is one of `container`, `volume`, `database`, `database_user`,
  `secret`, `resource`, `tailnet`, `fleet`. `action` is one of `create`,
  `update`, `remove`, `rename`, `move`, and for a box step also `recreate`
  and `start`.
- `source` is `compile` for a step from the compiled diff, `box` for a step
  from the box prediction.
- `box_prediction` is empty without `--remote`.
- `plan_sha256` is the SHA-256 of the plan without `plan_id`, `plan_sha256`,
  `created_at`, `verdict`, `exit_code`, `approval` and `stale`. `plan_id` is
  its first 12 hex digits. The same inputs give the same id.
- A plan holds secret names, never values.

## The lockfile

`projects/<name>.lock` (schema `src/bay_cli/schemas/bay_lock.schema.json`).
Only the CLI writes it, atomically. `bay up` adds these optional keys to an
environment:

```json
"envs": {
  "production": {
    "box": "box-1",
    "commit": "<sha>",
    "deployed_at": "2026-10-06T12:00:00Z",
    "result": "ok",
    "plan_id": "3f2a9c0d1e2b",
    "last_receipt_sha256": "<64 hex>",
    "previous": {"commit": "<sha>", "deployed_at": "...", "receipt_sha256": "<64 hex>"},
    "adopted": {"...": "..."}
  }
}
```

- The top-level `commit` is the project pin that the fleet compiles. One
  `bay.toml` serves all environments of a project, so `bay up staging` also
  moves the compiled production entries. Production then shows `drift` until
  `bay up production` runs.
- `result` is `pending` while a deploy runs, then `ok` or `failed`.
- `previous` is one level of history: enough for `bay rollback`.
- `box` is written by the first `bay up`. After that the fleet decides: a new
  `box` in `bay.toml` gives a note, not a move.
