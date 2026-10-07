# bay.toml reference (schema v3)

`bay.toml` lives in the app repo. It says WHAT the app is and WHERE it deploys. The fleet
repo holds everything else: boxes, secret values, shared resources and the per-project
lockfile. Only the `bay` CLI writes the fleet repo.

Status: the schema and the validator ship today, and so do the commands that act on the
file (`bay init`, `bay plan`, `bay up`, `bay show`, `bay rollback`, see [plan.md](plan.md)).
This page is the contract they follow. `bay up` deploys the whole box environment, not one project.

**Validated is not deployed.** The validator accepts a few keys that the compiler cannot deploy
yet. In the Keys tables below, each of them carries the tag `(validated, not deployed yet)`.
`bay plan` and `bay compile` list a file that uses one in `unsupported` and block it, unless you
pass `--allow-unsupported`, and then the container runs without that feature. The list is
[plan.md, Features Bay cannot deploy yet](plan.md#features-bay-cannot-deploy-yet), and the
`unsupported` field of a plan is the truth for one file.

## Check a file

```bash
bay toml validate              # checks ./bay.toml
bay toml validate path/to/bay.toml
bay toml validate --json bay.toml
```

- A valid file prints nothing and exits 0.
- An invalid file prints one line per violation and exits 1. Each line is
  `<path>: <message>`, for example `services.api.port: must be an integer`.
- Paths use dots for tables and `[n]` for arrays. Array indexes count from 0, so
  `mounts[0]` is the first `[[mounts]]` block.
- A file that is not valid TOML gives one line that starts with the file name.
- `--json` prints `{"ok": true|false, "violations": [{"path": ..., "message": ...}]}`
  with the same exit codes.

The check reads only the file. It needs no fleet, no network and no repo checkout. The
schema itself is `src/bay_cli/schemas/bay_toml.schema.json` (JSON Schema draft 2020-12).

## Import a fleet from today's YAML files

```bash
bay import --fleet ./fleet --out ./new-fleet   # writes the new layout, reads ./fleet only
bay import --fleet ./fleet --check             # writes nothing; exit 1 on a difference
bay import --fleet ./fleet --check --diff      # also prints the diff of each container
```

- `bay import` writes `bay.fleet.toml` (with `format = 2`), and one folder per
  project: `projects/<name>/bay.toml`, `projects/<name>/bay.lock` with every adopted
  name, and the config files the project mounts from `files/<name>/` today, now beside
  its `bay.toml`. Other config files (a path outside `<name>/`, the resources' files)
  are copied under `files/`. `--out` must be empty or new.
- Each container becomes a project with the same name. `<name>-prod`, `-staging` and
  `-dev` become an environment of `<name>`. `<name>-<suffix>` with the same repo or
  image becomes `[services.<suffix>]` when the result is exact. The report lists every
  decision.
- The main environment of a project is named after the box environment of its box (the
  `env` key of the box in `bay.fleet.toml`, see
  [layout-scenarios.md](layout-scenarios.md#words-deploy-env-box-env-and-group)). A box
  with `env = "testing"` gives `[deploy.testing]`. `-staging` and `-dev` keep their names.
  When every box has one `env`, that name is also `primary_env` in `bay.fleet.toml`, so
  container names stay as they are. `primary_env` is the deploy env that gets bare
  container names (`shop`, not `shop-production`). Its default is `production`. Rule 8
  below has the naming rules.
- A value `http://<container>:<port>` that reaches another project becomes a need with
  `env` set to today's variable name, and the other project gets `publish = true`.
- The report lists each secret to add (for example a password that is plain text
  today) and each value it cannot carry over exactly (`FLAG:`). It never prints a value.
- `--check` imports into a scratch directory, compiles, and renders today's file and the
  compiled one through the deploy code. It prints each container whose settings differ.

## Rules

1. No templating. No variables, no `${}`, no `{{ }}`. A value is a literal or a name.
2. Durations and sizes are strings with units. Durations use `h`, `d` or `w` (`7d`).
   Sizes use `k`, `m` or `g` (`512m`, `2g`). Rates use `/s` or `/m` (`100/s`).
3. Unknown keys fail, in every table.
4. All top-level keys come before the first table. TOML puts a key written below a
   `[table]` header into that table, so a misplaced key fails as an unknown key.
5. The top level sets `image` or `[build]`, never both. If it sets neither, Bay builds
   `./Dockerfile`. A service (`[services.<name>]`) sets `image` or an inline `build`, never
   both, and with neither it takes the image of the top level. An environment may
   override `image` or `build.args`, never both (see `[deploy.<env>]`).
6. `needs` is a list or `[needs.<name>]` tables. One file uses one form everywhere,
   in the top level and in every service.
7. `name` uses `a-z`, `0-9` and `-`, and starts with a letter or digit. It must not end
   in `-<env>` for any environment in `[deploy]`. `name` never changes: a new name is a
   new project.
8. The main container is named `<name>` in the primary environment and `<name>-<env>`
   in the others. Each extra service `[services.<s>]` is `<name>-<s>` in the primary
   environment and `<name>-<env>-<s>` in the others. The primary environment is
   `primary_env` of `bay.fleet.toml`; its default is `production`. A file with no table
   for the primary environment has none: every environment then carries its suffix. The
   key `web` stands for the main container in the rules (it is the `WEB_URL` name), so
   no extra service may be named `web`. A lock that adopts a container name keeps that
   name. A service name must not start
   with `<env>-` for any environment, because the names would collide. It must not start
   with `job-` either, because job containers use that prefix (see Behavior). A volume
   name must not start with `<env>-` for any environment, for the same reason.
9. Path lists (`open`, `locked`, `path`) start with `/`. They match the exact path or a
   prefix on a segment boundary: `/admin` matches `/admin` and `/admin/x`, not
   `/administrator`. The longest match wins.
10. `health` is required when `[access.password]` is set, at any level. Write a path or
    `"none"`. A routed health path skips the password by exact match. See Behavior for
    how Bay probes the path.
11. `port` is required when the main container takes traffic (`access.mode` is `public`
    or `tailnet`), and for a service with `path`, `domain` or `expose`. A health path
    also needs a port to probe.
12. At least one `[deploy.<env>]` table. Environment names use `a-z` and `0-9` only.
13. No domain appears twice in the file, across all environments, aliases and services.
    At most one routed environment may leave out `domain`, because two would get the
    same default domain.
14. One variable name comes from one place in a level: `env`, `secrets` or
    `fleet_secrets`.
15. A container may not declare a variable that Bay injects into it. Every service with a
    `port` injects `<SERVICE>_URL` into its siblings, and the main container injects
    `WEB_URL`. These names may not also appear in `env`, `secrets`, `fleet_secrets` or a
    need (`DATABASE_URL`, `REDIS_URL`, `<NEED>_URL` or a need's `env` name). The check
    reports the path `services.<name>` of the service to rename.

## Behavior

Each line below is a rule the commands that act on the file must follow. The validator
cannot check most of them. They are here so that one reading of the file is possible.

Data names and volumes:

- Adopted data names (database, database user, volumes, and the directory a `from`
  mount uses on the box) are per environment and live only in the fleet lockfile, which
  `bay show` prints.
- bay.toml never names an adopted database, user or volume.
- Volume names are `<name>-<volume>` in the primary environment and
  `<name>-<env>-<volume>` in the others.
- A volume name is shared by the whole project. Two services that mount the same volume
  name share one volume.

Jobs and release (both are validated, not deployed yet: this is the behaviour they are meant to have):

- Jobs run as one-shot containers named `<name>-job-<job>` in the primary environment
  and `<name>-<env>-job-<job>` in the others.
- A job gets the image, env, secrets, `fleet_secrets`, needs and mounts of the main
  container.
- A job may set `memory`.
- A failed `release` aborts the deploy before traffic moves.
- `release` runs once per environment per deploy, inside a one-shot container of the new
  image.

Health and deploy:

- Bay probes the health path directly on the container, not through the route.
- The password bypass for the health path matters only when the path is also routed.
- `health` for a service with a `port` defaults to `/`.
- The check gates that service's deploy the same way as the main container's check.
- When a health check fails during a deploy, Bay removes the new container and starts the
  previous container again.
- With `zero_downtime = false` there is a gap while the previous container is down.
- With `zero_downtime = true` the previous container never stopped, so there is no gap.

Services:

- A service inherits `update` and `logs` from the project.
- `replicas` defaults to `1` and `zero_downtime` defaults to `false` for each service.
- A service may set `replicas`, `zero_downtime`, `update` and `logs` itself.
- Every service that has a `port` gets a `<SERVICE>_URL` variable in every other service
  of the same environment. The name is uppercase and `-` becomes `_`. The main container
  is `WEB_URL`.
- A service with `inherit = false` receives no such variable.
- Each environment has its own container network, so a bare service name never crosses
  environments.
- A service with an inline `build` inherits nothing from the project `[build]`: no args,
  no secrets, no watch and no memory.
- A service that inherits the image shares the one build.

Routing and access:

- With `expose`, the box publishes the container port on the same port number.
- bay.toml never names a box port.
- The bind address is the box loopback address or the box tailnet address, never
  `0.0.0.0`.
- `locked` paths also need the password when `[access.password]` is set.
- An `open` path stays open in every environment, unless that environment overrides
  `access.open`.
- `[access.identity]` is valid in `public` mode.
- Bay sets the identity header only on requests that arrive from the tailnet. It strips
  the header on all others.
- An alias redirect is HTTP 308 and keeps the path and the query.
- Bay issues a certificate for every alias.

Secrets:

- `fleet_secrets` values are shared across environments by default.
- The fleet may hold a different value for one environment. The project does not see the
  difference.

## Full example

This is the corrected v3 example. It passes `bay toml validate`. The test suite checks
that this block and `tests/fixtures/bay_toml/corrected-example.toml` stay identical. It is a
schema example, not a file to deploy as it stands: it uses keys that carry the tag
`(validated, not deployed yet)` in the Keys tables (`release`, `[backup]`, `[[jobs]]`,
`aliases`, `owner`, `backup`, `extensions`, a service `path`, `[access.identity]`, a build
`memory` and `open` beside a password), so `bay plan` blocks it without `--allow-unsupported`.

<!-- corrected-example -->
```toml
# ── Identity ───────────────────────────────────────────────
name = "myapp"                 # [a-z0-9-]; prefixes containers, databases, volumes, secret names
fleet = "myfleet"              # which fleet repo
publish = false                # true = other projects may `needs = ["myapp"]` (fleet injects MYAPP_URL)

# ── Image (this example builds; `image = "ghcr.io/org/app:1.4"` is the other form) ──
# ── Runtime ────────────────────────────────────────────────
port = 3000                    # container port that receives traffic
command = "node server.js"     # default: image CMD
release = "npx prisma migrate deploy"  # once per deploy before traffic moves; failure aborts, old container stays
health = "/healthz"            # default "/"; "none" disables. Bay probes it on the container, not through the route.
                               # Required explicit when [access.password] is set. The route bypasses the password for it only if the path is routed.
replicas = 1
memory = "512m"                # default none; swap never allowed
update = "notify"              # notify | auto | off  (default notify)
zero_downtime = false          # true = old and new overlap; refused if a mount is single-writer
logs = "7d"                    # "off" | duration; log archive on the box (rotation always on, fleet cap)
secrets = ["SESSION_SECRET"]   # names only; the fleet holds the values, one vault file per box env

# ── Needs (table form here; `needs = ["postgres", "redis"]` when no need has options) ──
[needs.postgres]
env = "GF_DATABASE_URL"        # alias injected beside DATABASE_URL. Own database and user per environment.
extensions = ["vector"]
# Adopted database, user and volume names are per environment. They live only in the fleet lockfile (`bay show`).
[needs.redis]                  # REDIS_URL, own key namespace per environment
[needs.pigeon]                 # a published project; injects PIGEON_URL; fleet decides network path and ACL
# Removing a need never deletes data; the plan marks that step destructive and separate.

[build]
dockerfile = "Dockerfile"      # default
context = "."                  # default, relative to repo root
strategy = "local"             # local | remote | registry; default = fleet setting
memory = "2g"                  # build memory cap; default = fleet setting
watch = ["src/**", "package.json"]   # include list; a push touching nothing here does not rebuild
ignore = ["*.md", "docs/**"]   # exclude list; applied after watch
[build.args]
NODE_ENV = "production"
[build.secrets]
npmrc = "NPMRC"                # BuildKit secret id = secret NAME from the fleet

[env]                          # plain config, committed
LOG_LEVEL = "info"

[fleet_secrets]                # container var name = fleet secret name (shared values, one rotation)
TURNSTILE_SECRET_KEY = "TURNSTILE_SECRET_KEY"
BW_CLIENTID = "SHARED_BW_CLIENT_ID"

# ── Access ─────────────────────────────────────────────────
[access]
mode = "public"                # public | tailnet | internal
                               # public   = internet, HTTPS via Let's Encrypt
                               # tailnet  = only devices on the fleet VPN (compiles to `access: vpn`)
                               # internal = no route; reachable only by containers that need it
open   = ["/webhooks"]         # paths opened to public; bypass mode AND password
locked = ["/admin"]            # paths locked to the tailnet
# A locked path also needs the password when [access.password] is set. An open path stays open in every environment.
[access.limits]                # per source IP; default = fleet setting
rate = "100/s"
burst = 50
concurrent = 100
[access.password]              # HTTP basic auth on everything not in `open`; passwords are secrets PASSWORD_<USER>
users = ["admin"]
realm = "Staff only"
[access.identity]              # valid in any mode; set only on requests from the tailnet, stripped on all others
header = "X-Tailnet-Device"

# ── Data ───────────────────────────────────────────────────
[[mounts]]                     # exactly one of volume / from
path = "/app/data"             # inside the container
volume = "data"                # bay-managed named volume; backed up; survives recreation
owner = "472:472"              # default root
[[mounts]]
path = "/app/cache"
volume = "cache"
backup = false
[[mounts]]
path = "/etc/app/config.yaml"
from = "deploy/config.yaml"    # file or directory beside this bay.toml, read-only; a change recreates the container
mode = "0644"                  # default 0600

[backup]                       # managed mounts with backup = true (databases follow the fleet policy)
keep = "30d"                   # default fleet setting
hour = 3                       # UTC; default fleet setting

# ── Scheduled jobs (image, env, secrets, fleet_secrets, needs and mounts of the main container) ──────
[[jobs]]
name = "cleanup"
schedule = "0 2 * * *"         # UTC cron
command = "node dist/cleanup.js"
memory = "256m"                # default none; the job is a one-shot container <name>-job-cleanup

# ── Extra services ─────────────────────────────────────────
# Inherit by default: image/[build], env, secrets, fleet_secrets, needs. `inherit = false` starts empty.
# Inherit update and logs from the project; replicas = 1 and zero_downtime = false unless the service sets them.
# Own: command, port, health, memory, replicas, zero_downtime, update, logs, access, mounts, env, secrets, needs, image/[build].
# No `domain` and no `path` = internal. Routed services default to the project mode.
# Password and limits are never inherited.
# Siblings reach each other at http://<service>:<port>. Every service with a port gets <SERVICE>_URL (WEB_URL for the main
# container) in every other service of the same environment, except services with inherit = false, which get none.
[services.worker]
command = "node dist/worker.js"
memory = "256m"
update = "off"                 # own value; without it the service takes the project update
[services.api]
build = { dockerfile = "apps/api/Dockerfile", context = "apps/api" }
port = 4000
path = "/api"                  # routed on the main domain by prefix
replicas = 2
zero_downtime = true           # the previous container keeps running until the new one is healthy
[services.api.access.limits]
rate = "2/s"
[services.admin]
port = 5000
domain = "admin.example.com"   # a second hostname is a service, not a route; override per env below
[services.browser]
inherit = false                # sidecar: none of the project's secrets or needs
image = "ghcr.io/steel-dev/steel-browser:latest"
port = 3000
expose = "loopback"            # loopback | tailnet; the box port is the container port. Never 0.0.0.0.

# ── Deploy targets (one table per environment; each env = own data + secret values) ──
[deploy.production]
box = "eu-1"                   # default: fleet default box
domain = "app.example.com"     # default: <name>.<fleet default domain>
aliases = ["example.com"]      # HTTP 308 to domain, path and query kept; Bay issues a certificate per alias. redirect = false serves both
branch = "main"                # webhook builds follow this branch; default main
[deploy.staging]
box = "eu-1"
domain = "staging.example.com"
branch = "develop"
access.mode = "tailnet"        # any runtime / access / build.args / env key may be overridden per environment
services.admin.domain = "admin-staging.example.com"   # the plan refuses a duplicate domain on one box
[deploy.staging.env]
LOG_LEVEL = "debug"
```

Two faults made the draft example invalid, and the corrected example removes both. The
draft set `image` and a `[build]` table at the same level. It also wrote
`needs = ["postgres", "redis", "bucket"]` beside `[needs.postgres]` tables, which TOML
itself rejects. The draft is kept as `tests/fixtures/bay_toml/draft-v2-example.toml`.

## Keys

### Identity

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `name` | name | required | Project name. Prefixes containers, databases, volumes and secret names. Never changes. |
| `fleet` | name | required | The fleet this project deploys into. |
| `publish` | bool | `false` | Other projects may list this one in `needs`. The fleet then injects `<NAME>_URL` into them. |

### Image and runtime

These keys sit at the top level. Every key in this table may also be set in a
`[deploy.<env>]` table to override it for one environment.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `image` | string | none | Pull this image instead of building. Not together with `[build]`. |
| `port` | integer 1-65535 | none | Container port that receives traffic. |
| `command` | string | image CMD | Command to run. |
| `release` | string | none | (validated, not deployed yet) Runs once per environment per deploy, in a one-shot container of the new image, before traffic moves. A failure stops the deploy and the old container stays. |
| `health` | string | `"/"` when there is a port, else `"none"` | Health check path, or `"none"`. Bay probes it on the container, not through the route. A failed check removes the new container and starts the previous one again. A path other than `/` on an internal container (no `domain`, no `path`) is (validated, not deployed yet). |
| `replicas` | integer >= 1 | `1` | Number of containers. More than 1 on an internal container is (validated, not deployed yet). |
| `memory` | size | none | Memory limit. Bay sets `mem_limit` and `memswap_limit` to this one value, so the container gets no swap (a `docker run --memory-swap` equal to `--memory`). A container that ran with `mem_limit` alone is recreated once by the first deploy of the compiled file, and the box prediction names it as `memory: memswap_limit <now> -> <memory>` (see [plan.md](plan.md#the-box-prediction---remote)). |
| `update` | `notify`, `auto`, `off` | `notify` | What happens when a newer image appears. |
| `zero_downtime` | bool | `false` | `true`: the previous container keeps running until the new one is healthy. `false`: there is a gap. `true` on an internal container is (validated, not deployed yet). |
| `logs` | `"off"` or duration | fleet setting | How long the log archive on the box keeps logs. Rotation is always on. |
| `secrets` | list of names | `[]` | Secret names. The values are in the encrypted secrets file of the box env (`group_vars/<box env>/secrets.yml`), so two deploy envs on one box env share one value per name. |

Two more top-level keys apply to the main container. An environment cannot override
them.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `expose` | `loopback`, `tailnet` | none | Publish `port` on the box: on its loopback address or its tailnet address. Needs `port`. |
| `log_rotation` | table: `max_size` (size), `max_file` (integer) | fleet setting | Docker log rotation for this container: the size of one log file and how many files to keep. |

### `needs`

List form, when no need has options:

```toml
needs = ["postgres", "redis", "pigeon"]
```

Table form, when one need has options:

```toml
[needs.postgres]
env = "GF_DATABASE_URL"
[needs.redis]
```

| Need | Injects | Options |
|------|---------|---------|
| `postgres` | `DATABASE_URL` | `env` (an extra variable name with the same value), `extensions` (list; (validated, not deployed yet)) |
| `redis` | `REDIS_URL` | `env` |
| any other name | `<NAME>_URL`, or the `env` name instead | `env` (the variable name to use instead of `<NAME>_URL`). The name is a published project, or a shared resource the fleet defines. |

Each environment gets its own database and user. A project cannot need itself. A need of a
shared resource that sits on another box than the project's deploy env is (validated, not deployed yet) (it is
the "cross-box" case of [layout-scenarios.md](layout-scenarios.md#5-several-boxes-shared-resources-mixed-apps)).
`needs.postgres` on an internal container is (validated, not deployed yet) too. Removing a
need never deletes data. Adopted names are not options here: they live in the fleet
lockfile (see Behavior).

### `[build]`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `dockerfile` | string | `Dockerfile` | Path to the Dockerfile. |
| `context` | string | `.` | Build context, relative to the repo root. |
| `strategy` | `local`, `remote`, `registry` | fleet setting | Where the image is built. |
| `memory` | size | fleet setting | Build memory cap. (validated, not deployed yet) when it differs from the fleet's build memory. |
| `watch` | list of globs | everything | A push that touches none of these files does not rebuild. This filter runs first, before the config-only and hold checks, and it filters a push of `bay.toml` too (see [build-pipeline.md](build-pipeline.md#order-of-the-guards-on-a-push)). |
| `ignore` | list of globs | none | Files that never trigger a rebuild. Applied after `watch`. |
| `[build.args]` | table of strings | none | Build arguments. |
| `[build.secrets]` | table | none | BuildKit secret id = fleet secret name. |

An extra service may write `build` as an inline table:
`build = { dockerfile = "apps/api/Dockerfile", context = "apps/api" }`. A service with an
inline `build` inherits nothing from this table: no args, no secrets, no watch and no
memory. A service that inherits the image shares the one build.

### `[env]` and `[fleet_secrets]`

- `[env]` is plain configuration, committed in the repo. Keys are uppercase variable
  names. Values are strings, so quote numbers and booleans: `PORT = "3000"`.
- `[fleet_secrets]` maps a container variable name to a fleet secret name. Use it for
  values that several projects share, so one rotation updates them all. The values are
  shared across environments by default.

### `[access]`

A file with no top-level `[access]` table, or one without `mode`, fails the check with
`access.mode: is required`. There is no default mode. `bay init` writes `mode = "public"`.
A service with no `domain` and no `path` is internal whatever the project mode is.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `mode` | `public`, `tailnet`, `internal` | none: the top-level `[access]` table and its `mode` are required | `public`: the internet, HTTPS via Let's Encrypt. `tailnet`: only devices on the fleet VPN. `internal`: no route; only containers that need it can reach it. |
| `open` | list of paths | `[]` | Open to the public. These paths skip the mode and the password. They stay open in every environment unless that environment overrides `access.open`. Together with `[access.password]` it is (validated, not deployed yet). |
| `locked` | list of paths | `[]` | Only reachable from the tailnet. They also need the password when `[access.password]` is set. A path may not be both open and locked. |
| `[access.limits]` | `rate`, `burst`, `concurrent` | fleet setting | Per source IP. |
| `[access.password]` | `users` (required), `realm` | none | HTTP basic auth on everything not in `open`. The password for user `admin` is the secret `PASSWORD_ADMIN`. |
| `[access.identity]` | `header` (required) | none | (validated, not deployed yet) Valid in every mode, `public` included. The app receives the caller's device name in this header. Bay sets the header only on requests that arrive from the tailnet and strips it on all others. |

### `[[mounts]]`

Each mount sets `path` and exactly one of `volume` or `from`.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `path` | absolute path | required | Where the mount appears inside the container. |
| `volume` | name | none | A named volume that Bay manages. It survives recreation. |
| `from` | path | none | A file or directory relative to the directory of this `bay.toml`, read-only. A change recreates the container. No leading `/` and no `..`. `fleet:<path>` mounts the shared fleet file `files/<path>` instead. |
| `owner` | `uid` or `uid:gid` | root | (validated, not deployed yet) Volume mounts only. |
| `backup` | bool | `true` | Volume mounts only. Every volume mount with `backup` true, which is the default, is (validated, not deployed yet): write `backup = false` to deploy it. |
| `mode` | octal string | `"0600"` | `from` mounts only. |

Two mounts in one container may not use the same `path`. A volume name is shared by the
whole project. Two services that mount the same volume name share one volume.

**Move mounted files beside the `bay.toml` now.** 2.1 still reads the old place,
`files/<name>/<from>` in the fleet, as a fallback and prints a note. A later release reads
only the place beside the toml. The code and the changelog name no release for that yet,
so do not wait for one: run `git mv` as soon as you see the note.

Where `from` is read:

- **Beside the toml.** `from` is relative to the directory of the `bay.toml`, in an app
  repo and in the fleet alike. In a repo with several apps,
  `services/api/bay.toml` with `from = "conf/app.yaml"` reads
  `services/api/conf/app.yaml`. A project that lives in the fleet reads
  `projects/<name>/<from>`. The fleet HEAD decides the file, as the deploy copies config
  files from it: Bay looks first at `projects/<name>/<from>` at the fleet HEAD, then
  beside the toml at the project's pin, then at the old place below, and it is an error
  only when none of them exists.
- **A shared fleet file.** `from = "fleet:crowdsec/whitelist.yaml"` reads
  `files/crowdsec/whitelist.yaml` in the fleet. The prefix is explicit; Bay never
  guesses. On the box the file is `config/crowdsec/whitelist.yaml`.
- **The old place, for one release.** When the file is not beside the toml, Bay still
  reads `files/<name>/<from>` in the fleet, and prints a note that names the file. Move
  it beside the `bay.toml` with `git mv`. 2.1 reads both places; a later release reads
  only the new one. A path that the lock adopts is read first, without a note.
- On the box the file is `config/<name>/<from>`, or `config/<adopted path>` when the
  lock adopts one. Moving a file beside the toml does not change that path, so no
  container is recreated.

**Moving an app from the fleet into its repo.** A project whose `bay.toml` lives in
the fleet (`projects/<name>/bay.toml`) moves into its app repo with `bay adopt <name>`,
run in a checkout of that repo. The `bay.toml` and the files it mounts move together,
so every `from` keeps its meaning beside the toml, and a `fleet:` mount stays in the
fleet. The lock keeps the adopted names, so the next plan shows 0 steps. Then run
`bay up` in that checkout, and `git push` after it: `bay up` takes the unpushed adopt
commit and moves no code, so the push is config only on the box. See
[plan.md, bay adopt](plan.md#bay-adopt).

**Config-only push.** In an app repo, a push that changes only the `bay.toml` and the
files its mounts read (not `fleet:` ones) builds nothing and deploys nothing: no image, no `:latest`
move, no recreate. The box tags the image of the previous commit with the pushed commit, so that
`bay up` to that commit finds its image. Run `bay up` to deploy the change. See [build-pipeline.md](build-pipeline.md), "Config-only push".

### `[backup]`

The whole table is (validated, not deployed yet).

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `keep` | duration | fleet setting | How long backups are kept. |
| `hour` | integer 0-23 | fleet setting | Hour of the daily backup, UTC. |

Databases follow the fleet backup policy.

### `[[jobs]]`

The whole table is (validated, not deployed yet).

| Key | Type | Meaning |
|-----|------|---------|
| `name` | name | Unique among jobs, and not the name of a service. |
| `schedule` | five-field cron, UTC | When the job runs, for example `"0 2 * * *"`. |
| `command` | string | What to run. |
| `memory` | size | Optional memory limit for the job container. |

`name`, `schedule` and `command` are required. A job runs as a one-shot container named
`<name>-job-<job>` in the primary environment and `<name>-<env>-job-<job>` in the others.
It gets the image, env, secrets, `fleet_secrets`, needs and mounts of the main container.

### `[services.<name>]`

An extra container in the same project. The main container is not a `[services.*]` table: the key `web` stands for it, so no
service may use that name. Container names are in rule 8.

- Inherited by default: `image` or `[build]`, `env`, `secrets`, `fleet_secrets`, `needs`.
  `inherit = false` starts the service empty. A service with neither `image` nor its own
  `build` shares the project's build, and that is (validated, not deployed yet).
- The service inherits `update` and `logs` from the project. `replicas` defaults to `1`
  and `zero_downtime` to `false`.
- The service may set its own: `command`, `port`, `health`, `memory`, `replicas`,
  `zero_downtime`, `update`, `logs`, `log_rotation`, `access`, `mounts`, `env`,
  `secrets`, `fleet_secrets`, `needs`, `image` or `build`.
- Routing: `path = "/api"` routes a prefix of the main domain (`path` is (validated, not deployed yet)).  `domain = "..."` gives
  the service its own domain. Set one of them, or neither. With neither, the service is
  internal and may not set `access`.
- A routed service takes the project's `access.mode` unless it sets its own. Password
  and limits are never inherited.
- `expose = "loopback"` or `"tailnet"` publishes the port on the box. It needs `port`.
  The box port is the container port. The bind is the box loopback address or the box
  tailnet address, never `0.0.0.0`.
- Siblings reach each other at `http://<service>:<port>`. Every service with a `port`
  gets `<SERVICE>_URL` in every other service of the same environment, and the main
  container is `WEB_URL`. A service with `inherit = false` receives none.

### `[deploy.<env>]`

One table per deploy environment. Each has its own data and container suffix. Its secret
values come from the encrypted secrets file of the box env of its box (`group_vars/<box env>/secrets.yml`), so two
deploy envs on one box env share them (see
[layout-scenarios.md](layout-scenarios.md#which-env-does-a-verb-take)). `bay plan <env>` and
`bay up <env>` take this name.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `box` | name | fleet default box | The box this environment runs on. |
| `domain` | domain | `<name>.<fleet default domain>` | Main domain. |
| `aliases` | list of domains | `[]` | Extra domains that redirect to `domain` with HTTP 308. The path and the query are kept. Bay issues a certificate for every alias. The redirect is (validated, not deployed yet): with `redirect` left at `true`, `bay plan` blocks, and with `--allow-unsupported` the aliases are served instead. Set `redirect = false` to deploy them. |
| `redirect` | bool | `true` | `false` serves the aliases instead of redirecting them. |
| `branch` | git branch | `main` | Webhook builds follow this branch. |
| `track` | `"branch"` or `"pin"` | `"branch"` | What a push does. `branch`: a push builds and deploys new code under the pinned config. `pin`: a push only builds; `bay up` deploys. |

#### `track`: what a push deploys

Every push that passes the guards before the build builds an image and tags it with its
commit, `<image>:<commit12>` (the first 12 characters of the commit). Those guards run first,
in a fixed order: the `watch` filter, then the config-only rule. A push that fails the filter
or is config-only builds nothing. The order is in
[build-pipeline.md](build-pipeline.md#order-of-the-guards-on-a-push). The image moves to `:latest`, and the
container starts, only when the push may deploy. The pinned config (the
`bay.toml` that `bay up` compiled) always decides the config.

- `track = "branch"` (the default). A push builds and deploys. When the push
  also changes `bay.toml`, the build is **held**: the image keeps only its
  commit tag, the container keeps running, and the alert `build.held` says
  "config changed, run bay up". Bay compares the parsed file, so a comment or a
  key order change is not a config change. `bay up` deploys the held commit
  with its new config.
- `track = "pin"`. Every push that builds is held (the last guard, the hold guard). Only `bay up` deploys, and it deploys
  the image of the pinned commit. The image must be on the box, so push first
  and let the build finish. `track = "pin"` needs the `bay.toml` in the app
  repo; a project in the fleet cannot use it.

`bay rollback` freezes an environment: a push builds but does not deploy,
whatever `track` says, until a `bay up` to a newer commit. See
[plan.md](plan.md) ("Code and config").

The hold guard compares the `bay.toml` of a project whose file lives in the app
repo. A project in the fleet keeps its `bay.toml` in the fleet repo, so a push
to its app repo never changes its config.

Overrides. A `[deploy.<env>]` table may also set:

- any image and runtime key: `image`, `port`, `command`, `release`, `health`,
  `replicas`, `memory`, `update`, `zero_downtime`, `logs`, `secrets`;
- any `access` key, for example `access.mode = "tailnet"`;
- `build.args`, and nothing else under `build`;
- `env`, for example `[deploy.staging.env]`;
- `services.<name>.domain`, and nothing else under `services`.

How overrides combine: tables merge key by key, and a scalar or a list replaces the
value from the top level. `[deploy.staging.env]` adds to and replaces keys of `[env]`.
An `image` override and `build.args` in the same environment conflict. `build.args` in
an environment of a project that pulls an image has no effect, so it fails.

## What lives in the fleet, never in bay.toml

Box addresses and SSH, secret values, shared resource definitions, the prober that may
skip the allowlist, links between boxes, tailnet proxies, the webhook receiver, alert
recipients, CrowdSec, Traefik and Headscale settings, the framework version, the orphan
policy, log rotation caps, the default domain and the default box.

Apps with no repo of their own live as `projects/<name>/bay.toml` in the fleet repo.

### Tailnet routes (`[tailnet.routes.<name>]` in bay.fleet.toml)

A tailnet route gives a machine that Bay does not run (a laptop, a NAS) a name with a
trusted certificate, served by the ingress box. It is a table of the fleet file, not a
project, so a machine name never lands in an app repo.

```toml
[tailnet]
ingress_box = "infra"
cert_domain = "*.ts.example.com"

[tailnet.routes.notes]
domain = "notes.ts.example.com"
upstream = "http://laptop.acme.tailnet.internal:8080"
host = "upstream"     # upstream | client (default client)
identity = true       # inject X-Tailnet-Device
aliases = []
```

`bay compile` writes the table into `services.yml` as `tailnet_proxies:`. Edit it with
`bay route add`, `bay route ls` and `bay route rm`, and move an old
`group_vars/all/tailnet_proxies.yml` in with `bay route import`. `bay route add` edits
`bay.fleet.toml` only, never the ACL. The keys, the form of the upstream name (`laptop.acme.tailnet.internal`
is node `laptop` plus the fleet's MagicDNS suffix), the `allowlist` key, the checks, the ACL edits and the plan
steps are in [tailnet-ingress.md](tailnet-ingress.md#routes-in-bayfleettoml-21).
