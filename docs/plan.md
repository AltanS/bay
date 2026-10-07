# Plan, approve, up, show and rollback

These are the daily verbs of Bay v2. They work on one project of one fleet.

Bay keeps three truths apart:

- **WANTED**: `bay.toml` at the project's current commit.
- **PINNED**: the fleet lockfile `projects/<name>/bay.lock`, and the services
  file that the fleet last compiled from it.
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
- With no `bay.toml` here or above and no `--project`, `bay plan <env>` and
  `bay up <env>` cover the whole environment (see
  [The whole environment](#the-whole-environment)). The fleet is the one that
  `--fleet`, `BAY_FLEET` or `BAY_FLEET_NAME` names, or the fleet directory you
  stand in (a `bay.fleet.toml` here or above). `bay approve` also finds the
  fleet directory you stand in.
- Every verb that writes to a fleet repo or acts on a box prints
  `fleet: <name> (<path>)` as its first line on stderr, before it does
  anything. With `--json` the line stays on stderr, so stdout holds one
  document. Verbs that only read (`show`, `status`, `toml validate`,
  `self version`, `fleet ls` and the like) do not print it. When no fleet can
  be found, there is no line: the verb stops with "no fleet selected".
- Bay finds the app repo by the lock's `repo` URL, never by a path. It reads
  the git checkout you stand in when its `origin` is that repo. Otherwise it
  reads the fleet's repo cache, `<fleet>/.bay-cache/repos/<slug>`, which it
  clones on first use and fetches before each plan. Two projects in one repo
  share one cache. See [install.md](install.md#where-bay-reads-an-app-repo).
- A pinned commit that is in neither the checkout nor the cache is an error
  that names the project. Bay never skips the project.

### Projects with no repo

Some projects have no repo of their own. Their `bay.toml` (and the files it
mounts) live in the fleet as `projects/<name>/`. Use `--project <name>` (or
`bay show <name>`) for them. plan, up, show and rollback work the same way,
with these differences:

- WANTED is `projects/<name>/` at the fleet's HEAD, without
  `projects/<name>/bay.lock`. Commit an edit in the fleet repo before you plan
  it. `bay up` commits the lock, and that commit does not change WANTED.
- Its `commit` is the fleet commit that last changed `projects/<name>/`,
  the lock not counted. Its `repo` may name the repo a webhook builds from.
- `bay show` reports WANTED as dirty when the working tree of
  `projects/<name>/` differs from HEAD.
- A project in the fleet with no lock, or with no pinned commit, is read at
  the fleet's HEAD when other projects are planned, with a note. The working
  tree is never read.

## The verbs

```bash
bay init [--name N] [--fleet F] [--box B] [--domain D] [--toml-path P]   # draft bay.toml, register the project
bay plan [env] [--json] [--log PATH] [--at SHA] [--plan-id ID] [--remote] [--no-remote] [--data keep]
bay approve <plan-id> --reason "<why>"
bay up [env] [--at SHA] [--plan-id ID] [--force --reason "<why>"] [--data keep] [--json] [--log PATH] [--no-push]
bay show [name] [--json] [--no-remote]
bay rollback [env] [--force --reason "<why>"] [--data keep] [--json] [--log PATH] [--no-push]
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
3. Bay writes `projects/<name>/bay.lock`: `repo` from
   `git remote get-url origin`, `toml_path`, and `commit: null`.
4. Bay commits the fleet repo with the message `bay: init <name>`.

`--toml-path services/api/bay.toml` puts the draft at that path, relative to
the repo root, for a repo with several apps. The lock records it as
`toml_path`. The default is `bay.toml` at the repo root.

Bay refuses when `bay.toml` exists, when the name is taken, and when the repo
has no `origin` remote: Bay finds the repo by that URL. A name never changes.
Check the draft, commit it in the app repo, push it, then run `bay plan`.

A project whose lock pins no commit is not deployed. Other projects' plans
leave it out.

### bay plan

1. **WANTED**: Bay reads `bay.toml` at HEAD of the checkout you stand in, or
   at the head of the remote's default branch in the repo cache (`--at` picks
   another commit). Uncommitted edits are not part of the plan. The plan
   records them as `wanted.dirty`. When the WANTED commit is on no branch of
   the remote, the plan says so in a note: `bay up` will refuse it.
2. Bay copies the fleet inputs to a temporary directory. Every project is
   read at its pinned commit. This project is read at the WANTED commit.
   Each file that a `bay.toml` mounts is copied from beside the toml to
   `files/<target>` in the copy (see [The project folder](#the-project-folder)).
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
- A box move of a project with a named volume or a database, unless
  `--data keep` is given (see [Box move](#box-move)).

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
| Container of another project changed or removed | that project's own risk, by the rows above |
| Container that no project owns changed | shared |
| Container that no project owns removed | destructive |
| Box move of a project with a named volume or a database | destructive (blocked until `--data keep`) |
| Box move of a project with neither | shared |
| The move's container removed on the old box | the move's risk |
| The move's container created on the new box | safe |
| Box predicts a create, recreate or start (`source: box`) | safe |
| Box predicts a remove (`source: box`) | destructive |

`bay up` writes the whole compiled file, so a change to another project's
container is part of this plan too. Such a step carries that project in
`project` and keeps the risk it has in its own plan: a safe change of project
B is safe in a plan for project A, and two safe changes give `auto`.

**What is shared.** `shared` is kept for things that no single `bay.toml`
owns, so a change to them can touch every project:

- a `[resources.*]` entry of `bay.fleet.toml` (a shared postgres or redis).
  A change to it reaches every database on it and every project that uses
  it. A project's own database on that postgres is not shared: its steps
  carry the project and the risk rows above;
- the deploy webhook (the webhook receiver);
- the tailnet allowlist of `bay.fleet.toml`;
- a container in the services file that no project owns;
- a box move with no data (the project leaves one box for another).

The proxy, the gateway, the update watcher and the networks are refreshed by
every `bay up` but are not steps (see the note below). A step whose
`project` is null is always one of the shared things above.

`bay up` also refreshes the shared proxy, the gateway and the update watcher
on the boxes. That work comes from the fleet, not from `bay.toml`. The plan
says so once, in `notes`. It is not a step.

### Box move

The box a project runs on has two truths. WANTED is `deploy.<env>.box` in
`bay.toml` at the planned commit, or the fleet's `default_box` when it names
none. PINNED is `box` in the lock's environment record, written by the first
`bay up`. The compiler uses the PINNED box, so editing `deploy.<env>.box`
alone never moves a running workload. The plan compares the two.

When they differ, the plan has a step `kind: move`, `action: move`, with
`resource: "<old box> -> <new box>"`, and it compiles the project on the new
box. Per container it adds a `remove` on the old box (the move's risk) and a
`create` on the new box (safe). `moves` in the plan JSON lists, per move,
`from`, `to`, both box environments, the containers, and the named volumes
and databases that stay behind.

| The project has | Risk | Verdict |
|---|---|---|
| No named volume and no database | `shared` | `approve` |
| A named volume or a database | `destructive` | `blocked` until `--data keep`, then `approve` |

The blocker names each volume and database that would stay on the old box.

**`--data keep`** (on `bay plan`, `bay up` and `bay rollback`) says: start
empty on the new box. The new box gets new, empty volumes with the same names
and a fresh database in its own postgres resource. The old box's containers of
the project are removed (a `remove` step, so `bay approve` is still needed).
The old volumes and the old database stay untouched on the old box. Bay never
removes them. The plan lists them by name in `moves` and in `notes`, with the
`docker volume rm <name>` and `DROP DATABASE <name>;` lines to run by hand
later, when you no longer need the data.

**`--data move` is deferred.** Bay does not copy volumes or databases between
boxes yet, and refuses the flag. Copy the data yourself (backup on the old
box, restore on the new) after a `--data keep` move, or keep the box.

`bay up` applies an approved move like any plan, and writes the new box into
the lock's environment record. From then on the compiler uses the new box.
Both boxes are usually in one box environment (with a `group` each), so one
deploy covers both: the new box creates the containers, and the old box
removes the ones it no longer runs. When the old box is in another box
environment, `bay up` deploys that one too.

A rollback across a move is a move back: it needs `--data keep` again.

### The whole environment

`bay up` always deploys a whole box environment. `bay plan <env>` can show
all of it. Run it with no `--project` and no `bay.toml` here or above, in the
fleet directory or with `--fleet`, `BAY_FLEET` or `BAY_FLEET_NAME`:

```bash
cd ~/fleets/prod && bay plan production
bay --fleet ~/fleets/prod plan production --json
```

- Every project with `[deploy.<env>]` is read at its WANTED commit: a project
  in the fleet at the last fleet commit of its folder, a repo project at the
  head of its repo (the repo cache). The fleet is compiled once.
- Every step carries the project it belongs to. Risk, verdict and exit code
  work as for one project.
- The plan record has `project: null` and a list `projects`: per project its
  `name`, `box`, `box_env`, `wanted` and `pinned`. The top-level `wanted` and
  `pinned` hold no commit. A plan is stale when any project's lock moved.
- The projects must share one box environment. When they do not, the plan is
  blocked: plan them one at a time with `--project`.

`bay up <env>` in the same place applies such a plan (or `--plan-id <id>`
names a saved one). It pins every project of the plan to its WANTED commit
and commits once: `bay: up <env> (<n> projects)`. A `--plan-id` of a
one-project plan given from the fleet directory applies that project.

In an app repo, `bay plan <env>` keeps its one-project meaning.

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
   `bay up --plan-id` plans again with the same box check the saved plan used.
2. Bay refuses `blocked` (exit 20) and `stale` (exit 30). Bay refuses
   `approve` (exit 10) unless an approval matches. `--force --reason "<why>"`
   overrides `approve` only, never `blocked` or `stale`. The reason goes into
   the lock as `previous.force_reason`. Bay also refuses a repo project's
   commit that is on no branch of its remote: "push first". The box can only
   build what the remote has.
3. Bay writes the lock: the project pin moves to the planned commit. The
   environment record gets `result: pending` and `previous` (the pin it
   replaces).
4. Bay compiles the fleet into its services file, with the hash header.
5. Bay commits the fleet repo: `bay: up <name> <env> <short sha>`, and
   prints the fleet commit.
6. Bay runs today's deploy for the box's environment, limited to
   `--tags deploy_stack` (the same work as `bay deploy <env> --tags deploy_stack`,
   and the same tag the box check of `bay plan` runs). The `git_deploy` role
   renders the webhook rebuild script under that tag, so a later webhook build
   uses the deployed config, not the config from before this `up`. The tag does
   not clone, build or pull anything.
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
   Then Bay prunes `plans/` (see [plans/ prune](#plans-prune)). `pruned` in
   the JSON result lists the removed files.
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

#### plans/ prune

`bay up` commits its plan record to `plans/<plan-id>.json`, so `plans/` is
tracked and grows with every deploy. Do not delete it by hand: an uncommitted
removal makes the next receipt record `fleet_dirty: true`.

After the receipt commit, `bay up` prunes it:

- It keeps the 50 newest plan records (by `created_at`) and every record a
  lock still names: `envs.<env>.plan_id` and `envs.<env>.previous.plan_id` of
  any project.
- It removes the rest with `git rm`, in one commit:
  `bay: prune plans (<n> files)`. A removed record's approval file
  (`<plan-id>.approved`) goes with it.
- It touches only files git tracks. A plan that `bay plan` saved and no
  `bay up` applied stays an untracked file. Remove those by hand when you
  like; never remove a tracked record by hand.

### bay rollback

Bay takes the environment's `previous` commit and runs `bay up` with it. The
two pins swap, so a second rollback undoes the first. Bay refuses when there
is no `previous`. The output names the old and the new commit and the steps.

A rollback restores config **and** code:

- Config: the pin moves back, and Bay compiles that commit's `bay.toml`.
- Code: the box points `:latest` of every build container of the project at
  the image that the previous receipt (`<env>.prev.json`) names, then the
  deploy runs. The result lists this in `code_targets`.
- Freeze: Bay sets `frozen = true` (and `frozen_commit`) on the environment in
  the lock. While the environment is frozen, a push builds and tags its image,
  but does not deploy it, whatever `track` says. The alert `build.held` says
  so. The next `bay up` to a newer commit (a descendant of `frozen_commit`)
  clears the freeze. A `bay up` to the same or an older commit keeps it.

`bay rollback --to <commit>` rolls the code back to that commit's image. Bay
asks the box first (`docker image ls`). When `<image>:<commit12>` is not on the
box, Bay refuses before anything moves, and the message lists the commit tags
that the box has. For a project whose `bay.toml` lives in the app repo, the pin
moves to `<commit>` too. A project in the fleet keeps its pin (its pin is a
fleet commit), and only its code moves.

### Code and config

The pin is config: the `bay.toml` that `bay up` compiled. The code is the
image that a container runs. Every build is tagged `<image>:<commit12>`, and
the image carries the label `com.bay.commit`. The receipt names the commit and
the image of every container (see [deploy-receipt.md](deploy-receipt.md)).

`[deploy.<env>] track` decides what a push does (see
[bay-toml.md](bay-toml.md)):

- `branch` (the default): a push deploys new code under the pinned config. The
  code commit and the config commit can then differ, and that is expected.
  `bay plan` prints one information line, `code at <commit>, config pinned at
  <commit>`. It is not a step and does not change the plan id.
- `pin`: only `bay up` deploys code. When a container runs another commit than
  the one `bay up` would pin, `bay plan` shows a step of kind `image`, action
  `update`, risk `safe`.

`bay up` of a project whose `bay.toml` lives in the app repo asks the box to
point `:latest` at the pinned commit's image (`bay_code_targets`, run by
`python -m bay_reconcile.codepin` before the container pass). This is how
`bay up` releases a held build. In `pin` mode the image must be on the box, or
the deploy stops before any container changes, with the commit tags that the
box has. In `branch` mode a missing image is skipped, and `:latest` stays
where the last push put it. A project in the fleet passes no code targets.

Note: in `branch` mode `bay up` deploys the commit of the checkout it reads.
When that checkout is older than the code that a push already deployed, the
box goes back to the older image. Pull the app checkout before `bay up`.

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
`bay compile --working-tree` reads the fleet and the checkout you stand in as
they are instead. A repo project you do not stand in is not read, and the
compile names it. Use it while you develop, never to deploy.

## The project folder

From 2.1 (`format = 2` in `bay.fleet.toml`), a project is one folder:

```
projects/
├── shop/
│   └── bay.lock           # a repo project: only its lock lives here
└── gatus/
    ├── bay.toml
    ├── bay.lock
    └── config.yaml        # from = "config.yaml"
```

- `from =` in a mount is relative to the directory of the `bay.toml`, in the
  fleet and in an app repo.
- `from = "fleet:<path>"` mounts the shared fleet file `files/<path>`.
- The box keeps the path it has today: `config/<target>`, where `<target>`
  is the adopted path in the lock or `<name>/<from>`. So the move recreates
  no container.
- Bay copies each mounted file into the temporary fleet as `files/<target>`.
  The copy is made at the commit Bay reads, so an uncommitted file is not
  part of the plan.
- `files/` stays for rig files, `[resources.*]` files and shared files.

**The deploy reads the scratch copy.** The deploy reads mounted files from the
scratch copy at the pinned commit; uncommitted files are not deployed. `bay up`
keeps the temporary fleet until its deploy has finished and passes its
`files/` as `bay_config_files_root`. The box check of `bay plan --remote` passes
the same, so the prediction and the deploy read the same bytes. The plan lists
each uncommitted file under `files/` or in a project folder in a note:
`uncommitted file <path> is not deployed`. A plain `bay deploy` passes no such
variable and copies from the fleet's `files/` as before.

**One release of overlap.** Both places work in 2.1. A file that is not beside
the toml is read from its old place, `files/<name>/<from>`, with a note that
names it. Move it beside the toml with `git mv` when you are ready.

**The move to format 2.** The first `bay plan`, `bay up`, `bay show`,
`bay compile` or `bay init` of a 2.1 CLI moves each `projects/<name>.lock` to
`projects/<name>/bay.lock` (`git mv`), adds `format = 2` after the `name`
line of `bay.fleet.toml`, and commits once:
`bay: move locks into project folders`. It refuses when one project has both
lock forms, and when `bay.fleet.toml` has uncommitted changes. A CLI refuses
a fleet whose `format` is newer than it knows.

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
  The image is the reference the deploy asked for (`image_ref`), so a webhook
  build that stamps a new commit into the receipt is not drift either.
- `kind` is one of `container`, `volume`, `database`, `database_user`,
  `secret`, `resource`, `tailnet`, `fleet`, `image` (`track = "pin"` only,
  see "Code and config"). `action` is one of `create`,
  `update`, `remove`, `rename`, `move`, and for a box step also `recreate`
  and `start`.
- `source` is `compile` for a step from the compiled diff, `box` for a step
  from the box prediction.
- `box_prediction` is empty without `--remote`.
- `plan_sha256` is the SHA-256 of the plan without `plan_id`, `plan_sha256`,
  `created_at`, `verdict`, `exit_code`, `approval`, `stale` and `notes`
  (notes are text for the reader and never change a deploy). `plan_id` is
  its first 12 hex digits. The same inputs give the same id.
- A plan holds secret names, never values.

## The lockfile

`projects/<name>/bay.lock` (schema `src/bay_cli/schemas/bay_lock.schema.json`),
version 2. Only the CLI writes it, atomically. It holds `repo` (the clone URL,
or `null` for a project that lives in the fleet), `toml_path` (default
`bay.toml`), the project pin `commit`, and `envs`. It names no path on any
machine. A version 1 lock is read as version 2: its `local_path` is dropped,
and the next write stores version 2.

`bay up` adds these optional keys to an environment:

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
- `frozen` and `frozen_commit` are written by `bay rollback` and removed by
  the next `bay up` to a newer commit. While `frozen` is true, the compile
  writes `build.frozen: true`, and a push builds without deploying.
- `box` is written by the first `bay up`. After that the fleet decides: a new
  `box` in `bay.toml` gives a note, not a move.
