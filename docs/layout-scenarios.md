# Bay layout scenarios

Three kinds of place hold Bay files. Each scenario below shows all three.

| Place | Who writes it | What it holds |
|---|---|---|
| Machine | `bootstrap.sh` (the install; `uv tool install --editable` alone cannot deploy), `bay fleet init` | the CLI, the fleets, caches |
| Fleet repo | you (`bay.fleet.toml`, vault, hosts) and the CLI (locks, plans, compiled file) | boxes, secrets, shared resources, one folder per project |
| App repo | you | `bay.toml` next to the code, plus any files it mounts |

Rule of thumb: **an app that has a repo keeps its `bay.toml` in that repo.** An app with
no repo (an off-the-shelf image such as gatus) lives inside the fleet as
`projects/<name>/bay.toml`. Both kinds have `projects/<name>/bay.lock` in the fleet.

Everything here is valid in Bay 2.1 and the files pass `bay toml validate`. A feature that
Bay does not have yet carries the tag `(planned)` on its line. Some valid keys are also not
deployable yet: `bay plan` blocks a file that uses one (the `unsupported` list) unless you pass
`--allow-unsupported`. The list is in [plan.md](plan.md#features-bay-cannot-deploy-yet), and
[bay-toml.md](bay-toml.md#keys) tags each such key `(validated, not deployed yet)`. Scenarios
3, 8 and 9 use some of those keys and say so. Scenario 5 shows the cross-box need, which is on
the same list. Every other file in this page plans clean.

Names below are examples. `acme` is the fleet. `eu-1`, `eu-2`, `infra` and `na-1` are
boxes. `shop` and `blog` are apps.

## What `bay up` does

`bay up <env>` deploys the whole box environment, not one project. It runs from an app
repo or from the fleet. It commits the plan record under `plans/` and the receipt, and it
pushes the fleet. A project is covered when it has a `[deploy.<e>]` table (any name `<e>`)
on the same box environment as the plan. What gets pinned depends on where you run it:

| You run | Pinned to a new commit | Other covered projects |
|---|---|---|
| In an app repo, or with `--project shop` | `shop`, at the planned commit | keep their own pin and deploy at it |
| In the fleet directory, no `--project` | every covered project, each at its own WANTED commit | none: all of them move |

`bay plan production` with no project, run in the fleet directory, previews the whole env
(see [plan.md](plan.md#the-whole-environment)). A fleet inside a bigger repo is not pushed
(scenario 6). The first image of an app that builds from source is not made by `bay up`
(see [plan.md](plan.md#the-first-image)).

## Words used in the scenarios

One line each. The full table is in [Words](#words-deploy-env-box-env-and-group) below.

- **Fleet**: the repo that holds the boxes, secrets, shared resources and one folder per project.
- **Project**: one app. Its `bay.toml` is in its own repo or in the fleet. The fleet keeps its lock.
- **Lock**: `projects/<name>/bay.lock`. The CLI writes it. It pins the commit that was deployed.
- **Box**: one server, named in `[boxes.<name>]` of `bay.fleet.toml` (`eu-1`).
- **Box env**: the `env` of a box (`production`). It names `hosts/<env>`, `group_vars/<env>/` and the receipt file.
- **Deploy env**: the `[deploy.<env>]` table of a `bay.toml`. `bay plan <env>` and `bay up <env>` take this name.
- **Group**: the `group` of a box, the `[eu]` heading in the hosts file. It separates boxes of one box env.
- **Primary env**: `primary_env` of `bay.fleet.toml`, default `production`. It is the deploy env with bare container names (`shop`, not `shop-staging`).
- **WANTED, PINNED, RUNNING**: the `bay.toml` at the latest commit, the commit in the lock, and what the box receipt says runs. See [plan.md](plan.md).

## Sample `bay plan` output

`bay plan production --json` prints one document. This is the real shape, trimmed.

```json
{
  "plan_id": "3f2a9c0d1e2b",
  "project": "shop",
  "env": "production",
  "box": "eu-1",
  "wanted": {"commit": "0123456789ab", "dirty": false},
  "pinned": {"commit": "89abcdef0123"},
  "steps": [
    {"id": "s1", "kind": "container", "container": "shop", "resource": null,
     "project": "shop", "action": "update", "risk": "safe",
     "reason": "changed: env", "source": "compile"},
    {"id": "s2", "kind": "resource", "container": "postgres", "resource": "postgres",
     "project": null, "action": "update", "risk": "shared",
     "reason": "changed: memory", "source": "compile"}
  ],
  "blockers": [],
  "verdict": "approve",
  "exit_code": 10
}
```

`risk` is `safe`, `shared` or `destructive`. `shared` means a fleet-wide thing: a
`[resources.*]` entry, the webhook, the tailnet allowlist, a route, or a container that no
project owns. `destructive` removes data or a container. A step of another project carries
that project in `project` and keeps its own risk, so two safe changes give `auto`.
`source` is `compile`, or `box` (only with `--remote`). The `bay plan` table prints a
`PROJECT` column.

| Verdict | Exit | What you do |
|---|---|---|
| `auto` | 0 | Run `bay up`. First read every step with `source: box` and `project: null`: the box predicted it for a container that no project owns (a shared postgres, for example), and Bay rates such a step `safe` whatever it does to a shared container. It asks no approval, so the `reason` is the only warning. |
| `approve` | 10 | Read the steps. Run `bay approve <plan-id> --reason "<why>"`, then `bay up <env> --plan-id <plan-id>`. A plain `bay up` plans again, and it applies the approval only when nothing moved and the plan used no `--remote` (see [plan.md](plan.md#the-box-prediction---remote)). |
| `blocked` | 20 | Fix what `blockers` says. Approval cannot help. |
| `stale` | 30 | Something moved since the plan. Run `bay plan` again. |

## Words: deploy env, box env and group

The one-line list is near the top of this page. This is the full table. Four words name four different things. The docs use each one in one sense only.

| Word | Where it is set | What it is |
|---|---|---|
| Box | `[boxes.<name>]` in `bay.fleet.toml` | One server. The name (`eu-1`) is a label: `box = "eu-1"` in a deploy table, the lock, `default_box`, a resource's `box` list and `ingress_box` use it. Bay never reads `hosts/` to find a box. The compiler writes only the `group` of the box, and the deploy puts a container on the inventory hosts that are in that group. The `box` field of a receipt, and the `box` that a plan prints for a box prediction, is the `inventory_hostname`: the name at the start of the line in `hosts/<env>` (`eu-1 ansible_host=...`). Give the host line the same name as the box, so that one name shows everywhere. No check enforces it. |
| Box env | `env` of the box | The box environment. It names the inventory file `hosts/<env>`, the folder `group_vars/<env>/` (the secrets), and the receipt `/var/lib/bay/receipts/<env>.json`. Every command passes it to Ansible as a host pattern, so a group (or host) with this name must exist in the inventory. A hosts file with only `[eu]` and `[eu2]` headings has no such group: add `[<box env>:children]` and list the groups under it (scenario 11). `bay provision <box env>` and the deploy of `bay up` run on every host of that group. |
| Group | `group` of the box | The inventory group: the `[eu]` heading inside the hosts file. It limits a container to the boxes of that group. |
| Deploy env | `[deploy.<env>]` in a `bay.toml` | An environment of one app: own box, data and container suffix, and its own record in the lock. `bay plan <env>` and `bay up <env>` take this name. Its secret values come from the box env of its box (below). |

The deploy env and the box env often have the same name (`production` on production boxes,
`staging` on the staging box), but they are two keys. Bay finds the box env from the box that
the deploy table names. `primary_env` in `bay.fleet.toml` is a deploy env: the one that gets
bare container names. Its default is `production`.

### Which env does a verb take?

| Verb | The env is a | Bay uses it for |
|---|---|---|
| `bay plan`, `bay up`, `bay rollback` (`[env]`, default `primary_env`), `bay remove --env` | deploy env | the `[deploy.<env>]` table of each `bay.toml` |
| `bay approve <plan-id>`, `bay show [name]` | none | `show` prints the status of every deploy env of the project |
| `bay deploy <env>`, `bay provision <env>` | box env (or a group name inside the hosts file) | the Ansible host pattern: the hosts of that group. With two boxes in one box env, `bay provision production` runs on both. `bay provision eu2` (a group) or `bay provision production -- --limit eu-2` (a host) runs on one. For a deploy, use the `--limit` form: `bay deploy eu2` writes the receipt as `eu2.json`, which `bay status` and `bay plan` do not read |
| `bay validate --env <env>` | box env | `hosts/<env>` and its vault |
| `bay vault <verb> <env>`, `bay secret missing <env>` | box env | `group_vars/<env>/secrets.yml`, else `group_vars/all/secrets.yml` |
| `bay doctor [env]` | box env | the vault file and the box receipts. The default is `primary_env`, read as a box env name: pass the box env when the two differ. |
| `bay status --env <env>` | box env | the receipts to read |
| The receipt file | box env | `/var/lib/bay/receipts/<box env>.json`, written by every deploy of that box env, one per box env |

**Which verbs find the fleet from the directory you stand in.** Only `plan`, `up`, `approve`,
`rollback`, `remove`, `doctor` and `show <name>` do (rule 5 of [install.md](install.md#pick-a-fleet)).
Inside the fleet directory, every other verb (`deploy`, `provision`, `vault`, `secret`, `validate`,
`status`, `route`, `compile`, `adopt`, `init`) stops with "no fleet selected" unless you name the fleet
with `--fleet <path>` or `BAY_FLEET`. The commands below that start with `bay --fleet $F` do that.
One exception: a few older verbs take the working directory as the fleet when nothing else names
one: `bay service add|edit|remove` (they write `group_vars/all/services.yml` directly, and the next
`bay compile` then refuses that generated file as edited by hand), `bay service prune-webhooks`, and
`bay server add|remove` (they write the hosts files and `group_vars/`). See rule 5 of
[install.md](install.md#pick-a-fleet).

**Secret values are per box env.** They live in `group_vars/<box env>/secrets.yml`, one
encrypted file for the whole box env. Two deploy envs on one box env share that file: one key
name has one value for both. `bay secret missing <box env>` compares that file with every
secret name that any project of the fleet needs. It does not filter by deploy env. To give two
deploy envs different values, put their boxes in different box envs (scenario 4), or use
different key names.

Give each box of one box env its own `group`, and a matching heading in the hosts file.
Boxes that share a group would each run the containers of both. A box that is alone in
its box env may leave `group` out.

## 0. The machine

```
~/.local/bin/bay                          # the CLI, one per machine
~/.config/bay/fleets/
└── acme/                                 # clone made by `bay fleet init acme --from <url>`
~/code/
├── shop/                                 # app repo with bay.toml (scenario 2)
└── blog/                                 # app repo with bay.toml (scenario 3)
```

Bay picks the fleet in one fixed order of five rules: `--fleet <path>`, `BAY_FLEET=<path>`,
the `fleet = "acme"` line of the `bay.toml` you stand in, `BAY_FLEET_NAME=acme`, and last the
fleet directory you stand in (for `plan`, `up`, `approve`, `rollback`, `remove`, `doctor` and
`show <name>` only). The rules, and the list of verbs that use the last one, are in
[install.md](install.md#pick-a-fleet). `bay init`
and `bay adopt` have no `bay.toml` to read yet, so they do not use the third rule. A stray
`BAY_FLEET` in your shell profile beats the `bay.toml` line. A command that changes something
prints `fleet: acme (<path>)` first on stderr, so you see which fleet won. A command that
only reads (`show`, `status`) does not print it. The folder `acme`, the `name` in
`bay.fleet.toml` and the `fleet =` line must be the same word
([install.md](install.md#the-three-names-of-a-fleet)).

**Fleet layout migration.** Pull the fleet first, before any `bay up` on a fleet made before
2.1: a writer checks that the fleet is not behind its remote, and refuses when it is. A fleet made before 2.1 keeps its locks in
`projects/<name>.lock`. Readers (`bay show`, `bay plan`) never move them. They read both
forms and print `layout: migration to project folders pending (bay up or bay compile does
it)`. A writer (`bay up`, `bay rollback`, `bay compile`, `bay adopt`, `bay init`) moves
them once, after that check. It makes one commit,
`bay: move locks into project folders`. A machine with Bay 2.0 cannot
read the result, so run `bay self update` everywhere.

## 1. Smallest fleet: one box, one image-only app

Nothing has its own repo. Everything is in the fleet. The app folder holds the toml, the
lock and the files the toml mounts.

```
acme-fleet/
├── bay.fleet.toml                        # hand-edited: format, name, boxes, domains, defaults
├── hosts/
│   └── production                        # Ansible inventory (hand-edited)
├── group_vars/
│   ├── all/services.yml                  # GENERATED by `bay compile`, hash header, never edit
│   └── production/secrets.yml            # ansible-vault, `bay vault edit production`
├── projects/
│   └── gatus/
│       ├── bay.toml                      # in-fleet app (image, no repo)
│       ├── bay.lock                      # CLI-owned pin + deploy record
│       └── config.yaml                   # mounted by `from = "config.yaml"` below
└── plans/                                # plan records, committed by `bay up`
```

`bay.fleet.toml`:

```toml
format = 2                                # a Bay 2.0 CLI refuses this fleet
name = "acme"
default_box = "eu-1"
default_domain = "acme.example"
primary_env = "production"                # optional; default "production"; the deploy env with bare container names

[boxes.eu-1]
env = "production"                        # box env: hosts/production, group_vars/production/, the receipt name
group = "eu"                              # inventory group: the [eu] heading in hosts/production
```

`hosts/production` holds the heading `[eu]` with one line for `eu-1`, and a group `production`
that holds `eu`, because the box env is the Ansible host pattern:

```ini
# hosts/production
[eu]
eu-1 ansible_host=192.0.2.10
[production:children]
eu
```

(Scenario 11 adds a second box.) The words box env, group and deploy env are defined in
[Words](#words-deploy-env-box-env-and-group).

`projects/gatus/bay.toml`:

```toml
name = "gatus"
fleet = "acme"
image = "twinproduction/gatus:latest"
port = 8080
health = "none"

[access]
mode = "tailnet"

[[mounts]]
path = "/config/config.yaml"
from = "config.yaml"                      # relative to THIS bay.toml: projects/gatus/config.yaml

[deploy.production]
```

`from =` always means "relative to the folder of the `bay.toml`". That is true in a fleet
and in an app repo. `files/` at the fleet root is for rig files, shared resources and files
that several projects share (scenario 5).

For an in-fleet app, WANTED is the last commit under `projects/gatus/`, not counting
`bay.lock`. `bay up` copies the mounted files from that commit, so a file you did not
commit is not deployed. The plan names each such file in a note.

A fleet that still keeps a mounted file at `files/gatus/config.yaml` works too. The plan
prints a note. Move it with `git mv files/gatus/config.yaml projects/gatus/config.yaml`.
The path on the box does not change, so nothing is recreated.

**Create the first in-fleet app.** No verb does it, and `bay init` is only for app repos.

```
mkdir -p projects/gatus && $EDITOR projects/gatus/bay.toml projects/gatus/config.yaml
bay toml validate projects/gatus/bay.toml
git add projects/gatus && git commit -m "feat: add gatus"      # an uncommitted file is not read
bay plan production --project gatus       # no lock yet: one create step per container
bay up production --project gatus         # writes projects/gatus/bay.lock
```

You never write the lock. The first `bay up` creates it: `repo: null`, `toml_path: bay.toml`,
`commit` = the fleet commit that last changed `projects/gatus/`, and the `production`
record. An app that builds from source does not belong here: keep it in its own repo
(scenario 2), because a lock with a clone URL can only come from `bay init` or `bay import`.

Daily: `bay plan production --project gatus`, then `bay up production --project gatus`.

## 2. One app with its own repo

`shop` is built from source. Its `bay.toml` lives next to the Dockerfile. The fleet holds
only the lock.

```
shop/                                     # app repo
├── bay.toml                              # WANTED: what the app is, where it deploys
├── Dockerfile
├── deploy/
│   └── nginx.conf                        # mounted with `from = "deploy/nginx.conf"`
└── src/ ...

acme-fleet/                               # fleet repo
├── projects/shop/bay.lock                # PINNED: repo URL, commit, box, adopted names
├── projects/gatus/                       # bay.toml  bay.lock  config.yaml (scenario 1)
└── group_vars/all/services.yml           # generated from shop@<commit> + projects/gatus
```

`shop/bay.toml`:

```toml
name = "shop"
fleet = "acme"
port = 3000
health = "/healthz"
secrets = ["SESSION_SECRET"]              # names only; values are in the vault
needs = ["postgres"]                      # needs a resource of kind postgres on the same box (scenario 5)

[access]
mode = "public"                           # required: public | tailnet | internal; there is no default

[[mounts]]
path = "/etc/nginx/nginx.conf"
from = "deploy/nginx.conf"                # relative to this bay.toml

[deploy.production]
domain = "shop.acme.example"
```

`acme-fleet/projects/shop/bay.lock` (CLI-owned, trimmed). It holds no path of any machine:

```json
{
  "lock_version": 2,
  "name": "shop",
  "repo": "git@github.com:acme/shop.git",
  "toml_path": "bay.toml",
  "commit": "0123456789ab",
  "envs": {
    "production": {
      "box": "eu-1",
      "adopted": { "database": "shop_prod", "role": "shop" },
      "deployed_at": "2026-10-07T10:00:00Z",
      "result": "ok"
    }
  }
}
```

`adopted` holds the names that Bay keeps from before the fleet: an existing database, role,
volume or container, so nothing is renamed. It is not the verb `bay adopt` (scenario 7), except
that the verb writes two commit keys into the same object. `commit` is the one project pin; each
env record may also hold the `commit` of its own last `bay up` (plan.md, [The lockfile](plan.md#the-lockfile)).

Flow from the app repo:

```
cd ~/code/shop
bay init --fleet acme                     # once: drafts bay.toml, writes projects/shop/bay.lock in the fleet
                                          # (no bay.toml yet, so name the fleet; later commands read `fleet =`)
git add bay.toml && git commit -m "chore: add bay.toml"
git push                                  # 1. push first: bay up refuses a commit the remote lacks
bay plan production                       # WANTED (this commit) vs PINNED vs RUNNING
bay up production                         # 2. pins this commit, deploys the whole env, puts the webhook script on the box
git commit -am "feat: first build"        # 3. a push that changes code, made AFTER bay up
git push                                  #    the webhook builds the first image
bay plan production                       # 4. then see plan.md "The first image" for the last steps
```

Expect step 2 to fail. The first `bay up` of an app that builds from source fails on purpose:
`bay up` builds no image, and `shop` has none yet, so the box cannot create the container
([plan.md, The first image](plan.md#the-first-image)). Bay has no special code or message for this
case. It looks like any failed deploy: the command exits 1 with
`deploy failed: Command failed with exit code N`, the JSON has `"result": "failed"` and an `error`
text, the lock records `result: failed`, and Bay still commits that record and pushes the fleet.
`bay show` says `HALF` (the last `bay up` failed; all status words are in the
[bay show table](plan.md#bay-show)). The pin and the webhook script are in place, which is what
step 2 is for. To be sure that this is the expected failure and not another one, read the Ansible
log (`--log`) or the receipt on the box: the failed action is the container that has no image. The
way out is steps 3 and 4: push code, wait for the build, run `bay up` again.

The push of step 1 builds nothing, because the box has no webhook script for `shop` yet. The
first image comes from a push made after `bay up` (step 3), or from a `bay deploy production`
with no `--tags`, which clones and builds. Followed in this order, `bay show` says `HALF` until the
build has finished and a `bay up` has run again ([plan.md](plan.md#the-first-image)). The push
reaches the box through the webhook receiver: you register the GitHub hook by hand, in the repo
settings, with the payload URL `https://<webhook domain>/webhook/<container name>` (`shop` here). The
webhook domain is `[webhook] domain` of `bay.fleet.toml`, or `webhook_domain` of the box that runs
the container. Bay has no verb that creates the hook; `bay validate --check-webhook-health` probes it.
Before the first push, run `bay --fleet <path> deploy production` (no `--tags`) once for the box env:
it installs the receiver and the build trigger, and on the box it makes the SSH deploy key
`/opt/<stack>/builds/shop/.deploy_key.pub` (only when the repo has no token). Bay does not register
that key: add it to the GitHub repo yourself, under Settings > Deploy keys (read-only). With an SSH
repo URL, the first such deploy stops at the clone until the key is on GitHub. Add it, then run the
deploy again.

Where Bay reads the app code, in this order:

1. The git checkout you stand in, when its `origin` matches the `repo` in the lock.
2. A bare clone in the fleet cache, `acme-fleet/.bay-cache/repos/<slug-of-repo-url>`
   (git-ignored). Bay fetches it before each plan. Two projects of one repo share one
   clone.

No file maps a project to a folder on your disk. A commit that is in neither place is an
error, never a skip. `bay init` refuses a repo with no `origin` remote, because Bay finds
the repo by that URL. `bay up` also refuses a commit that is on no branch of the remote:
"push first". The box builds from the remote, so an unpushed commit would build other
code. A `bay.toml` that is not committed is not read.

`bay init` does not put secret names into the vault. Run `bay vault edit production` for
that. A single step that does both, `bay init --secrets`, is (planned).

## 3. App repo with several services and a monorepo path

`blog` has a web app, a worker and an API in one repo. One `bay.toml` at the repo root
describes all three. The `bay.toml` may also sit deeper. The lock records `toml_path`, and
`bay init --toml-path <path>` sets it.

```
blog/
├── bay.toml                              # one file for web + worker + api
└── apps/web/Dockerfile  apps/api/Dockerfile
```

```toml
name = "blog"
fleet = "acme"
port = 3000
needs = ["postgres", "redis"]

[access]
mode = "public"

[build]
dockerfile = "apps/web/Dockerfile"

[services.worker]
command = "node apps/worker/main.js"      # inherits image, env, secrets, fleet_secrets, needs, update, logs

[services.api]
build = { dockerfile = "apps/api/Dockerfile", context = "apps/api" }
port = 4000
path = "/api"                             # routed under the main domain

[deploy.production]
domain = "blog.acme.example"
```

A service inherits these keys of the project: `image` (or the `[build]`), `env`, `secrets`,
`fleet_secrets`, `needs`, `update` and `logs`. It does not inherit `port`, `health`, `memory`,
`mounts`, a password or limits, and `inherit = false` starts it empty (the full rule is in
[bay-toml.md](bay-toml.md#servicesname)).

Containers on the box: `blog`, `blog-worker`, `blog-api`. The fleet only gains
`projects/blog/bay.lock`. As written, this file hits two unsupported cases of 2.1: `path = "/api"`
and the worker that shares the build of the project. `bay plan` lists them in `unsupported` and blocks
until you pass `--allow-unsupported` ([plan.md](plan.md#features-bay-cannot-deploy-yet)).

## 4. Two environments, two boxes

Same `shop` repo. Production on `eu-1`, staging on `eu-2` from the `develop` branch.
Each environment owns its data. Its secret values are separate because `eu-2` is its own box env
(`staging`): secrets are per box env, not per deploy env (see [Words](#words-deploy-env-box-env-and-group)).

The fleet gets `hosts/staging` and `group_vars/staging/secrets.yml`, next to the production
files. One `projects/shop/bay.lock` holds one project pin (`commit`, the one the fleet
compiles) and one record per env: `envs.production` and `envs.staging`, each with the `commit`
of its own last `bay up`. One compiled `services.yml` covers both envs, from the one pin.

```toml
# bay.fleet.toml
[boxes.eu-1]
env = "production"
group = "eu"
[boxes.eu-2]
env = "staging"
group = "eu2"

[resources.postgres]                      # one resource, two boxes (scenario 5 explains `kind` and `box`)
kind = "postgres"
box = ["eu-1", "eu-2"]
image = "pgvector/pgvector:pg17"
```

`shop/bay.toml` deploy tables:

```toml
[deploy.production]
box = "eu-1"
domain = "shop.acme.example"

[deploy.staging]
box = "eu-2"
domain = "staging.shop.acme.example"
branch = "develop"
access.mode = "tailnet"
[deploy.staging.env]
LOG_LEVEL = "debug"
```

`bay up staging` deploys every project that has a `[deploy.staging]` on `eu-2`. It does not
deploy to the production box. But the project has one pin, so `bay up staging` moves it: the
compiled production entries move to the same commit, and `bay show shop` says `drift` for
production until `bay up production` runs (plan.md, [The lockfile](plan.md#the-lockfile); `drift`
means the box runs something else than the pin, see the [bay show table](plan.md#bay-show)).
Read the plan before you approve it.

**Which branch holds the `bay.toml`.** The pin is one commit, and the compiler emits only the
`[deploy.<env>]` tables of the `bay.toml` at that commit. So keep both tables on both branches:
merge the `[deploy.staging]` commit of step 5 below into `main` before the next `bay up production`.
If `main` has no `[deploy.staging]`, a `bay up production` from `main` pins a commit without it: the
plan shows a destructive `remove` step for each staging container, and the next deploy of the
staging box env removes them.

Container names. The primary env (`production`) has no suffix. Other envs add `-<env>`.

| Container | production | staging |
|---|---|---|
| main | `shop` | `shop-staging` |
| service `worker` | `shop-worker` | `shop-staging-worker` |
| job `cleanup` | `shop-job-cleanup` | `shop-staging-job-cleanup` |

(Only the name rule matters here: a job is `(validated, not deployed yet)`, see scenario 9.)

Missing secrets: `bay secret missing staging` lists the names (add `--fleet <path>` when you stand in the fleet directory). It never prints a value.

**A second env on a new box, in order.** Do the fleet first, then the app repo. Every command of
steps 3 and 4 runs on the fleet, and none of them reads the fleet directory you stand in (rule 5 of
[install.md](install.md#pick-a-fleet)), so each one names it: `F=~/.config/bay/fleets/acme`, then
`bay --fleet $F ...`.

1. `hosts/staging`, one heading per group, and a `staging` group that holds them (the box env is
   the Ansible host pattern):

   ```ini
   [eu2]
   eu-2 ansible_host=192.0.2.11
   [staging:children]
   eu2
   ```

2. `bay.fleet.toml`: the box `eu-2` with `env = "staging"` and `group = "eu2"`, and `"eu-2"` in
   the `box` list of the postgres resource (above). Without it, `needs = ["postgres"]` has no
   postgres on `eu-2` and the plan blocks (scenario 5).
3. The secrets of the env: write `group_vars/staging/secrets.yml` as a `secrets:` mapping,
   run `bay --fleet $F vault encrypt staging`, and later change it with `bay --fleet $F vault edit staging`.
   Add every name that `bay --fleet $F secret missing staging` lists. Staging has its own values: nothing is shared with
   production unless the project uses `fleet_secrets`.
4. Commit and push the fleet. Provision the box: `bay --fleet $F provision staging` (the
   first time, when the fleet's admin user is not on the box yet, it falls back to `root` on its own). Then install the
   rig on it: `bay --fleet $F deploy --rig staging` (Traefik, the cron jobs, the monitor and the rest;
   scenario 11 says which verb installs what).
5. In the app repo add the `[deploy.staging]` table above and commit it on the `develop` branch.
   Push `develop`. `bay up` refuses a commit that the remote lacks, so this push comes first. It
   builds nothing yet.
6. Stand on the right commit. WANTED is the HEAD of the checkout you stand in, so run
   `git switch develop` before `bay plan staging`. On `main` the plan would pin the commit of
   `main` to staging. Do not plan this first time from the fleet directory: with no checkout Bay
   finds the deploy branch in the `bay.toml` that is pinned, and the pinned file has no
   `[deploy.staging]` yet, so Bay reads the remote's default branch and blocks. Pass
   `--project shop --at <commit of develop>` there instead (`--at` needs `--project` in the fleet directory). After the first `bay up staging` the pin names the
   table, and the fleet directory finds `develop` ([plan.md](plan.md#bay-plan), step 1).
7. `bay plan staging`, then `bay up staging`. This pins the commit and puts the webhook script on
   `eu-2`. Expect it to fail, as step 2 of scenario 2 does: `shop-staging` has no image yet.
8. Build the first image. On this new box a push alone builds nothing yet: the webhook receiver
   image and the build trigger of `shop-staging` come only from a `bay deploy <env>` with no `--tags`
   or with `--tags git_deploy`, on a box that runs a
   build app, and step 4 ran before `eu-2` had one. Run `bay --fleet $F deploy staging` (no `--tags`)
   once: it installs both, makes the SSH deploy key `/opt/<stack>/builds/shop-staging/.deploy_key.pub`
   on `eu-2` (when the repo has no token), and clones and builds the first image (see
   [plan.md](plan.md#the-first-image)). Bay does not register the key: add it to the GitHub repo
   yourself, under Settings > Deploy keys (read-only). Then run `bay up staging` again. From then on, a push to
   `develop` builds: it reaches `eu-2` through the webhook receiver of that box. `eu-2` uses
   `[webhook] domain`, or its own `webhook_domain` (scenario 5 sets one on `infra`). Register the
   GitHub hook for `shop-staging` at `https://<that domain>/webhook/shop-staging`. Staging owns its
   data: it starts with an empty database.

Scenario 11 shows the box and hosts files in more detail.

## 5. Several boxes, shared resources, mixed apps

The realistic shape. Three boxes with different roles, one shared Postgres, a backup
sidecar, build apps in their own repos, image apps in the fleet.

Boxes: `eu-1` runs `shop`, `blog` and the shared Postgres. `infra` runs the deploy
webhook receiver and the registry. `na-1` is an app box in a second region.

```
acme-fleet/
├── bay.fleet.toml                        # format, boxes, [resources.*], [webhook], [tailnet]
├── hosts/production
├── group_vars/all/headscale_acl.yml      # tailnet policy (hand-edited rig config)
├── projects/
│   ├── shop/bay.lock                     # repo app -> lock only
│   ├── blog/bay.lock                     # repo app -> lock only
│   ├── gatus/                            # image app -> toml, lock, files together
│   │   └── bay.toml  bay.lock  config.yaml
│   └── vaultwarden/bay.toml  bay.lock
├── files/shared/robots.txt  files/crowdsec/ ...   # shared and rig files
└── .bay-cache/                           # git-ignored: fetched repos, rig-state cache
```

`bay.fleet.toml` extras:

```toml
[boxes.infra]
env = "production"
group = "infra"
webhook_domain = "deploy.infra.acme.example"

[resources.postgres]                      # shared, used by `needs = ["postgres"]`
kind = "postgres"                         # postgres | redis | container (default container)
box = "eu-1"                              # one box, or a list: ["eu-1", "eu-2"]
image = "pgvector/pgvector:pg17"
volumes = ["postgres_data:/var/lib/postgresql/data"]

[webhook]
domain = "deploy.acme.example"            # default webhook domain; a box's `webhook_domain` overrides it
secret = "WEBHOOK_SECRET"                 # a secret NAME; the value is in the vault
```

**How a resource maps to boxes.** `needs = ["postgres"]` finds the one resource of `kind =
"postgres"` whose `box` contains the box of the project's deploy env. That is the whole rule:

- `box` is a name or a list. A list runs the same image and config on each listed box, and
  each box has its own empty data.
- A project on a box that no postgres resource lists does not reach a postgres on another
  box. The compiler puts this in `unsupported`, so `bay plan` blocks with "postgres on another
  box than eu-2 (cross-box data access)". It is the same blocker and the same
  `--allow-unsupported` escape as every key in
  [plan.md](plan.md#features-bay-cannot-deploy-yet), and the escape deploys the container with no
  database for that need. Do not use it for this case. Fix it by adding the box to the list, or
  by a second resource of the same kind that lists only that box (`[resources.postgres-staging]`,
  `kind = "postgres"`, `box = "eu-2"`).
- Two resources of one kind that both list the same box are an error ("keep one per box").
- A `kind` left out is `container`, which no `needs = ["postgres"]` matches: write the `kind`.

A project mounts a shared fleet file with an explicit prefix. There is no guessing:
`from = "fleet:shared/robots.txt"` reads `<fleet>/files/shared/robots.txt`.

Who edits what:

| Change | Edit this | Then |
|---|---|---|
| App code, port, health, env, mounts | `bay.toml` in the app repo | `git push`, then `bay up` |
| An image-only app | `projects/<name>/bay.toml` in the fleet | commit, then `bay up` |
| A new box, domain default, shared DB | `bay.fleet.toml` (a new box also needs `hosts/`, scenario 11) | `bay up` (it compiles) |
| A secret value | `bay vault edit production` | `bay plan --remote`, then `bay up --plan-id <id>`. A plain `bay plan` never reads a value and can show 0 steps while `bay up` recreates the containers that use it ([plan.md](plan.md#bay-plan)) |
| A tailnet route | `bay route add` (edits `bay.fleet.toml`) | `bay plan`, `bay up` (scenario 15) |
| Tailnet ACL, CrowdSec, Traefik | `group_vars/...` as before | `bay deploy production --tags <role>` |

`bay up` always compiles first. Run `bay compile` by hand only to look at the result.
Rig parts (Traefik, CrowdSec, Headscale, Zot, webhook receiver) are not projects.

## 6. Fleet inside a bigger repo

A fleet may be a subfolder of another repo (the way a test fleet can sit in a workspace repo).
Bay commits with path-scoped commits and does not push.
The fleet root is the subfolder that holds `bay.fleet.toml`.

Point at it with `bay --fleet ~/workspace/devfleet ...`. `bay up` reports `push_skipped`.

## 7. Moving an app from the fleet into its repo

Use this when an app has a repo but its `bay.toml` still lives in the fleet. Two cases:

- **An app that builds from source**, which came into the fleet through `bay import` of an old
  YAML fleet. Its lock already carries the repo URL that the webhook builds (the import wrote it:
  no verb sets it for a new in-fleet project). The order below is for this case, because the box
  has a webhook script for it.
- **An image-only app** with `repo: null` that has a repo of its own (for example one that holds
  its config files). It has no webhook script on the box, so a push cannot rebuild it. The adopt
  only moves the files into the repo. The order still holds, because `bay up` must accept the
  unpushed adopt commit.

Before and after, for the build case:

```
BEFORE                                    AFTER
fleet projects/shop/bay.toml + files -->  shop/bay.toml + files    (one local commit in the app repo)
fleet projects/shop/bay.lock              fleet projects/shop/bay.lock
  repo: git@github.com:acme/shop.git        repo: git@github.com:acme/shop.git   (the same string)
  adopted: {...}                            adopted: {...}         (kept, so nothing is renamed)
```

The adopt sets `repo` to the `origin` URL of the checkout when it was `null`, and keeps the
lock's own string when it already named this repo, so the compiled build entry does not change.
A lock that names another repo stops the adopt.

Steps. The order matters: adopt, `bay up`, then `git push`.

```
cd ~/code/shop
bay --fleet ~/.config/bay/fleets/acme adopt shop --check    # prints the files and the lock diff, changes nothing
bay --fleet ~/.config/bay/fleets/acme adopt shop
bay plan production                       # must say: 0 container steps, verdict auto
bay up production                         # in the app checkout: takes the unpushed adopt commit, moves no code
git push                                  # last: the push is config only, it builds and recreates nothing
```

Two things to check before the push, when the project has more than one deploy env:

- **One `bay up` per box env.** The adopt writes the adopt commit into the record of every deploy env, but
  `bay up <env>` deploys only the box env of that deploy env, and only that box env gets the new
  `rebuild.sh`. A box that did not get it still treats the push as code, builds and recreates. Run
  `bay up <env>` for each deploy env that sits on another box env (two deploy envs on one box env need
  one `bay up`), and only then push.
- **Adopt from the deploy branch.** The adopt commit lands on the branch you have checked out, and
  `bay adopt` does not check it. The webhook follows `[deploy.<env>].branch` (default `main`), so a
  commit that is on another branch triggers nothing there. A deploy env that follows another branch gets
  the commit only when you merge it into that branch.

`bay adopt` copies `projects/shop/bay.toml` and its files into the app repo, at
`--toml-path` (default `bay.toml`). It rewrites the lock to repo form. It deletes
everything in the fleet folder except the lock. It commits both repos. The app repo commit
stays local: you push it, after `bay up`. The `bay.toml` is not in the repo yet, so name
the fleet with `--fleet`, `BAY_FLEET` or `BAY_FLEET_NAME`. For a monorepo, add
`--toml-path <path>`.

Why `bay up` comes before the push (the build case). The box still runs the old `rebuild.sh`,
which treats every push as code. Pushed first, the adopt commit would build the app and recreate it.
`bay up` writes the new script. It accepts the adopt commit before it is pushed (the one
exception to "push first") and moves no code for it. Then the push touches only
`bay.toml` and the files beside it. That is a config-only push: the box logs
`config-only push <commit12>: run bay up`. It builds no image, does not move `:latest`, recreates no
container and sends no alert. It does one thing: it tags the image that already runs (the one of the
previous commit) with the adopt commit, `<image>:<commit12>`, so that a later `bay up` to this commit
finds its image, also in `track = "pin"`. Run `bay up` in the app
checkout, because the fleet's repo cache does not have the adopt commit yet.
For a later config-only push (no alert), `bay show <name>` saying `behind` is the sign that a
`bay up` is owed (scenario 10).

Only the file location moves. The container, database, volumes and files stay as they are
because the lock keeps the adopted names. The pin changes from a fleet commit to an app
commit, and that is by design.

`bay rollback` straight after an adopt is refused: the previous pin is a fleet commit. Use
`bay up --at <commit>`. `bay rollback --to <commit>` of an app commit still works: it moves the
pin and the code (scenario 10).

`bay plan` on the adopt commit prints no "bay up will refuse it" note. It says the commit is the adopt
commit and is not pushed yet, and that `bay up` takes it. Once you push, the note is gone.

## 8. App needs app

`web` calls `api`. Both have their own repo.

`api/bay.toml`:

```toml
name = "api"
fleet = "acme"
publish = true                            # other projects may list "api" in `needs`
port = 4000
health = "/healthz"
secrets = ["API_SECRET"]

[access]
mode = "internal"                         # no route; only containers that need it can reach it

[deploy.production]
box = "eu-1"
```

`web/bay.toml`:

```toml
name = "web"
fleet = "acme"
port = 3000
needs = ["api"]                           # the fleet injects API_URL into web

[access]
mode = "public"

[deploy.production]
domain = "app.acme.example"
```

Read `API_URL` in the `web` code. Never write the address of `api` into `[env]`. The name
is `<NAME>_URL`. To pick another name, use the table form `[needs.api]` with
`env = "BACKEND_URL"`. Without `publish = true`, `web` may not need `api`.

When `api` runs on another box, the fleet decides the network path and the tailnet ACL.
The compiler picks it. The docs say no more than that, so do not hard-code a tailnet name.
All envs on a box share one Docker network, `services`. A container of an env other than
`primary_env` carries the env in its name (`api-staging`), so read `API_URL`, never a container name:
a name without the env reaches the `primary_env` container.

As written, `api` hits two unsupported cases of 2.1: it is an internal container (no `domain`,
no `path`) that builds from source (it has neither `image` nor `[build]`, so it builds
`./Dockerfile`), and it has a health path other than `/`. `bay plan` lists both in `unsupported` and
blocks until you pass `--allow-unsupported`
([plan.md](plan.md#features-bay-cannot-deploy-yet)). `web` plans clean.

A project whose lock pins no commit is not deployed: run `bay up` for `api` once. `bay up`
deploys the whole env in one run, so do not count on `api` being ready before `web`
starts. Let `web` retry.

## 9. Scheduled job plus a locked admin path

`shop` runs a nightly clean-up. It keeps `/admin` for the tailnet only. It also has a
staff page behind a password.

```toml
name = "shop"
fleet = "acme"
port = 3000
health = "/healthz"
secrets = ["SESSION_SECRET"]

[access]
mode = "public"
locked = ["/admin"]                       # only reachable from the tailnet
open = ["/webhooks"]                      # opened to the public in every env

[[jobs]]
name = "cleanup"
schedule = "0 2 * * *"                    # UTC cron
command = "node dist/cleanup.js"
memory = "256m"

[services.staff]                          # one staff path, one password
command = "node dist/staff.js"
port = 5000
path = "/staff"
health = "/"                              # required when a password is set
[services.staff.access.password]
users = ["staff"]                         # the secret is PASSWORD_STAFF, in the vault
realm = "Staff only"

[deploy.production]
domain = "shop.acme.example"
```

- A top-level `[access.password]` covers every path not in `open`. To protect one path,
  give it its own service, as above. A service never inherits a password.
- A `locked` path needs the password too, when the top level has `[access.password]`.
- `/admin` matches `/admin/x`, not `/administrator`. A path may not be `open` and `locked`.
- `bay secret missing production` lists `PASSWORD_STAFF` until it is in the vault.

The job is a one-shot container, `shop-job-cleanup`. It gets the image, env, secrets,
needs and mounts of the main container. In 2.1 the compiler cannot deploy `[[jobs]]` yet, and a
service with `path` (`staff` above has `path = "/staff"`) is unsupported too: `bay plan` lists both in
`unsupported` and blocks until you pass `--allow-unsupported`
([plan.md](plan.md#features-bay-cannot-deploy-yet)). With the flag the job does not run and the
`staff` service is not deployed. Give `staff` its own `domain` instead of `path` to deploy it
(`access.open` beside a top-level `[access.password]` is unsupported as well). This doc names no
verb to run a job once by hand or to read its last result.

## 10. Rollback, both track modes

`track` says what a push to the app repo does. It is set per env. `branch` is the default.

```toml
[deploy.production]
track = "branch"                          # default: a push builds and deploys new code
# track = "pin"                           # a push only builds. `bay up` deploys.
```

| What you push | `track = "branch"` | `track = "pin"` |
|---|---|---|
| Code only | builds and deploys | builds, then **holds** |
| Only `bay.toml` and the files its mounts read | nothing is built or deployed: `config-only push <commit12>: run bay up`. The box tags the image of the previous commit with this commit (`<image>:<commit12>`) | the same: nothing is built, the image gets the tag, so a later `bay up` to this commit finds its image |
| Code and `bay.toml` | builds, then **holds**: the alert `build.held` names `bay up` | builds, then holds |
| A `[build]` edit (Dockerfile path, build arg, `image`) | builds, then holds | builds, then holds |

The checks apply in a fixed order: the webhook's `watch` and `ignore` filter, the config-only
rule, then the hold guard. The order is stated once, in
[build-pipeline.md](build-pipeline.md#order-of-the-guards-on-a-push). A push that
the filter drops (it touches only files outside `watch`, a push of `bay.toml` alone included)
reaches none of the other checks: it is not built, not held and not logged as config-only.
So when you set `watch`, put `bay.toml` and the files its mounts read in it
([bay-toml.md](bay-toml.md#build)).

After a config-only push, nothing alerts you. The signal is `bay show shop`: it says `behind`
(HEAD is ahead of the pin), and you run `bay up`. On the box, `journalctl -u bay-build@shop` has
the line `config-only push <commit12>: run bay up`.

A config-only push needs `build.bay_build_hash` to match. `bay_build_hash` is the hash of
the `[build]` keys. So a `[build]` edit is never a config-only push: it always builds.

`bay up` releases a held or built image. Every build gets an image tag with its commit
(`<image>:<commit12>`). `:latest` moves only when a deploy goes ahead. A held build is not
a failure and does not trip the circuit breaker. A comment edit in `bay.toml` does not
hold, because Bay compares the parsed file.

`bay rollback` restores config **and** code:

```
bay show shop                             # which env is bad
bay rollback production --project shop
bay show shop
```

- It moves the pin back to `previous` (a record in the lock).
- It asks the box to point `:latest` at the image of the previous receipt (`<env>.prev.json`, a second
  record, on the box). When the box cannot, the output says `code: kept <container> (<reason>)`. The
  usual reason is a previous receipt from before 2.1: then only the config rolls back.
- Plain rollback is safe only straight after the bad `bay up`. Every deploy of the box env that
  reaches the container pass rotates `<env>.prev.json`, so after a later deploy (even a `bay up` that changes nothing, or one for
  another project of the same box env) the plain rollback moves the pin back and keeps the code that
  runs. Use `--to <commit>` then.
- It plans like `bay up`. A verdict `approve` stops it unless you pass `--force --reason "<why>"`
  (`--data keep` after a box move). `blocked` and `stale` stop it whatever you pass. There is no
  `--plan-id` ([plan.md](plan.md#bay-rollback)).
- It freezes the env (`frozen = true` in the lock). While frozen, a push builds but does
  not deploy, whatever `track` says.
- The next `bay up` to a newer commit clears the freeze. Push the fix, then run `bay up`.

`bay up` from a stale checkout never moves the running code backwards. In `branch` mode it
moves code forward only: when the box runs a commit newer than the pin, `:latest` stays and
only the config changes. A whole-environment `bay up <env>` makes the same check for every
project. `--force-code` moves it anyway, as a destructive step. `pin` mode and
`bay rollback` move code backwards on purpose.

**An image-only app in the fleet** (such as gatus, `image = "...:latest"`). It builds nothing, so
rollback moves the config pin only: Bay compiles the previous `bay.toml` and runs `bay up`. There is
no code target, because the box has no commit-tagged image to point `:latest` at. The freeze is
set in the lock but changes nothing, because no push builds this app. `bay rollback --to` refuses
with "builds no image". Rollback cannot undo a bad `:latest` pull: the pin changes the
`bay.toml` that Bay compiles, not which image `:latest` names. Pin a tag in `image` (for example `:1.4.2`) if you need to go back to an older image.

`previous` is one level deep. For anything older, or to undo only the last bad push:

```
bay rollback production --project shop --to 0123456789ab
```

`--to` is the one form for an older state. It moves the code to the image of that commit, and
for an app whose `bay.toml` is in its repo it moves the pin to that commit too: config and code.
It works when the image of that commit still exists on the box. Otherwise it refuses before
anything moves and lists the commit tags the box has. A second plain rollback undoes the first.
How far back that reaches: nothing counts commit tags, but the `docker system prune -af --volumes` cron job
of the box (weekly by default) removes every image that no container uses, tags and all
([plan.md](plan.md#bay-rollback), "How far back `--to` reaches").

**A bad push in branch mode.** A push deploys by itself, and it never changes `previous` (only
`bay up` does). Say the last `bay up` was good, then a push put bad code on the box. Plain
`bay rollback` would go to the state before that `bay up`, which can be older than the last good
push. Roll back to the last good commit instead:

```
bay rollback production --project shop --to <last good commit>
bay up production --project shop                       # after you pushed the fix and its build finished
```

The env is frozen after the rollback: pushes build but do not deploy. The `bay up` to a newer
commit clears the freeze. A push that fails its health check is rolled back on the box by itself.

`--to` rolls the config back too. For an app whose `bay.toml` is in its repo, the pin moves to
`<last good commit>`, so a `bay.toml` change pinned after it is undone as well. To keep the newer
config, use the other way: commit a `git revert` of the bad code and push it. While the env is not
frozen, that push deploys the reverted code under the pinned config, and the later `bay.toml`
changes stay ([plan.md](plan.md#undo-a-bad-push-in-branch-mode)).

**Rollback in pin mode.** The happy path:

```
bay rollback production --project shop                     # straight after a bad bay up
bay rollback production --project shop --to <commit>       # older, or after a later deploy; the image must be on the box
bay up production --project shop                           # forward again, once the fix is pushed and its build is held
```

A push already holds, so the freeze adds nothing you can see. Plain
`bay rollback` moves the pin back and skips the code move (`code_kept`) when the previous image
is not on the box. The env is then half rolled back: old config, newer code, frozen. Nothing moves by
itself, but the code does not say that this mix is safe, so do not leave it if it is not.
`bay plan` compares the running commit with the HEAD you stand on, not with the pin, and prints no
check of the image. `bay show` says `behind` while HEAD is ahead of the pin (the status words are
in the [bay show table](plan.md#bay-show)). `bay rollback --to`
refuses when the image is missing:
`<image>:<commit12> is not on the box ... Commit tags on the box: ...`. To go forward: push the fix,
wait for its build, then `bay up` to that commit. In pin mode the image must be on the box, or
the deploy stops before any container changes. Full rules: [plan.md](plan.md#bay-rollback).

## 11. Adding a box

A new box lives in two fleet files. `bay.fleet.toml` says what it is. `hosts/` says how to
reach it.

```toml
# bay.fleet.toml
[boxes.eu-2]
env = "production"
group = "eu2"                             # its own group: boxes that share one run each other's containers
```

```ini
# hosts/production
[eu]
eu-1 ansible_host=192.0.2.10
[eu2]
eu-2 ansible_host=192.0.2.11
[production:children]
eu
eu2
```

The name at the start of each host line (`eu-1`, `eu-2`) is the `inventory_hostname`. It is the `box` of the
receipt, so use the box names of `bay.fleet.toml` there. The `[production:children]` block makes `production`
(the box env) an Ansible group. Without it, a command that gets `production` finds no host.

(In scenario 4, `eu-2` is the staging box of a second env. Here it is a second box of the
`production` env.)

Commit and push the fleet. Then provision the new box. Do it before the first `bay up`
to that box:

```
bay --fleet ~/.config/bay/fleets/acme provision eu2        # the group of the new box only
bay --fleet ~/.config/bay/fleets/acme deploy --rig production -- --limit eu-2   # the rig, new box only
bay --fleet ~/.config/bay/fleets/acme doctor production    # does the box answer?
```

Which verb puts what on the new box:

- `bay provision` hardens SSH and installs Docker, the firewall and CrowdSec. When the box has only
  `root` and the fleet's `ansible_user` does not exist on it yet, the first run tests the
  connection as `ansible_user`, finds the host unreachable and falls back to `root` for that run,
  as in the [README](../README.md). It needs no `-- -u root`.
- `bay deploy --rig` runs every rig role once: Traefik, Watchtower, the access gateway, the cron jobs
  (the image prune among them), the container monitor, backups and the CrowdSec allowlist.
- `bay up` runs only the `deploy_stack` tag. That tag also refreshes Traefik, Watchtower and the
  access gateway, but not the cron jobs, the monitor, backups or the allowlist.
- The webhook receiver image and the build trigger of a container come only from a
  `bay deploy <env>` with no `--tags`, or with `--tags git_deploy`, on a box that runs a build app. A box with no build app yet gets neither from
  `--rig` (see scenario 4, step 8).

`bay provision production` would run on every host of the box env, `eu-1` included. The code has no
guard against that, and no doc states that each provision role is safe to repeat on a live box (the
playbook says it "works on fresh servers and reprovisioning", and the docs tell you to re-run it after a
config change), so aim it at the new box. A group name works (`eu2`). A host works too, with an extra
argument for Ansible: `bay provision production -- --limit eu-2`. For a deploy, use the `--limit`
form, as above: a deploy aimed at the group `eu2` writes its receipt as `eu2.json`, not
`production.json`.

SSH access to the box, and its tailnet enrolment, come from you, out of band. An empty box
runs nothing until an app names it: `box = "eu-2"` in a `[deploy.<env>]` table, or
`default_box` in `bay.fleet.toml`. `bay up production` then deploys the whole env,
including the new box. An app on the new box that has `needs = ["postgres"]` also needs a postgres
resource that lists `eu-2` (scenario 5, "How a resource maps to boxes"), or its plan blocks.

## 12. Box move with data

`shop` moves from `eu-1` to the box `eu-2`. Add `eu-2` first (scenario 11), and add `"eu-2"` to
the `box` list of the postgres resource, so that `needs = ["postgres"]` finds a postgres on the
new box (scenario 5). Commit and push the app after the fleet, so the box exists when the app asks
for it:

```toml
# shop/bay.toml
[deploy.production]
box = "eu-2"
```

```
bay plan production --project shop                 # verdict: blocked, names each volume and database left behind
bay plan production --project shop --data keep     # verdict: approve
bay approve <plan-id> --reason "move to eu-2, data stays"
bay up production --project shop --data keep
```

The lock's box is PINNED, and `bay.toml` is WANTED. The compiler uses the lock's box, so
the edit alone moves nothing. The plan compares the two. A difference is a step `move`,
with a `remove` on the old box and a `create` on the new box per container. Its risk is
`destructive` when the project has a named volume or a database, `shared` if not. A
destructive `move` is `blocked` until you pass `--data keep`. `bay up` writes the new box
into the lock. If the deploy of the new box fails, Bay does not deploy the old box, so its
container stays.

`--data keep` means:

- The container stops on `eu-1`.
- The volumes and the database stay on `eu-1`. Bay never deletes them. The weekly cron job
  `docker system prune -af --volumes` does not delete them either: on Docker Engine 23 and later
  it removes only anonymous volumes, and Bay gives every volume a name (`<stack_name>_<volume>`).
- The app starts on `eu-2` with empty volumes and a new database.

The plan lists the old volumes and the old database by name. It prints the
`docker volume rm` and `DROP DATABASE` lines to run by hand when you no longer need the
data.

To bring the data across, copy it by hand (for example `rsync` of the volume and
`pg_dump` to the new database). `--data move` is (planned): Bay refuses the flag today.
DNS is yours too: point the domain at `eu-2` when the data is in place. This doc names no
Bay verb for DNS. A rollback across a move is a move back, and it needs `--data keep`
again.

## 13. Multi-tenant: one app, three customers

One repo, three customers. Each customer gets its own project: `shop-acme`, `shop-globex`,
`shop-initech`. Each has its own data, its own secret names and its own project pin.

```
shop/                                     # one app repo
├── Dockerfile
└── tenants/acme/bay.toml  globex/bay.toml  initech/bay.toml   # name = "shop-acme" ...

acme-fleet/projects/
└── shop-acme/bay.lock  shop-globex/bay.lock  shop-initech/bay.lock   # each has its own toml_path
```

`shop/tenants/globex/bay.toml`:

```toml
name = "shop-globex"
fleet = "acme"
port = 3000
needs = ["postgres"]                      # globex has its own database

[fleet_secrets]                           # container variable = vault key. One vault file serves all three
SESSION_SECRET = "GLOBEX_SESSION_SECRET"  # tenants, so each tenant names its own key

[access]
mode = "public"

[deploy.production]
domain = "shop.globex.example"
track = "pin"                             # scenario 10: roll out one tenant at a time
```

Register each one with `bay init --toml-path tenants/globex/bay.toml`.

None of the three sets `[build]`, so each builds `Dockerfile` at the repo root: `dockerfile` and
`context` are relative to the repo root, not to the `bay.toml` (the mount `from` is the one key
read beside the toml). `bay init` looks for a Dockerfile only beside the `bay.toml`, so here it
warns "no Dockerfile" and does not read the port from it. Set `port` yourself.

Why not one project with three envs (`[deploy.acme]`, `[deploy.globex]` ...)?

- One project has one pin (`commit` in the lock), shared by all its envs, as in scenario 4: a
  `bay up globex` also moves the compiled entries of the project's other envs (`acme`, `initech`) to
  that pin. With one project per tenant, `bay up` with `--project shop-globex` leaves `shop-acme` at its
  own pin. You can roll one tenant back alone. (A `bay up globex` from the fleet directory pins every
  covered project, see [What `bay up` does](#what-bay-up-does).)
- A deploy env has its own suffix on the container name (`shop-globex`) but not its own box
  env: its box env is the one of its box (see [Words](#words-deploy-env-box-env-and-group)).
  `bay up globex` deploys the whole box environment, not one tenant.

A new tenant is a new project: `bay init`, add its secrets, `bay up`.

## 14. Fresh machine

A new laptop. Nothing is installed. You will deploy `shop` to the `acme` fleet.

```
git clone https://github.com/AltanS/bay ~/.local/share/bay/framework
~/.local/share/bay/framework/bootstrap.sh          # needs git and uv
bay fleet init acme --from git@github.com:acme/acme-fleet.git
cp <the vault password you were given> ~/.config/bay/fleets/acme/.vault_pass
git clone git@github.com:acme/shop.git ~/code/shop
cd ~/code/shop
bay doctor production                     # fleet, vault, boxes, repo, fleet clone
bay plan production
```

This is the install of [install.md](install.md#install), exactly. `bootstrap.sh` does the Python
packages, the Ansible roles and collections, and the `bay` command. `uv tool install --editable`
alone is not enough to deploy. If the shell cannot find `bay`, run `uv tool update-shell`.

- **The vault password** comes from the operator, out of band. Bay never stores it and no
  repo holds it. It is the one file `.vault_pass` in the root of the fleet clone, here
  `~/.config/bay/fleets/acme/.vault_pass` ([install.md](install.md#the-vault-password)). After you
  copy it, run `git status` in the fleet. `bay fleet init --from` clones the repo and leaves its
  `.gitignore` as it is, and a fleet that was made with `bay fleet init <name>` ignores only
  `.bay-cache/`. If `.vault_pass` shows as untracked, add the line `.vault_pass` to the `.gitignore`,
  commit it and push it: an untracked file makes the fleet dirty, and the receipt records
  `fleet_dirty: true`. Without the file `bay doctor` fails its Vault check, and `bay vault edit` and
  every deploy stop. `bay doctor` checks that the vault opens. It prints no name and no value.
- **SSH access to the boxes** and **tailnet membership** also come out of band.
- **The fleet name.** `--from <url>` clones into the folder `~/.config/bay/fleets/acme`. That
  folder name, the `name` in `bay.fleet.toml` and the `fleet = "acme"` line of every `bay.toml`
  must be the same word ([install.md](install.md#the-three-names-of-a-fleet)). If the repo says another
  `name`, use that name as the folder, or use `--fleet <path>` or `BAY_FLEET=<path>` for a clone you
  made yourself.
- `.bay-cache/` holds its own `.gitignore`, so git never shows the fleet cache. You need
  no line in the fleet's `.gitignore` for it.
- Do not run `bay init` again. The lock is already in the fleet. The first `bay plan`
  fetches the app code into the fleet cache (scenario 2). You need read access to the app
  repo from this machine.
- **Push first.** `bay up` refuses a commit that is on no branch of the remote.
- **Pull the fleet first.** A stale clone can revert config. The plan blocks when the
  fleet is behind its remote, and `bay doctor` warns.
- The command is an editable install: the code that runs is the checkout. A framework edit on
  this machine takes effect at once. See
  [install.md](install.md#development-mode-the-editable-install).

## 15. Tailnet route

A route sends `notes.ts.acme.example` over the tailnet to a tool on a laptop. A route is
rig routing, not an app. It has no `bay.toml`, no lock and no repo. It sits in
`bay.fleet.toml`.

```toml
[tailnet]
ingress_box = "infra"                     # the box that serves the routes
cert_domain = "*.ts.acme.example"         # one wildcard certificate (DNS-01)

[tailnet.routes.notes]
domain = "notes.ts.acme.example"          # under cert_domain
upstream = "http://devbox.acme.tailnet.internal:8787"   # tailnet IP, one label, or a name under .tailnet.internal or .ts.net
host = "upstream"                         # upstream | client (default client): the Host header the upstream sees
identity = true                           # inject the X-Tailnet-Device header
```

**The procedure.** The ordered list is in one place: [tailnet-ingress.md, Add a route, in
order](tailnet-ingress.md#add-a-route-in-order). In short: pull the fleet, then edit the group_vars
by hand. Two kinds of edit go there. The ingress box prerequisites (DNS-01, the token, the identity
sidecar) are done once per fleet, before the first route. The two ACL edits are done for each
route, and only when the fleet has `headscale_acl_policy`. Then validate, commit and push those
edits, deploy the ACL (`bay deploy production --tags headscale`, only with the policy), then
`bay route add`, `bay validate` again (now it sees the route and checks the ACL for its port),
`bay plan`, `bay approve`, `bay up`. `bay route add` edits and commits `bay.fleet.toml` only. It never
touches the ACL or any group_vars file, `bay plan` shows no step for them, and `bay up` deploys them
from the working tree without committing them, so commit and push them yourself (the receipt records
`fleet_dirty: true` until you do). The prerequisites (DNS-01, the Cloudflare token, the identity
sidecar) are listed in
[tailnet-ingress.md](tailnet-ingress.md#before-the-first-route-the-ingress-box-prerequisites), and the
route's `bay up` deploys them. `bay route add` needs `--ingress-box` and `--cert-domain` the first time.

The `route` verbs skip the fleet directory you stand in (rule 5 of
[install.md](install.md#pick-a-fleet)), so each command here names the fleet with `--fleet`. You may
leave it out when `BAY_FLEET` is set, or when you run from an app repo whose `bay.toml` names the
fleet. `add` and `rm` keep the comments of `bay.fleet.toml` and commit the fleet repo (`--no-commit`
only edits):

```
F=~/.config/bay/fleets/acme
bay --fleet $F route add notes --domain notes.ts.acme.example \
    --upstream http://devbox.acme.tailnet.internal:8787 --host upstream --identity \
    --ingress-box infra --cert-domain "*.ts.acme.example"      # the last two: first route only
bay --fleet $F route ls
bay --fleet $F route rm notes
bay --fleet $F plan production            # steps: route_added, route_changed, route_removed (risk: shared)
bay --fleet $F approve <plan-id> --reason "<why>"
bay --fleet $F up production --plan-id <plan-id>   # runs the deploy_stack, headscale and traefik tags
```

**Which env name `plan` and `up` take for a route.** A route has no project and no deploy env of its
own, but `bay plan` and `bay up` take a deploy env, and they need a project in it. Give the deploy env of
a project that deploys on a box of the ingress box's box env (`production` here, if `shop` deploys there
as `[deploy.production]` on `infra`'s box env). A plan for another box env is blocked while a route
change is pending. If no project deploys on the ingress box's env, there is no route-only plan (a gap:
`bay up` stops with "no project has [deploy.production]"). Run from the fleet directory,
`bay plan production` covers the whole env and `bay up production` pins every covered project to its
WANTED commit, so the route ships every pending app change too. Run `bay plan production` first and
read all the steps, not only the route step. To ship the route with one project only, run `bay up` from
that app repo, or with `--project <name>`: it pins only that project (plan.md, [The whole
environment](plan.md#the-whole-environment)).

**What the tags do.** A `bay up` with a route step runs `--tags deploy_stack,headscale,traefik` on every
host of the ingress box's box env. `deploy_stack` covers the Traefik role (the DNS-01 resolver, the token
file, the route file) and the identity sidecar. The `headscale` tag runs the tasks of the Headscale
role: the split-DNS records, and the ACL render when the fleet defines `headscale_acl_policy`. So that
`bay up` also deploys ACL edits you made by hand, on the hosts of that env that are the Headscale
server. Make `ingress_box` the Headscale control host: Bay does not check it, and a Headscale host in
another box env is never reached by this `bay up` (deploy it with
`bay deploy <its box env> --tags headscale`). A plain `bay up` with no route step runs `deploy_stack`
only, which does not render the ACL. Deploy the ACL first with `bay deploy <box env> --tags headscale`
anyway, as the steps of [tailnet-ingress.md](tailnet-ingress.md#add-a-route-in-order) say: a bad policy
then fails before the route changes.
A fleet that still has `group_vars/all/tailnet_proxies.yml` (the method from before 2.1) runs
`bay route import` once. It moves the routes into `bay.fleet.toml` and deletes the file. The
first `bay up` shows one `route_added` step per route and recreates no container.

On the ingress box (`infra`), Traefik serves the route on the tailnet entrypoint,
Headscale holds the split-DNS record, and CrowdSec reads the router name. A change in DNS
records restarts Headscale, and the plan says so. A plan for an env other than the ingress
box's is blocked while a route change is pending. `bay validate` checks that the domain is
under `cert_domain`, the box is the ingress box, the upstream is a tailnet name or IP with a
port, and no domain repeats. The upstream is never `localhost`. The upstream host form and how to
read a node's name are in [tailnet-ingress.md](tailnet-ingress.md#the-route-keys).

`bay validate` warns when no ACL rule has the ingress box as its only `src` for that port. It
is a warning, because allow-all mode is legal. If no rule lets the ingress box reach the port,
the route answers with a 502 or a timeout, and the ACL leaves no log line.

`bay show --routes` lists WANTED, PINNED and RUNNING per route. RUNNING reads the receipt of
the ingress box. No receipt lists routes yet, so RUNNING is `unknown` today, and `bay status`
does not show routes. See [tailnet-ingress.md](tailnet-ingress.md#routes-in-bayfleettoml-21).

## Bay does not do

- Canary or blue-green releases. `zero_downtime = true` makes old and new overlap for one
  container swap. It is not a traffic split.
- Needs between projects of two different fleets.
- A restart-policy knob. Bay picks the restart policy of every container.
- `--data move` (planned). Moving a volume or a database across boxes is by hand (scenario 12).
- `bay init --secrets` (planned). Secret names go into the vault with `bay vault edit`.
- Tailnet DNS names for containers. A container has no name on the tailnet. Use a tailnet
  route (scenario 15) or the injected `<NAME>_URL`.
- Delete data. `bay remove` and a box move stop containers and print the `docker volume rm`
  and `DROP DATABASE` lines. You run them.

## Removing a project

`bay remove shop` plans it. `bay remove shop --env staging` plans one env, but only after you
delete that env from the app: it is blocked while the `bay.toml` still has `[deploy.staging]`,
because the next `bay up` would start the env again. Delete the `[deploy.staging]` table, commit it
and push it (for an app in a repo, Bay reads the commit and refuses one the remote lacks), then run
`bay remove shop --env staging`. The plan has one
`remove` step per container, risk `destructive`, so the verdict is `approve`:

```
bay remove shop
bay approve <plan-id> --reason "shop is retired"
bay up production --plan-id <plan-id>
```

`bay remove shop` takes out every env of the project. In `bay up <env> --plan-id`, `<env>` is
only a guard: Bay refuses a name that is neither the plan's env nor one of the planned envs. It
does not limit the removal, so `bay up staging --plan-id <id>` applies the whole plan too. The deploy
runs once for each distinct box env of the removed envs (a project in staging and production on one
box env is deployed once).

Only a receipt that confirms the containers are gone deletes the env from the lock. With no
env left, `projects/shop/` leaves the fleet. The volumes and the database stay. The weekly
`docker system prune -af --volumes` keeps them too, because it removes only anonymous volumes on
Docker Engine 23 and later. See
[plan.md](plan.md#bay-remove).

**The app side.** `bay remove` changes the fleet and the boxes only. It never touches the app
repo, the GitHub hook or the `bay.toml` in the repo: they stay as they were. After a full remove
the lock is gone, so `bay plan` from that repo stops with "project shop is not in fleet acme"
(`bay init` refuses a repo that still has a `bay.toml`, so to register the app again, move the file
aside, run `bay init`, and restore it). The compiled file no longer lists the project, so the
box's webhook receiver gets its service list and image map without it, so a push has no service to build. The docs
make no promise that the old webhook script file on the box is deleted. Remove the GitHub hook
yourself: `bay service prune-webhooks <owner>/<repo>` lists the hooks that no service claims and deletes
them on request (it needs `github_admin_token` in the vault). Delete the `bay.toml` from the repo when you
no longer want it.

## What never goes where

- No secret value in any `bay.toml` or in `bay.fleet.toml`. Only names. Values are in the vault.
- No box address, SSH user or tailnet IP in an app repo. Those are fleet facts.
- No machine path in a lock. A lock holds the repo URL, never a folder on your disk.
- No tailnet name in an app repo. A route lives in `[tailnet.routes.*]` in the fleet.
- No hand edit of `group_vars/all/services.yml`. `bay compile` refuses the next run.
- No `rm -r plans/`. The next receipt reports the fleet as dirty. Bay prunes `plans/` by
  itself and keeps the last 50 plus every plan a lock still names.
- Rig parts (Traefik, CrowdSec, Headscale, Zot, webhook receiver) are not projects. They stay in roles and `group_vars`.
