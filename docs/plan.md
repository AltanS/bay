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
bay adopt <name> [--toml-path P] [--check] [--json]   # move an in-fleet bay.toml into its app repo
bay remove <name> [--env E] [--json] [--remote] [--no-remote]   # plan taking a project out; bay up --plan-id applies it
```

`env` is the `[deploy.<env>]` name. The default is the fleet's primary
environment (`production`).

With `--json`, stdout holds exactly one JSON document, also for an error
(`{"error", "hint", "code"}`). Progress lines and everything the deploy
prints, Ansible included, go to stderr. With `--log <path>`, they are
appended to that file instead, with or without `--json`.

The pin is a config pin. Bay compiles `bay.toml` at the pinned commit. The
code is a separate thing: `[deploy.<env>] track` decides whether a push moves
it, and `bay up` and `bay rollback` can move it too (see
[Code and config](#code-and-config)). The `image` in the box receipt shows what
really runs.

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
   in the repo cache at the head of the branch that `[deploy.<env>].branch`
   names (the branch the webhook builds; read from the pinned `bay.toml`,
   else the cache's HEAD). With no `branch` declared, the cache's HEAD: the
   remote's default branch. `--at` picks another commit. Uncommitted edits are not part of the plan. The plan
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
| Tailnet route added, changed or removed (`kind: route`, see [tailnet-ingress.md](tailnet-ingress.md#routes-in-bayfleettoml-21)) | shared |
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
later, when you no longer need the data. The notes name a volume as Docker
knows it on the old box, `<stack_name>_<volume>`: `stack_name` from
`group_vars/<box env>/`, else `group_vars/all/`, else `bay`. The `volumes`
list in `moves` keeps the bare name.

**`--data move` (planned) is deferred.** Bay does not copy volumes or databases between
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
  head of its deploy branch in the repo cache (or HEAD of the checkout you
  stand in). The fleet is compiled once.
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
   The lock migration to project folders (a pure rename plus one `format` line)
   does not make a plan stale.
2. Bay refuses `blocked` (exit 20) and `stale` (exit 30). Bay refuses
   `approve` (exit 10) unless an approval matches. `--force --reason "<why>"`
   overrides `approve` only, never `blocked` or `stale`. The reason goes into
   the lock as `previous.force_reason`. Bay also refuses a repo project's
   commit that is on no branch of its remote: "push first". The box can only
   build what the remote has. The one exception is the `bay adopt` commit
   (`adopted.app_commit` in the lock): `bay up` takes it unpushed and moves no
   code for it (see [bay adopt](#bay-adopt)).
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
   not clone, build or pull anything. When the plan has a `route` step, Bay
   runs `--tags deploy_stack,headscale,traefik`: Headscale renders the
   split-DNS records and Traefik the route file. A route change is blocked in
   a plan for a box env other than the ingress box's.
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
  deploy runs. The result lists this in `code_targets`. When the box cannot
  do it, the container keeps its image and the result says so: `code_kept`
  in the JSON, and `code: kept <container> (<reason>)` in the output. The
  usual reason is a previous receipt from before 2.1, which names no commit
  ("the previous receipt names no commit for this container"); then only the
  config rolled back. Use `bay rollback --to <commit>` to move the code.
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

Straight after `bay adopt`, `bay rollback` is refused with "the previous pin
is a fleet commit; use `bay up --at <commit>`". The adopt clears `previous`,
because an app repo pin cannot go back to a fleet commit. The refusal holds
until a `bay up` to a newer app commit records a `previous` again.
`bay rollback --to <commit>` (code only) still works.

### bay adopt

`bay adopt <name>` moves a project's `bay.toml` out of the fleet and into its
app repo. Run it inside a checkout of the app repo. The `bay.toml` is not
there yet, so it cannot name the fleet: pick the fleet with `--fleet`,
`BAY_FLEET` or `BAY_FLEET_NAME`.

```bash
cd ~/code/shop
bay --fleet ~/fleets/prod adopt shop --check   # the files and the lock change; changes nothing
bay --fleet ~/fleets/prod adopt shop
bay plan production                            # must show 0 steps
bay up production                              # takes the unpushed adopt commit
git push                                       # config only: the box does nothing
```

The order is adopt, `bay up`, then `git push`. Before the adopt the project
lived in the fleet, so the box's `rebuild.sh` has no `bay.toml` path and no
pinned hashes, and treats any push as code: pushed first, the adopt commit
would build the app and recreate its containers. So `bay up` comes first. It
accepts the adopt commit before it is pushed (the one exception to "push
first": the lock records it as `adopted.app_commit`), and it moves no code
for it: no code target, the running image stays. The box only gets the new
`rebuild.sh` with `bay_toml_path`, `bay_toml_hash`, `bay_build_hash` and
`bay_toml_files`, and the new config. Run `bay up` in the app checkout: the
adopt commit is not in the fleet's repo cache yet. Then push. The adopt
commit changes only the `bay.toml` and the files beside it, so the new script
sees a config-only push (see [build-pipeline.md](build-pipeline.md),
"Config-only push"): it tags the running image with the adopt commit and ends
with exit 0. The next `bay plan` shows zero steps. Any other unpushed commit
is still refused.

Bay refuses, before it changes anything, when:

- the fleet has no `projects/<name>/bay.toml` (the project is not in the
  fleet, or it already lives in its repo);
- the working directory is not a git repo, or the repo has no `origin` remote
  or no commit;
- a file exists at `--toml-path` (default `bay.toml` at the repo root);
- the app repo has uncommitted changes, so the adopt commit holds only the
  move;
- `projects/<name>/`, or a `files/` copy that a mount reads, has uncommitted
  changes in the fleet;
- `name` in the `bay.toml` is not `<name>`, or the `bay.toml` is not valid;
- the lock names another `repo` than the checkout's `origin`;
- the fleet changed `projects/<name>/` after `bay up` pinned it (WANTED is not
  PINNED). Run `bay up` first, so the adopt moves exactly what runs;
- an environment is frozen by `bay rollback`;
- two copies of one file would land at the same place in the app repo, or a
  different file is already there.

What it does:

1. **Copy.** `projects/<name>/bay.toml` goes to `--toml-path`. Every other
   file of the folder goes beside it, at the same relative path, so
   `from = "deploy/x.yaml"` still reads `deploy/x.yaml` beside the toml. A
   mount that the compiler reads from `files/` (the old place
   `files/<name>/<from>`, or an adopted path) moves beside the toml too. A
   `fleet:` mount stays in the fleet. A `files/` copy that another fleet entry
   also reads stays as well (`kept` in the output).
2. **App commit.** One commit with exactly those files:
   `chore: add bay.toml (adopted from fleet <fleet>)`. It stays local. You
   push it after `bay up`, which accepts this one commit unpushed.
3. **Lock.** `projects/<name>/bay.lock` takes the repo form: `repo` (the
   lock's own `repo` when it already named this repo, else the `origin` URL),
   `toml_path`, and `commit` (the app commit). Every environment with a pin
   moves its `commit` to the app commit. Every environment gets
   `adopted.from_fleet_commit` (the fleet HEAD before the adopt) and
   `adopted.app_commit` (the adopt commit), and loses `previous`. Every other adopted name stays, so no container, volume,
   database or config path is renamed.
4. **Fleet commit.** `git rm` of the folder contents (the lock stays) and the
   moved `files/` copies, plus the new lock, in one commit:
   `bay: adopt <name> into <repo>`. Bay never pushes.
5. **Next steps.** Bay prints them: `bay plan <env>` (it must show 0 steps),
   then `bay up <env>`, then `git push`.

The compiled output does not change. On the box a mounted file stays at
`config/<name>/<from>` (or `config/<adopted path>`), wherever the file lives.
A project that builds from source gains `build.bay_toml_hash`,
`build.bay_build_hash`, `build.bay_toml_path` and `build.bay_toml_files` (the
hold guard and the config-only rule, see [Code and config](#code-and-config)). The container hash
leaves the `build` table out, so the plan shows no step for that: the first
`bay up` after the adopt writes them to the box.

`--toml-path services/shop/bay.toml` is for a repo with several apps: the
files then land in `services/shop/`. `--check` prints the file list and the
lock diff and writes nothing. `--json` prints one document with `files`,
`removed`, `kept`, `lock_diff`, `app_commit`, `fleet_commit` and `next`.

### bay remove

`bay remove <name>` takes a project out of the fleet. `bay remove <name> --env
<env>` takes out one environment of it. Like every change, it is a plan first:

```bash
bay --fleet ~/fleets/prod remove shop                 # saves a plan, exit 10 (approve)
bay --fleet ~/fleets/prod approve 3f2a9c0d1e2b --reason "shop is retired"
bay --fleet ~/fleets/prod up production --plan-id 3f2a9c0d1e2b
```

**The plan.** Bay compiles the fleet without the project (or without that
environment of it) and compares the result with the services file. Each
container of the project that the fleet compiled or the box runs becomes one
step: `kind: container`, `action: remove`, risk `destructive`. The reason of
each step names the volumes and the database that stay (`volume a stays`,
`volumes a, b stay`, and the same for databases). So the verdict is `approve`.
The plan record has a `remove` block (see [The plan JSON](#the-plan-json)):
the containers per environment, every named volume by its name on the box
(`<stack_name>_<volume>`), and the database and its role (the adopted names
from the lock, else the derived ones). `bay plan --plan-id <id>` checks a saved
remove plan again.

**The box check.** `bay remove` takes the same flags as `bay plan`, with the
same defaults. Without `--remote`, the plan reads the box receipt (RUNNING) and
does not run the check mode: `box_checked` is `false`, and a note says so.
`--no-remote` skips the receipt too. With `--remote`, Bay runs the check mode of
the deploy on each box the project runs on, against the compiled file
*without* the project, as `bay plan --remote` does. The reconciler lists a
managed container that the file no longer holds as `remove` (reason `orphan`).
Each remove step then takes the box's words into its reason and gets
`source: box`, and `box_checked` and `box_prediction` are filled. When the box
does not predict a `remove` for a step (the container does not run there, or
the box runs an older Bay), the step stays, and a note names it: that step
rests on the receipt alone. Any other change the box predicts becomes a
`source: box` step, as in a normal plan. `bay plan --plan-id` and `bay up
--plan-id` repeat the check when the saved plan has it.

**Bay never deletes data.** The containers stop and leave. The volumes and the
database stay where they are. The plan and `bay up` print the lines to delete
them, marked "run by hand when you are sure":

```text
box eu-1: docker volume rm acme_shop-data
box eu-1, resource postgres: DROP DATABASE shop; DROP ROLE shop;
```

Run the SQL line in the postgres resource of that box. Take a backup first if
you may need the data again.

**Apply.** `bay up <env> --plan-id <id>` applies the approved plan:

1. Plan again. A blocked or stale plan is refused, and an unapproved one too.
2. The lock: each environment that leaves gets `result: pending`.
3. Compile the fleet without the project, and commit:
   `bay: remove <name> (plan <id>)`.
4. Deploy each box environment the project ran on. The reconciler removes
   every container that is no longer in the compiled file.
5. Read the receipt. It must be from this deploy and must not list a container
   of the project, other than as `action: remove`.
6. Confirmed: the environment leaves the lock. With no environment left,
   `projects/<name>/` leaves the fleet (`git rm`, so the history keeps it):
   `bay: remove <name>: the receipt confirms it`. Not confirmed: the
   environment stays in the lock with `result: failed`, and the command exits
   1. Run `bay remove <name>` again: the new plan finds the containers in the
   receipt.
7. Prune `plans/` and push the fleet, as `bay up` does.

**One environment.** `--env <env>` is blocked while `bay.toml` still has
`[deploy.<env>]`, because the next `bay up` would start it again. Delete that
table, commit it (and push it, for a project in an app repo), then run
`bay remove <name> --env <env>`. The plan reads `bay.toml` at that commit, and
`bay up` moves the lock's pin to it. The other environments do not change.

Bay refuses or blocks a remove when:

- the name is a `[resources.*]` entry, or a container that Bay runs on every
  box itself (the proxy, the IDS, the update watcher, the gateway, the
  registry, the webhook receiver). Bay refuses these at once.
- another project needs the project (`needs` in its `bay.toml`). Take that
  need out and run `bay up` for that project first.
- the compile without the project would change anything else: another
  project, a route, the webhook or the tailnet allowlist. Apply that with
  `bay up` first, so the remove plan holds only the removal.
- the fleet is behind its remote, or the fleet repo has uncommitted changes.

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
  <commit>`. It is not a step.
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

