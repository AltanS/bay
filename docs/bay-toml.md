# bay.toml reference (schema v3)

`bay.toml` lives in the app repo. It says WHAT the app is and WHERE it deploys. The fleet
repo holds everything else: boxes, secret values, shared resources and the per-project
lockfile. Only the `bay` CLI writes the fleet repo.

Status: the schema and the validator ship today. The commands that act on the file
(`bay init`, `bay up`, `bay plan`) come later in Bay v2. This page is the contract they
will follow.

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

## Rules

1. No templating. No variables, no `${}`, no `{{ }}`. A value is a literal or a name.
2. Durations and sizes are strings with units. Durations use `h`, `d` or `w` (`7d`).
   Sizes use `k`, `m` or `g` (`512m`, `2g`). Rates use `/s` or `/m` (`100/s`).
3. Unknown keys fail, in every table.
4. All top-level keys come before the first table. TOML puts a key written below a
   `[table]` header into that table, so a misplaced key fails as an unknown key.
5. Each level sets `image` or `[build]`, never both. With neither, the level builds
   `./Dockerfile`.
6. `needs` is a list or `[needs.<name>]` tables. One file uses one form everywhere,
   in the top level and in every service.
7. `name` uses `a-z`, `0-9` and `-`, and starts with a letter or digit. It must not end
   in `-<env>` for any environment in `[deploy]`. `name` never changes: a new name is a
   new project.
8. Container names are `<name>-<service>` in the primary environment and
   `<name>-<env>-<service>` in the others. The primary environment is `production`. A
   file with no `production` table has no primary environment. The main container is
   service `web`, so no extra service may be named `web`. A service name must not start
   with `<env>-` for any environment, because the names would collide.
9. Path lists (`open`, `locked`, `path`) start with `/`. They match the exact path or a
   prefix on a segment boundary: `/admin` matches `/admin` and `/admin/x`, not
   `/administrator`. The longest match wins.
10. `health` is required when `[access.password]` is set, at any level. Write a path or
    `"none"`. The health path skips the password by exact match.
11. `port` is required when the main container takes traffic (`access.mode` is `public`
    or `tailnet`), and for a service with `path`, `domain` or `expose`. A health path
    also needs a port to probe.
12. At least one `[deploy.<env>]` table. Environment names use `a-z` and `0-9` only.
13. No domain appears twice in the file, across all environments, aliases and services.
    At most one routed environment may leave out `domain`, because two would get the
    same default domain.
14. One variable name comes from one place in a level: `env`, `secrets` or
    `fleet_secrets`.

## Full example

This is the corrected v3 example. It passes `bay toml validate`. The test suite checks
that this block and `tests/fixtures/bay_toml/corrected-example.toml` stay identical.

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
health = "/api/health"         # default "/"; "none" disables. Required explicit when [access.password] is set.
replicas = 1
memory = "512m"                # default none; swap never allowed
update = "notify"              # notify | auto | off  (default notify)
zero_downtime = false          # true = old and new overlap; refused if a mount is single-writer
logs = "7d"                    # "off" | duration; log archive on the box (rotation always on, fleet cap)
secrets = ["SESSION_SECRET"]   # names only; fleet holds values per environment

# ── Needs (table form here; `needs = ["postgres", "redis"]` when no need has options) ──
[needs.postgres]
env = "GF_DATABASE_URL"        # alias injected beside DATABASE_URL. Own database and user per environment.
extensions = ["vector"]
database = "myapp_prod"        # adopt an existing db name (importer writes this; default <name>_<env>)
role = "myapp"                 # adopt an existing database user name
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
                               # tailnet  = only devices on the fleet VPN (compiles to the fleet allowlist)
                               # internal = no route; reachable only by containers that need it
