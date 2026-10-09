```
██████╗  █████╗ ██╗   ██╗
██╔══██╗██╔══██╗╚██╗ ██╔╝
██████╔╝███████║ ╚████╔╝
██╔══██╗██╔══██║  ╚██╔╝
██████╔╝██║  ██║   ██║
╚═════╝ ╚═╝  ╚═╝   ╚═╝
∼∽∼∽∼∽∼∽∼∽∼∽∼∽∼∽∼∽∼∽∼∽∼∽∼
```

[![CI](https://github.com/AltanS/bay/actions/workflows/ci.yml/badge.svg)](https://github.com/AltanS/bay/actions/workflows/ci.yml)

Bay is a CLI tool and an Ansible framework. It provisions hardened Docker servers with a VPN-aware reverse proxy. It deploys your apps to them from declared config files.

Two files are the source of truth. An app defines its identity and deployment target in its `bay.toml`. A fleet repo defines the boxes, secrets, and shared resources in its `bay.fleet.toml`. The daily workflow is `bay plan`, `bay approve`, and `bay up`. Bay compiles the two files into `group_vars/all/services.yml`. Bay generates this file. Never edit it by hand. Run `bay deploy` and `bay provision` for infrastructure tasks: the proxy, the firewall, the webhook receiver, or a new box. File locations are documented in [docs/layout-scenarios.md](docs/layout-scenarios.md).

> **[docs/features.md](docs/features.md)** -- full feature overview and competitive advantages.

## What it does

- **Provisions** a bare Ubuntu server into a hardened Docker host: users, SSH lockdown, nftables firewall, and CrowdSec IDS.
- **Deploys** a container stack with Traefik reverse proxy, automatic SSL, and per-service VPN access control. Bay renders a compose file to describe the stack. The reconciler (`bay_reconcile`) and `rebuild.sh` create the containers using `docker run`, not `docker compose`.
- **Backs up** application data with [restic](https://restic.net/). Backups are deduplicated and encrypted to S3-compatible storage with per-accessory repos and systemd timers. You restore with one command (`bay backup restore <env> <accessory>`).
- **Monitors** container images for updates with [Watchtower](https://github.com/nicholas-fedor/watchtower). It notifies by default, with opt-in auto-updates per service.
- **Alerts** on crashes, build failures, deploy outcomes, disk pressure, and backup failures. It sends alerts to a list of recipients, each Telegram or a webhook (Campfire, Slack, plain text).

`bay.toml` and `bay.fleet.toml` define every app and shared resource on the boxes. `bay up` compiles these files. The deploy then builds the container specs (Traefik labels, container settings, env files, access control) from the output. The rig (Traefik, CrowdSec, Watchtower, the access gateway, the webhook receiver) comes from framework roles and the fleet's `group_vars/`, not from `bay.toml`.

## Documentation

The full reference is in **[docs/](docs/README.md)**. The docs index links every guide, grouped by topic. Highlights:

| Topic | Doc |
|-------|-----|
| What changed between releases | [CHANGELOG.md](CHANGELOG.md) |
| Feature overview & comparison | [docs/features.md](docs/features.md) |
| First project walkthrough | [docs/onboarding.md](docs/onboarding.md) |
| Where every file lives, 15 scenarios | [docs/layout-scenarios.md](docs/layout-scenarios.md) |
| `bay.toml` reference (the app config) | [docs/bay-toml.md](docs/bay-toml.md) |
| plan, approve, up, rollback, adopt, remove | [docs/plan.md](docs/plan.md) |
| Compiled `services.yml` schema (generated, never hand-edited) | [docs/services.md](docs/services.md) |
| Access gateways (none / WireGuard / Headscale) | [docs/access-gateways.md](docs/access-gateways.md) |
| Tailnet HTTPS ingress, ACL & identity | [docs/tailnet-ingress.md](docs/tailnet-ingress.md) |
| Build → deploy pipeline | [docs/build-pipeline.md](docs/build-pipeline.md) · [docs/build-strategies.md](docs/build-strategies.md) |
| Backups (restic) | [docs/backups.md](docs/backups.md) |
| Alerting (Telegram + webhook sinks) | [docs/alerting.md](docs/alerting.md) |
| Multi-region deploys | [docs/multi-region.md](docs/multi-region.md) |
| CrowdSec IDS/IPS | [docs/crowdsec.md](docs/crowdsec.md) |
| Architecture decisions (ADRs) | [docs/adr/](docs/adr/) · [docs/design-decisions.md](docs/design-decisions.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Reporting a vulnerability | [SECURITY.md](SECURITY.md) |
| Community expectations | [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |

See the **[full index →](docs/README.md)** for everything. It covers build observability, the server-side reconciler, the rollout playbook, and ADRs.

## Prerequisites

- [uv](https://docs.astral.sh/uv/): Python package manager. It installs Python, Ansible, and all dependencies automatically.
- A target server running Ubuntu (tested on 22.04/24.04).
- SSH access to the target as `root` (first provision) or as the fleet's `ansible_user` (`bay-admin` in `example/`, every run after). `bay provision` tests the connection as `ansible_user` and falls back to `root` when the host is unreachable as that user. You need no extra flag.

Install uv if you do not have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Quick start

Install Bay once per machine. Make a fleet. Then set up an app repo.

```bash
git clone https://github.com/AltanS/bay ~/.local/share/bay/framework
~/.local/share/bay/framework/bootstrap.sh
bay fleet init prod
# edit ~/.config/bay/fleets/prod/bay.fleet.toml: the box name and default_domain
cd my-app && bay init --fleet prod
```

`bay fleet init` writes a `bay.fleet.toml` with one box, `main` (`env = "production"`), `default_box = "main"` and `default_domain = "example.com"`. It runs `git init`. It creates the first commit (`bay: fleet init`) and adds `.vault_pass` to `.gitignore`. This lets `bay init` accept the fleet at once. Commit your edits to `bay.fleet.toml` before running `bay up`. `bay init` requires an `origin` remote in the app repo.

Here `--fleet prod` after `init` is the fleet name: the directory `~/.config/bay/fleets/prod`. It is not the global `--fleet <path>` option, which precedes the verb. `bay init` writes `fleet = "prod"` into the generated `bay.toml`. Later commands in the repo use that line to find the fleet.

Read **[docs/install.md](docs/install.md)** for installation steps, updates, and fleet selection. Read **[docs/onboarding.md](docs/onboarding.md)** for a step-by-step guide to your first project.

`bay init` writes `bay.toml` in your app repo. Next, `bay plan` shows pending changes. Then `bay up` pins the commit and deploys the whole box environment.

#### Pre-flight check, provision and rig deploy

This is the rig side: a new box, the firewall, and the proxy. App changes go through `bay plan` and `bay up` ([docs/plan.md](docs/plan.md)). The fleet requires `hosts/` and `group_vars/` first ([docs/onboarding.md](docs/onboarding.md#fleet-files)). Run these commands in the app repo, where the `fleet =` line sets the fleet, or pass `--fleet <path>` before the verb.

```bash
# Check the fleet, the CLI, DNS, SSH, the vault password and the boxes
bay doctor

# Validate config files: YAML/schema, inventory, vault keys
bay validate

# Provision server (first time: hardens SSH, installs Docker, firewall)
bay provision production
# On a fresh box with only root, provision falls back to root on its own:
# it tests the SSH connection as ansible_user first and uses root when that fails.

# Deploy services
bay deploy production
```

`bay doctor` checks your **environment**: the active fleet, the installed CLI, DNS resolution, SSH reachability, the password file, box receipts, and app repos. `bay validate` checks your **config**: YAML syntax, the compiled services schema, inventory, and encrypted keys. It also runs automatically before every deploy. Running it here is optional, but it helps you test config changes without waiting for a full deploy.

## Project structure

The framework checkout (the code that `bay` runs):

```
bay/
  bootstrap.sh                 # Installs the bay command on this machine
  version.yml                  # Framework version declaration (bay_version)
  ansible.cfg                  # Ansible settings (inventory, roles path)
  provision.yml                # Server hardening playbook
  deploy.yml                   # Service deployment playbook (two-phase)
  restore.yml                  # Backup restore playbook
  requirements.yml             # External Galaxy role dependencies
  pyproject.toml               # Python deps (Typer, Rich, Ansible)
  src/bay_cli/                 # The CLI (Typer + Rich)
    cli.py                     # Typer app, command registration, entry point
    bay_toml.py                # bay.toml validator
    fleet.py                   # bay.fleet.toml reader
    compiler.py                # bay.toml + bay.fleet.toml -> group_vars/all/services.yml
    plan.py, apply.py          # bay plan, bay up
    lockfile.py                # projects/<name>/bay.lock
    adopt.py, remove.py        # bay adopt, bay remove
    routes.py                  # tailnet routes
    importer.py                # bay import (old YAML fleet -> new layout)
    commands/                  # One module per verb group (doctor, route, fleet, self, vault, ...)
  src/bay_reconcile/           # Container reconciler (ships to the boxes)
  roles/                       # Ansible roles (below)
  docs/                        # Full documentation, see docs/README.md
  example/                     # Example fleet files (reference only)
  vendor/                      # External Galaxy roles and collections (gitignored)
```

A fleet repo lives at `~/.config/bay/fleets/<name>`:

```
my-fleet/
  bay.fleet.toml               # Boxes, domains, shared resources (hand-edited)
  projects/<name>/bay.toml     # An app that has no repo of its own (hand-edited)
  projects/<name>/bay.lock     # CLI-owned pin and deploy record, one per project
  plans/                       # Plan records, committed by bay up
  hosts/                       # Ansible inventory (hand-edited)
  group_vars/all/services.yml  # GENERATED by bay compile, never edit
  group_vars/<env>/secrets.yml # ansible-vault, edited with bay vault edit
  .vault_pass                  # the vault password, out of git
```

An app with its own repo stores its `bay.toml` there. The fleet holds only its lock.

Key roles (see all roles under `roles/`):

```
roles/
  common/                    # System baseline (apt packages, timezone, locale, unattended upgrades)
  swap/                      # Swap file and swappiness
  users/                     # User and SSH key management
  sshd_hardening/            # SSH drop-in hardening (MaxStartups, LoginGraceTime)
  nftables/                  # Firewall rules
  crowdsec/                  # CrowdSec IDS and the nftables bouncer
  traefik/                   # Reverse proxy config, ACME, the `services` Docker network
  deploy_stack/              # Deploy orchestration (lock, env files, config files, databases)
  container_lifecycle/       # Container specs and the reconciler run, deploy receipt
  build_image/               # Registry login and image pulls (images not built on the box)
  git_deploy/                # Clone, build (local, remote, registry), deploy keys, webhook receiver
  backup/                    # Restic backups (pg_dump, mysql, redis, file -> S3)
  watchtower/                # Container image update monitoring and auto-update
  access_gateway/            # VPN backend orchestration (wireguard, headscale or none)
  headscale/                 # Headscale coordination server (self-hosted Tailscale)
  tailscale_node/            # Tailscale daemon, the VPS joins its own tailnet
  alert_channel/             # The one alert sender that every role uses
  docker_monitor/            # Container crash monitor (systemd service)
  cronjobs/                  # Docker prune cron jobs
```

## Playbooks

| Playbook | Purpose | Usage |
|---|---|---|
| `provision.yml` | One-time server hardening: users, SSH, firewall, CrowdSec, Docker | `bay provision production` |
| `deploy.yml` | Repeatable deployment: build/pull images, deploy containers, write rig state | `bay deploy production` |
| `restore.yml` | Restore an accessory from backup | `bay backup restore production postgres` (low-level: `bay restore production -- -e accessory=postgres -e confirm=yes`) |

All playbooks require a target environment as the first argument.

### Deploy modes: rig vs fast

Deploy separates **infrastructure roles** (nftables, access_gateway, traefik, zot, tailnet_identity, watchtower, tailscale_register, backup, crowdsec_allowlist, docker_monitor, cronjobs, boot_safety) from **app roles** (build_image, git_deploy, deploy_stack and the alert and log roles). A rig state file on the server (`{{ stack_dir }}/.rig-state`) tracks when infrastructure was last configured:

- `bay deploy production`: checks rig state. It runs every role, including infrastructure roles, if the framework version changed, if the fleet rig config changed (the last commit touching `group_vars/`, `hosts/` or `files/`), or if the box lacks rig state. Otherwise, it skips the infra roles for a fast app-only deploy.
- `bay deploy --rig production`: forces all roles to run, including infrastructure. Writes an updated rig state on success.
- `bay deploy production --tags deploy_stack`: manual tag override that bypasses rig logic. Runs every role with the tag, including infra roles. Does not write the rig state.

The rig state file contains:
```json
{"rigged_at": "2026-10-07T12:24:07Z", "bay_version": "2.1.13", "consumer_ref": "0281637"}
```

### Deploy privilege separation

The deploy playbook runs in three phases to minimize root usage:

1. **Root bootstrap**: creates the stack directory under `/opt` (requires root), sets ownership to `<app_user>:docker`, and creates the ACME cert file (`root:root 0600`, required by Traefik)
2. **App deploy**: everything else runs as the app account via `become_user`. The fleet sets this account in `app_user` (`bay` in `example/group_vars/all/main.yml`; no role sets a default). The app account belongs to the `docker` group, so it manages containers and images without root. Tasks that write system files (systemd units, files under `/etc`, the firewall) switch to root for that task only (`become_user: root`).
3. **System services**: CrowdSec allowlist, monitoring, cron jobs and boot safety (requires root for systemd/logrotate)

This limits the blast radius if a container is compromised. Container work runs as the app account. The playbook uses root only for directories and system files.

## Apps and shared resources

- **An app** is a `bay.toml`. It defines what the app is (image or build, port, health, needs, secrets, mounts) and where it deploys (`[deploy.<env>]`). Bay sets up Traefik routing, SSL and access control. An app with a repo keeps the file in that repo. An app without a repo lives at `projects/<name>/bay.toml` in the fleet.
- **A shared resource** (a Postgres, a Redis) is a `[resources.*]` table in `bay.fleet.toml`. An app requests it using `needs`.
- **`services.yml` is compiled.** `bay up` (and `bay compile`) generate `group_vars/all/services.yml` from the fleet and pinned `bay.toml` files. It includes a hash header. Manual edits cause the next compile to fail.

See **[docs/bay-toml.md](docs/bay-toml.md)** for the app schema, access modes, env, secrets and mounts. See **[docs/services.md](docs/services.md)** for the compiled output. The sections below showing `services.yml` keys describe this compiled form. You configure the same settings in `bay.toml` or `bay.fleet.toml`.

## Secrets management

Manage secrets with `ansible-vault`. The setup:

1. **`group_vars/production/secrets.yml`** in your fleet holds all secret values under a `secrets:` dict.
2. An app lists secret names in `bay.toml` (`secrets = ["DB_PASSWORD"]`). The compiled `services.yml` carries the names as `env.secret`. Neither file contains values.
3. At deploy time, the `deploy_stack` role resolves secrets and writes per-service `.env` files.
4. The password for the encrypted secrets file lives in `.vault_pass` in the fleet root. Keep it out of git. `bay fleet init <name>` adds it to the new `.gitignore`. A fleet cloned with `bay fleet init --from` keeps its own `.gitignore`, so add the line there. Bay reads exactly that file. See [docs/install.md](docs/install.md#the-vault-password).

Manage secrets with `bay vault` (edit, view, encrypt, decrypt, set). Generate values with `bay secret`. See `bay vault --help` and `bay secret --help` for examples and the secrets key-casing convention.

## Using Bay

Install the Bay command once per machine. It does not live inside your project. Your actual config (boxes, secrets, app definitions) lives in a **fleet**. Bay stores this repo at `~/.config/bay/fleets/<name>`, or at the path you pass to `--fleet`. Bay provides the roles, playbooks, and CLI. See [Quick start](#quick-start) to set up your first fleet.

### Fleet structure

```
~/.config/bay/fleets/prod/
├── bay.fleet.toml           # Fleet settings
├── projects/                # One folder per project: its lock, and bay.toml for an app with no repo
├── plans/                   # Plan records
├── group_vars/              # Real configuration and secrets
└── hosts/                   # Real inventory
```

Bay resolves the active fleet in this order: `--fleet <path>`, `BAY_FLEET`, the `fleet =` line in your current `bay.toml`, `BAY_FLEET_NAME`, and your current fleet directory (for `plan`, `up`, `approve`, `rollback`, `remove`, `doctor`, and `show <name>` only). If none match, the command stops and lists these options. Read [docs/install.md](docs/install.md#pick-a-fleet) for the full rule.

[SKILL.md](SKILL.md) documents the framework for an AI agent in a fleet. It covers critical rules, the full command inventory compiled from the CLI, and a doc map. Run `bay --skill` to print it raw for piping.

### Versioning

Bay releases are git tags. A fresh install tracks the default branch. Use `bay self update` to point the checkout to a tag.

```bash
# Show the installed version and where it lives
bay self version

# Update to the latest release
bay self update

# Move to one given release
bay self update --to v2.1.0

# See the version, the fleet and the feature flags
bay status

# One JSON document: the version, the fleet and the deploy receipt of every box
# (see docs/deploy-receipt.md)
bay status --json
```

**Before updating, read [CHANGELOG.md](CHANGELOG.md)**. It lists changes between releases. The *Upgrade notes* section highlights required manual steps, such as provision runs, renamed variables, or migrations. `bay self update` checks out the latest tag. Read the changelog to see what changes.

#### Runtime compatibility check

A fleet can gate deploys on a minimum framework version. Set `bay_minimum_version` in `group_vars`:

```yaml
# group_vars/all/main.yml
bay_minimum_version: "0.1.0"
```

If the installed framework is older than this value, the playbook aborts with an error before any role runs.

### CLI commands

The CLI help is the complete command reference. It matches the code, and every command includes copy-pasteable examples:

```bash
bay --help              # all commands, grouped by area
bay deploy --help       # per-command flags, quirks, and examples
bay gateway --help      # sub-apps (fleet, self, route, toml, gateway, vault, secret, backup, build, alerts, service, server, region) list their own commands
```

### DNS

Point a wildcard A record to your server:

```
*.example.com  →  <server-ip>
```

An app uses the `domain` key from its `[deploy.<env>]` table. If unset, it defaults to `<name>.<default_domain>` from `bay.fleet.toml` (for example, `status.example.com`). The wildcard record lets you add apps without changing DNS.

### SSL

Traefik uses the Let's Encrypt **HTTP-01 challenge**. It issues certs automatically on the first request to a domain. You do not need a DNS provider API. The optional tailnet ingress is the only exception: it requires a DNS-01 wildcard cert ([docs/tailnet-ingress.md](docs/tailnet-ingress.md)).

Set the ACME contact address in `group_vars/production/domains.yml`:

```yaml
letsencrypt_email: you@example.com
```

## Testing

```bash
make test                # Run all tests
make test-framework      # Framework unit tests only
make test-python         # pytest suite only
make test-bootstrap      # Bootstrap end-to-end test only
make lint                # mypy + ruff, then ansible-lint with production profile
```

See [Framework development](#framework-development) for details on each test suite.

## Backups

Bay uses [restic](https://restic.net/) for deduplicated, encrypted backups to S3-compatible storage. Each backed-up shared resource gets its own restic repository, systemd timer, and retention policy. Backups stay off until the fleet sets `backup_enabled: true` in `group_vars/all/main.yml`.

Configure the backup in `bay.fleet.toml` on the shared resource. The `method` field is required (`pg_dump`, `mysql`, `redis` or `file`):

```toml
[resources.postgres]
kind = "postgres"
image = "postgres:17"
[resources.postgres.backup]
method = "pg_dump"
schedule = "0 3 * * *"    # optional, five-field cron
retain = 7                # optional
```

`bay compile` writes this into `services.yml` as the accessory's `backup:` block. Do not edit this compiled form. Volume backups of an app (`backup` on a `[[mounts]]` volume, and the `[backup]` table of `bay.toml`) deploy since 2.3.0. `backup` defaults to `true` on a volume mount. Every volume mount gets a backup entry unless you set `backup = false` on it. The backup runs when `backup_enabled` is true in the box env: see [docs/bay-toml.md](docs/bay-toml.md#mounts) and [docs/backups.md](docs/backups.md).

Restore one accessory with `bay backup restore production postgres`. It takes a `pre-restore` snapshot first.

See **[docs/backups.md](docs/backups.md)** for full setup instructions, S3 provider reference, retention options, restore procedures, and monitoring.

## Alerting

Bay alerts on container crashes, build failures, deploy outcomes, disk pressure, and backup failures. Every alert has an ID and a severity (`alerts/registry.yml`). Alerts go to a list of **recipients**. Each recipient defines an adapter (`telegram` or `webhook`), its config, and a severity floor (`min_level`). A webhook recipient posts to Campfire, Slack, or any endpoint that accepts an HTTP POST, without edits to the framework's templates.

```yaml
# group_vars/all/alerts.yml
alert_recipients:
  - name: chat
    adapter: webhook
    min_level: info
    config:
      url: "{{ secrets.alert_webhook_url }}"
      format: campfire       # campfire | slack | raw
```

The key in the secrets file must be **lowercase**. It is an Ansible role var. Bay's convention is that UPPERCASE `secrets:` keys are container env vars. An UPPERCASE spelling silently resolves to undefined.

Every recipient is best-effort. A dead alert endpoint never fails a deploy, a backup, or a build. An empty list is the default: alerts are made, but go nowhere. The older two-sink variables (`alert_webhook_url`, `docker_monitor_telegram_*`) still work. Run `bay alerts list` to see every alert and its recipients.

See **[docs/alerting.md](docs/alerting.md)** for format adapters, the full alert list, legacy migration, and troubleshooting.

## Container auto-updates

Bay uses [Watchtower](https://github.com/nicholas-fedor/watchtower) (nickfedor fork, Docker 29+ compatible) to monitor container images for updates. By default, Watchtower runs in **monitor-only mode**. It checks for new images daily at 4 AM UTC and sends Telegram notifications. It may pull a newer image, but it does not recreate or restart containers. The next `bay up` then plans a recreate for that container. Apps opt in to automatic updates with `update = "auto"` in `bay.toml`. A shared resource uses the same key in `bay.fleet.toml`. The compiled `services.yml` carries this setting as `update: auto`. Do not edit the compiled file.

See **[docs/services.md](docs/services.md#container-auto-updates)** for the `update` key reference. Override Watchtower defaults in `group_vars`:

```yaml
watchtower_enabled: true              # Enable/disable entirely (default: true)
watchtower_schedule: "0 0 4 * * *"    # 6-field cron (default: 4 AM daily)
watchtower_monitor_only: true         # Global default mode (default: true)
watchtower_cleanup: true              # Remove old images after update (default: true)
```

## Multi-region deployments

Bay deploys the same stack to multiple regional servers from a single fleet with no framework changes. Define regions as Ansible inventory groups. Override per-region configuration (secrets, VPN peers) in `group_vars/<region>/`. Set each app's box and domain per deploy env in its `bay.toml` (`[deploy.<env>]`). Target individual regions or all regions at once with standard CLI commands.

```bash
bay deploy production -- --limit eu   # Deploy to the EU region only
bay deploy production -- --limit na   # Deploy to the NA region only
bay deploy production                 # Deploy to all regions
```

Running `bay deploy eu` (using the group name as the env) also works. However, the box then writes its receipt as `eu.json` instead of `production.json`. `bay status`, `bay plan` and `bay rollback` read only `production.json`. Keep the box env as `production` and add `-- --limit <group>`.

See **[docs/multi-region.md](docs/multi-region.md)** for the full setup guide: inventory structure, group_vars layering, per-region domains, per-region secrets, and operational workflows.

## Build from source (GitHub deploy)

Services can build from a Git repository instead of pulling from a registry. A `bay.toml` with no `image` builds from source. Set the Dockerfile and related options in `[build]`. The framework clones the repo, builds the Docker image, and tags it with the commit SHA. The build strategy can be `local` (the default: build on the box), `remote` (a build server pushes to a registry), or `registry` (your CI builds and pushes). `push` is a deprecated alias of `remote`. Set the strategy per app in `[build] strategy` or for the fleet in `[defaults] build_strategy` ([docs/build-strategies.md](docs/build-strategies.md)). A webhook receiver listens for GitHub push events and triggers rebuilds automatically. It does not expose the Docker socket.

You write two files. The app `bay.toml` sets what to build and which branch each env follows. The fleet `bay.fleet.toml` configures the webhook:

```toml
# bay.toml in the app repo
[build]
dockerfile = "Dockerfile"
[deploy.production]
branch = "main"

# bay.fleet.toml
[webhook]
domain = "deploy.example.com"
secret = "WEBHOOK_SECRET"   # a secret name; the value is in the encrypted secrets file
```

The repo URL comes from the project lock. `bay init` populates it using `git remote get-url origin`. A project located in the fleet can set `[build] repo` in its `bay.toml` instead. The compiler prefers this setting over the lock. `bay adopt` compares `[build] repo` against `origin`, writes it to the lock, and removes it from the `bay.toml` it moves to the app repo. `bay compile` converts these files into the `build:` and `webhook:` blocks below. Do not edit this compiled output directly. Edit `bay.toml` or `bay.fleet.toml`.

```yaml
# compiled form (group_vars/all/services.yml), do not edit
services:
  myapp:
    access: public
    build:
      repo: git@github.com:user/myapp.git
      branch: main
    domains:
      - myapp.example.com
    ports:
      internal: 3000

webhook:
  domain: deploy.example.com
  secret: "{{ secrets.WEBHOOK_SECRET }}"
```

```bash
bay --fleet <path> deploy production   # no --tags: receiver, build triggers, deploy keys, first clone and build
```

Run that command once per box env running a build app. Run it again after adding a `local` build app. Only this deploy clones a `local` repo and generates its deploy key. `bay up` keeps the rest current. It renders `rebuild.sh`, the receiver list of build containers, the receiver image, and the build triggers. It also stops the triggers of removed containers ([docs/plan.md](docs/plan.md#bay-up)). `bay up` does not build the initial image of a new build app ([docs/plan.md](docs/plan.md#the-first-image)). On the box, it creates one SSH deploy key per build container whose repo lacks a token: `/opt/<stack>/builds/<container>/.deploy_key.pub`. Bay does not register this key. Add it to the GitHub repo manually under Settings > Deploy keys with read-only access. For SSH repo URLs, the first run stops at the clone step until you add the key to GitHub. Add it and rerun the deploy. Next, configure the webhook in the repo settings. Set the payload URL to `https://<webhook domain>/webhook/<container name>`, supply the `[webhook] secret` value, select `application/json` as the content type, and choose push events only.

See **[docs/services.md](docs/services.md#build-from-source)** for the complete `build:` schema, image tagging rules, and webhook configuration.

Auto-builds include a circuit breaker, health checks with rollback, build timeouts, and notification dedup. The circuit breaker trips after 5 consecutive failures by default, configured via `git_deploy_cb_max_failures`. See **[docs/build-strategies.md](docs/build-strategies.md#circuit-breaker)** for details. Reset it with `bay --fleet <path> build reset <service>`.

## Architecture notes

### Access gateways

Bay supports two VPN gateway backends for apps with `mode = "tailnet"` in `bay.toml` (compiled form: `access: vpn`): **WireGuard** (manual peer configuration, static IPs) and **Headscale** (self-hosted Tailscale coordination server with automatic tunnel management and OIDC self-service enrollment). Set `access_gateway` in `group_vars/all/access_gateway.yml` to select a backend: `wireguard`, `headscale` or `none`. `none` means no VPN at all; a deploy with a `tailnet` app then fails. If the fleet does not set it, the deploy uses `wireguard`, the default in `roles/access_gateway/defaults/main.yml`. Both gateways feed into the same downstream pipeline (nftables, CrowdSec, Traefik IPAllowList). Service definitions work identically with either option.

VPN services are accessible via their public domain when the client is on the tailnet. Headscale's MagicDNS split-DNS automatically resolves VPN service domains to the server's tailnet IP for enrolled clients. Requests travel through the tunnel and pass the IPAllowList. You do not need `/etc/hosts` edits or tailnet IP bookmarks. Non-tailnet clients receive a 403.

See **[docs/access-gateways.md](docs/access-gateways.md)** for traffic flow diagrams, split-DNS details, configuration examples, and a detailed comparison.

### Headscale quick start

1. **Configure**: set `access_gateway: headscale` and `headscale_domain` in `group_vars/all/access_gateway.yml`
2. **DNS**: point `hs.example.com` (A record) to your server IP
3. **Provision + deploy**: `bay provision production && bay deploy production`
4. **Create user + pre-auth key**: `bay gateway add-user alice && bay gateway key alice`
5. **Enroll device**: install the Tailscale app, run `tailscale up --login-server https://hs.example.com --authkey <key>`
6. **Verify**: `bay gateway nodes`, the device should appear in the node list

See **[docs/access-gateways.md](docs/access-gateways.md#headscale-quick-start)** for the detailed walkthrough.

### Traefik with host networking

Traefik runs with `network_mode: host` to see real client IPs without Docker NAT. App containers live on one named bridge network per box: `services` (`traefik_docker_network`, created by the `traefik` role). Every env on the box shares it. A container of an env other than `primary_env` includes the env in its name (`shop-staging`). Always use the `<NAME>_URL` that Bay injects, never a container name. A name without the env reaches the `primary_env` container. Traefik discovers containers via the Docker socket API and routes to their bridge IPs.

### CrowdSec integration

CrowdSec reads Traefik access logs and SSH auth logs. It shares decisions with the nftables bouncer, which adds offending IPs to an nftables blocklist set. VPN IPs are whitelisted in both CrowdSec and Traefik.

### Deploy lock

The `deploy_stack` role acquires a file-based deploy lock before deploying. This prevents concurrent deploys. Bay automatically ignores stale locks older than 1 hour.

## Renaming `stack_name`

The `stack_name` variable (set in `group_vars/all/main.yml`) controls more than the project label. Changing it affects several parts of the deployment:

- **Volume name prefixes**: all persistent named volumes use the format `{stack_name}_*`.
- **Container names do not change**. A container uses the project name (`shop`, `shop-worker`), not the stack name. The `services` Docker network also keeps its name.
- **Local image tag**: the local tag of an image built on the box includes the stack name. A rename creates a new tag.
- **Stack directory**: `/opt/{stack_name}/` on the server holds Compose files, env files, and configs when `stack_dir` derives from `stack_name`, as shown in the example fleet.
- **Headscale user**: if you use the headscale gateway, the user that owns the server nodes defaults to `stack_name` (`headscale_server_user`).
- **MagicDNS domain**: `headscale_magic_dns_domain` defaults to `<stack_name>.tailnet.internal`.
- **Node hostnames**: tailnet nodes register under the stack namespace.
- **Config/env file paths**: all paths under `/opt/{stack_name}/`.

### Volume data loss risk

Deploying with a new `stack_name` creates empty volumes (`newname_*`). Existing data stays in the old volumes (`oldname_*`). Databases, Vaultwarden stores, and monitoring history seem lost until you manually migrate the volumes.

### Safe migration procedure

1. **Stop and remove the old containers** (keep volumes). The old and new containers share names. Locate them by the volumes they mount:
   ```bash
   for v in $(docker volume ls -q --filter name=^oldname_); do
     docker ps -aq --filter volume="$v"
   done | sort -u | xargs -r docker rm -f
   ```
2. **Deploy the new stack** so Bay creates the new containers and volumes:
   ```bash
   bay deploy production
   ```
3. **Stop the new containers** that mount the new volumes:
   ```bash
   for v in $(docker volume ls -q --filter name=^newname_); do
     docker ps -q --filter volume="$v"
   done | sort -u | xargs -r docker stop
   ```
4. **Copy each volume** from old to new:
   ```bash
   docker run --rm \
     -v "oldname_pgdata:/src:ro" \
     -v "newname_pgdata:/dst" \
     alpine sh -c "rm -rf /dst/* && cp -a /src/. /dst/"
   ```
   Repeat this step for every volume (`docker volume ls | grep oldname_`).
5. **Start new containers**:
   ```bash
   bay deploy production
   ```
6. **Verify** that services are healthy and data is intact. Then clean up:
   ```bash
   docker volume rm $(docker volume ls -q --filter name=oldname_)
   rm -rf /opt/oldname
   ```

### Headscale namespace

If you use `access_gateway: headscale`, changing `stack_name` also changes the default Headscale user that owns the server nodes. A fleet that sets an explicit `headscale_server_user` keeps that user. After renaming, run `bay gateway migrate-namespace` to rename the Headscale user and update node hostnames in the tailnet. Without this step, enrolled devices lose access to VPN-protected services.

For the default migration from the legacy `server` user (pre-v0.40.0):

```bash
bay gateway migrate-namespace --dry-run    # preview
bay gateway migrate-namespace              # server -> stack_name
```

For custom renames (for example, after changing `stack_name` from `oldapp` to `newapp`):

```bash
bay gateway migrate-namespace --from oldapp --to newapp --dry-run
bay gateway migrate-namespace --from oldapp --to newapp
```

The command renames the Headscale user and all node hostnames assigned to it. Node hostnames follow the `{user}-{region}` convention. You can safely run this command multiple times. It skips resources that are already migrated.

## Framework development

### Test suites

Run `make install` first in a fresh clone. It installs Galaxy roles and
collections into `vendor/`. It also sets `core.hooksPath` to `.githooks/`
so pre-commit and pre-push gates run. `core.hooksPath` is per-clone config.
Every checkout needs it once. Without `make install`, the suite runs against
an incomplete `vendor/` tree. The framework test writes mock Galaxy role stubs
to fill the gap, so it can pass against stubs instead of real dependencies.

```bash
make install                 # Galaxy deps into vendor/ + core.hooksPath
make test                    # Framework + bootstrap + Python suites
```

| Command | Script | What it tests |
|---------|--------|---------------|
| `make test-framework` | `tests/test_framework.sh` | Playbook syntax, ansible-lint, role structure, YAML validity, Jinja2 templates, Galaxy dependencies, expected files, required variables |
| `make test-bootstrap` | `tests/test_bootstrap.sh` | End-to-end install: runs `bootstrap.sh` from the local repo into throwaway directories, checks `bay` runs and `bay fleet init` and `bay fleet ls` work, then runs the installer a second time |
| `make test-python` | `tests/*.py` | The pytest suite (CLI, compiler, reconciler, docs) |
| `make test` | All three | Runs framework + bootstrap + Python tests |
| `make lint` | (none) | `mypy` and `ruff` (`make typecheck`), then `ansible-lint` with production profile (see `.ansible-lint`) |

The bootstrap test sets `BAY_REPO=<local path>` to clone from the working tree. It needs no GitHub access.

### Workflow

```bash
# Edit roles, templates, playbooks...
make test                    # Verify nothing is broken
make lint                    # Check style
git commit                   # Commit framework changes
make release VERSION=0.5.1   # Maintainers only: bump version.yml, tag, push

# On a machine that uses Bay:
bay self update      # Move to the latest release
bay test             # Check your fleet still works
```

Never tag or push a release by hand. `make release` bumps `version.yml`, commits, tags, and pushes in one step. A manual tag leaves `version.yml` behind. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, required checks, and the release process.

## License

Bay is released under the [MIT License](LICENSE).