In `branch` mode `bay up` applies the config at the pin and moves the code
only forward. For each build container, Bay compares the commit in the
receipt with the commit that `bay up` pins. It reads the app checkout first,
then the fleet's repo cache (fetched when the checkout does not know a
commit), with `git merge-base --is-ancestor`:

| The running commit is | `bay up` does |
|---|---|
| newer than the pin (a push deployed it after the pin) | keeps the code: no code target, `:latest` stays. The plan prints `code at <running>, config pinned at <pin>` and lists the container in `code.keep`. Only the config changes. |
| the same as the pin, or older (a held build, the first deploy) | points `:latest` at the pin's image, as above |
| unknown (a commit in neither the checkout nor the cache, or on another branch) | refuses: `cannot order <pin> and <running>; fetch the repo or pass --force-code` |

`bay plan --force-code` and `bay up --force-code` turn the refusal into a step
of kind `image`, action `update`, risk `destructive`: the code moves to the pin
even when it may be older than what runs. Like any destructive step it needs
`bay approve` or `--force --reason`. So a `bay up` from a stale checkout never
moves the running code to an older image. That holds for a whole-environment
`bay up <env>` too: its plan runs the same check per project, prefixes the
notes and blockers with the project name, and carries the union of the kept
containers in `code.keep`. Two exceptions move code backwards on purpose:
`pin` mode and `bay rollback` (below). The `bay adopt` commit moves no code
at all.