open   = ["/webhooks"]         # paths opened to public; bypass mode AND password
locked = ["/admin"]            # paths locked to the tailnet
# The health path bypasses the password by exact match. Identity header is stripped on non-tailnet requests.
[access.limits]                # per source IP; default = fleet setting
rate = "100/s"
burst = 50
concurrent = 100
[access.password]              # HTTP basic auth on everything not in `open`; passwords are secrets PASSWORD_<USER>
users = ["admin"]
realm = "Staff only"
[access.identity]              # tailnet only; app receives caller's device name in this header
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
from = "deploy/config.yaml"    # file or directory from this repo, read-only; a change recreates the container
mode = "0644"                  # default 0600

[backup]                       # managed mounts with backup = true (databases follow the fleet policy)
keep = "30d"                   # default fleet setting
hour = 3                       # UTC; default fleet setting

# ── Scheduled jobs (same image, env, secrets, needs) ───────
[[jobs]]
name = "cleanup"
schedule = "0 2 * * *"         # UTC cron
command = "node dist/cleanup.js"

# ── Extra services ─────────────────────────────────────────
# Inherit by default: image/[build], env, secrets, fleet_secrets, needs. `inherit = false` starts empty.
# Own: command, port, health, memory, access, mounts, env, secrets, needs, image/[build].
# No `domain` and no `path` = internal. Routed services default to the project mode.
# Password and limits are never inherited.
# Siblings reach each other at http://<service>:<port>; Bay injects <SERVICE>_URL.
[services.worker]
command = "node dist/worker.js"
memory = "256m"
[services.api]
build = { dockerfile = "apps/api/Dockerfile", context = "apps/api" }
port = 4000
path = "/api"                  # routed on the main domain by prefix
[services.api.access.limits]
rate = "2/s"
[services.admin]
port = 5000
domain = "admin.example.com"   # a second hostname is a service, not a route; override per env below
[services.browser]
inherit = false                # sidecar: none of the project's secrets or needs
image = "ghcr.io/steel-dev/steel-browser:latest"
port = 3000
expose = "loopback"            # loopback | tailnet; publish the port on the box for Traefik or probes

# ── Deploy targets (one table per environment; each env = own data + secret values) ──
[deploy.production]
box = "eu-1"                   # default: fleet default box
domain = "app.example.com"     # default: <name>.<fleet default domain>
aliases = ["example.com"]      # redirect to domain; redirect = false serves both
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
| `release` | string | none | Runs once per deploy before traffic moves. A failure stops the deploy and the old container stays. |
| `health` | string | `"/"` when there is a port, else `"none"` | Health check path, or `"none"`. A failed check keeps and restarts the previous container. |
| `replicas` | integer >= 1 | `1` | Number of containers. |
| `memory` | size | none | Memory limit. Swap is never allowed. |
| `update` | `notify`, `auto`, `off` | `notify` | What happens when a newer image appears. |
| `zero_downtime` | bool | `false` | Old and new containers overlap during a deploy. |
| `logs` | `"off"` or duration | fleet setting | How long the log archive on the box keeps logs. Rotation is always on. |
| `secrets` | list of names | `[]` | Secret names. The fleet holds one value per environment. |

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
| `postgres` | `DATABASE_URL` | `env` (an extra variable name with the same value), `extensions` (list), `database` (adopt an existing database name; default `<name>_<env>`), `role` (adopt an existing database user name) |
| `redis` | `REDIS_URL` | `env` |
| any other name | `<NAME>_URL` | `env`. The name is a published project, or a shared resource the fleet defines. |

A project cannot need itself. Removing a need never deletes data.

### `[build]`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `dockerfile` | string | `Dockerfile` | Path to the Dockerfile. |
| `context` | string | `.` | Build context, relative to the repo root. |
| `strategy` | `local`, `remote`, `registry` | fleet setting | Where the image is built. |
| `memory` | size | fleet setting | Build memory cap. |
| `watch` | list of globs | everything | A push that touches none of these files does not rebuild. |
| `ignore` | list of globs | none | Files that never trigger a rebuild. Applied after `watch`. |
| `[build.args]` | table of strings | none | Build arguments. |
| `[build.secrets]` | table | none | BuildKit secret id = fleet secret name. |