In `pin` mode the code follows the pin exactly, backwards included: that is
what `pin` means. `bay rollback` also skips the order check, because it moves
the code back on purpose.

The plan field `code` (`{"keep": [<container>, ...]}`) is part of the plan
body, so it changes the plan id. It is absent when `bay up` keeps no code.

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

`bay show --routes` prints the fleet's tailnet routes instead, each with
WANTED (`bay.fleet.toml`), PINNED (the compiled `tailnet_proxies`) and RUNNING
(the routes the ingress box receipt lists) and one status: `ok`, `pending`
(the fleet file differs from the compiled file), `drift` (the box serves
something else) or `unknown` (no receipt lists routes). No receipt lists
routes yet, so RUNNING is `unknown` for every route today, and `bay status`
does not show routes at all.

## bay compile

`bay compile` reads every project at the commit its lock pins, through the
same temporary copy of the fleet that `bay plan` and `bay up` compile. A
project with no pinned commit is left out, with a note on stderr.
`bay compile --working-tree` reads the fleet and the checkout you stand in as
they are instead. It reads only that one checkout: a repo project you do not
stand in is not read (not from the repo cache either), and the compile names
it. Use it while you develop, never to deploy.

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

**The move to format 2.** Only a verb that writes the fleet moves the locks:
the first `bay up`, `bay rollback`, `bay compile`, `bay adopt` or `bay init`
of a 2.1 CLI. It moves each `projects/<name>.lock` to
`projects/<name>/bay.lock` (`git mv`), adds `format = 2` after the `name`
line of `bay.fleet.toml`, and commits once:
`bay: move locks into project folders`. It first checks that the fleet is
not behind its remote, and refuses when it is ("pull it first") or when the
remote cannot be read: the move on a stale clone would fork the fleet
history. It refuses when one project has both lock forms, and when
`bay.fleet.toml` has uncommitted changes. A reader (`bay show`, `bay plan`)
never moves anything: it reads either lock form and prints one stderr line,
`layout: migration to project folders pending (bay up or bay compile does it)`.
`bay route` does not read or write locks, so it does not move them. A CLI
refuses a fleet whose `format` is newer than it knows.

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
  `secret`, `resource`, `tailnet`, `route`, `fleet`, `image` (`track = "pin"`
  only, see "Code and config"). `action` is one of `create`,
  `update`, `remove`, `rename`, `move`, and for a box step also `recreate`
  and `start`. A `route` step has `route_added`, `route_changed` or
  `route_removed`, and names the route in `resource`.
- `source` is `compile` for a step from the compiled diff, `box` for a step
  from the box prediction.
- `box_prediction` is empty without `--remote`.
- `plan_sha256` is the SHA-256 of the plan without `plan_id`, `plan_sha256`,
  `created_at`, `verdict`, `exit_code`, `approval`, `stale` and `notes`
  (notes are text for the reader and never change a deploy). `plan_id` is
  its first 12 hex digits. The same inputs give the same id.
- A plan holds secret names, never values.
- Only a `bay remove` plan has `remove`: `project`, `scope` (`project` or
  `env`), `in_fleet`, `commit` (with `--env`: the commit the pin moves to),
  `envs` (per environment `env`, `box`, `box_env` and every container name,
  which the receipt must confirm gone), `volumes` (`env`, `box`, and `name` as
  the box names it), `databases` (`env`, `box`, `resource`, `name`, `role`)
  and `cleanup` (the lines to run by hand). See [bay remove](#bay-remove).

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
- `adopted.from_fleet_commit` is written by `bay adopt`: the fleet commit the
  project was read from before its `bay.toml` moved into the app repo. While
  an environment has it and no `previous`, `bay rollback` is refused.
  `adopted.app_commit` is the adopt commit in the app repo: `bay up` accepts
  that commit before it is pushed and passes no code target for it.
- `frozen` and `frozen_commit` are written by `bay rollback` and removed by
  the next `bay up` to a newer commit. While `frozen` is true, the compile
  writes `build.frozen: true`, and a push builds without deploying.
- `box` is written by the first `bay up`. The compiler uses it. A different
  `box` in `bay.toml` gives a `move` step in the plan (see [Box move](#box-move)),
  and `bay up` writes the new box here.