An extra service may write `build` as an inline table:
`build = { dockerfile = "apps/api/Dockerfile", context = "apps/api" }`.

### `[env]` and `[fleet_secrets]`

- `[env]` is plain configuration, committed in the repo. Keys are uppercase variable
  names. Values are strings, so quote numbers and booleans: `PORT = "3000"`.
- `[fleet_secrets]` maps a container variable name to a fleet secret name. Use it for
  values that several projects share, so one rotation updates them all.

### `[access]`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `mode` | `public`, `tailnet`, `internal` | required at the top level | `public`: the internet, HTTPS via Let's Encrypt. `tailnet`: only devices on the fleet VPN. `internal`: no route; only containers that need it can reach it. |
| `open` | list of paths | `[]` | Open to the public. These paths skip the mode and the password. |
| `locked` | list of paths | `[]` | Only reachable from the tailnet. A path may not be both open and locked. |
| `[access.limits]` | `rate`, `burst`, `concurrent` | fleet setting | Per source IP. |
| `[access.password]` | `users` (required), `realm` | none | HTTP basic auth on everything not in `open`. The password for user `admin` is the secret `PASSWORD_ADMIN`. |
| `[access.identity]` | `header` (required) | none | The app receives the caller's device name in this header. The header is removed from requests that do not come from the tailnet. |

### `[[mounts]]`

Each mount sets `path` and exactly one of `volume` or `from`.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `path` | absolute path | required | Where the mount appears inside the container. |
| `volume` | name | none | A named volume that Bay manages. It survives recreation. |
| `from` | repo path | none | A file or directory from this repo, read-only. A change recreates the container. No leading `/` and no `..`. |
| `owner` | `uid` or `uid:gid` | root | Volume mounts only. |
| `backup` | bool | `true` | Volume mounts only. |
| `mode` | octal string | `"0600"` | `from` mounts only. |

Two mounts in one container may not use the same `path`.

### `[backup]`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `keep` | duration | fleet setting | How long backups are kept. |
| `hour` | integer 0-23 | fleet setting | Hour of the daily backup, UTC. |

Databases follow the fleet backup policy.

### `[[jobs]]`

| Key | Type | Meaning |
|-----|------|---------|
| `name` | name | Unique among jobs, and not the name of a service. |
| `schedule` | five-field cron, UTC | When the job runs, for example `"0 2 * * *"`. |
| `command` | string | What to run. |

All three keys are required. A job uses the same image, env, secrets and needs as the
main container.

### `[services.<name>]`

An extra container in the same project. The main container is service `web`.

- Inherited by default: `image` or `[build]`, `env`, `secrets`, `fleet_secrets`, `needs`.
  `inherit = false` starts the service empty.
- The service may set its own: `command`, `port`, `health`, `memory`, `access`,
  `mounts`, `env`, `secrets`, `fleet_secrets`, `needs`, `image` or `build`.
- Routing: `path = "/api"` routes a prefix of the main domain. `domain = "..."` gives
  the service its own domain. Set one of them, or neither. With neither, the service is
  internal and may not set `access`.
- A routed service takes the project's `access.mode` unless it sets its own. Password
  and limits are never inherited.
- `expose = "loopback"` or `"tailnet"` publishes the port on the box. It needs `port`.
- Siblings reach each other at `http://<service>:<port>`. Bay injects `<SERVICE>_URL`.

### `[deploy.<env>]`

One table per environment. Each environment has its own data and its own secret
values. `bay up` deploys `production`; `bay up staging` deploys `staging`.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `box` | name | fleet default box | The box this environment runs on. |
| `domain` | domain | `<name>.<fleet default domain>` | Main domain. |
| `aliases` | list of domains | `[]` | Extra domains that redirect to `domain`. |
| `redirect` | bool | `true` | `false` serves the aliases instead of redirecting them. |
| `branch` | git branch | `main` | Webhook builds follow this branch. |

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
