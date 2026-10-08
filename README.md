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

Bay is a command-line tool and an Ansible framework. It provisions hardened Docker servers with a VPN-aware reverse proxy and deploys your apps to them from a declared config.

Two files are the source of truth. An app says what it is and where it deploys in its `bay.toml`. A fleet (the repo that holds your boxes, secrets and shared resources) says what the boxes are in its `bay.fleet.toml`. The daily flow is `bay plan`, `bay approve` and `bay up`. Bay compiles the two files into `group_vars/all/services.yml`. That file is generated and never hand-edited. `bay deploy` and `bay provision` are for rig work (the proxy, the firewall, the webhook receiver, a new box). Where each file lives: [docs/layout-scenarios.md](docs/layout-scenarios.md).

> **[docs/features.md](docs/features.md)** -- full feature overview and competitive advantages.

## What it does

- **Provisions** a bare Ubuntu server into a hardened Docker host (users, SSH lockdown, nftables firewall, CrowdSec IDS)
- **Deploys** a container stack with Traefik reverse proxy, automatic SSL, and per-service VPN access control. Bay renders a compose file as a description of the stack. The reconciler (`bay_reconcile`) and `rebuild.sh` create the containers with `docker run`, not with `docker compose`.
- **Backs up** application data with [restic](https://restic.net/): deduplicated, encrypted backups to S3-compatible storage with per-accessory repos, systemd timers, and a one-command restore (`bay backup restore <env> <accessory>`)
- **Monitors** container images for updates with [Watchtower](https://github.com/nicholas-fedor/watchtower) — notify-by-default with opt-in auto-update per service
- **Alerts** on crashes, build failures, deploy outcomes, disk pressure and backup failures, to a list of recipients, each Telegram or a webhook (Campfire, Slack, plain text)

Every app and shared resource on the boxes comes from `bay.toml` and `bay.fleet.toml`. `bay up` compiles them, and the deploy builds the container specs (Traefik labels, container settings, env files, access control) from the result. The rig (Traefik, CrowdSec, Watchtower, the access gateway, the webhook receiver) comes from the framework roles and the fleet's `group_vars/`, not from `bay.toml`.

## Documentation

Full reference lives in **[docs/](docs/README.md)** — the docs index links every guide, grouped by topic. Highlights:

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

See the **[full index →](docs/README.md)** for everything, including build observability, the server-side reconciler, the rollout playbook, and ADRs.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package manager (installs Python, Ansible, and all dependencies automatically)
- A target server running Ubuntu (tested on 22.04/24.04)
- SSH access to the target as `root` (first provision) or as the fleet's `ansible_user` (`bay-admin` in `example/`, every run after). `bay provision` tests the connection as `ansible_user` and falls back to `root` when the host is unreachable as that user, so you need no extra flag

Install uv if you don't have it:

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

`bay fleet init` writes a `bay.fleet.toml` with one box, `main` (`env = "production"`), `default_box = "main"` and `default_domain = "example.com"`, and runs `git init`. It makes the first commit (`bay: fleet init`) and puts `.vault_pass` in the `.gitignore`, so `bay init` accepts the fleet at once. Commit your edit of `bay.fleet.toml` in the fleet before `bay up`. `bay init` needs an `origin` remote in the app repo.

Here `--fleet prod` after `init` is the fleet name, the folder `~/.config/bay/fleets/prod`. It is not the global `--fleet <path>` option, which goes before the verb. `bay init` writes `fleet = "prod"` into the `bay.toml` it drafts, and later commands in this repo find the fleet from that line.

Read **[docs/install.md](docs/install.md)** for the install steps, updates and how Bay picks a fleet. Read **[docs/onboarding.md](docs/onboarding.md)** for the first project, step by step.

`bay init` writes a `bay.toml` in your app repo. Then `bay plan` shows what will change, and `bay up` pins the commit and deploys the whole box environment.

#### Pre-flight check, provision and rig deploy

This is the rig side: a new box, the firewall, the proxy. App changes go through `bay plan` and `bay up` ([docs/plan.md](docs/plan.md)). The fleet needs `hosts/` and `group_vars/` first ([docs/onboarding.md](docs/onboarding.md#fleet-files)). Run these commands in the app repo, where the `fleet =` line picks the fleet, or pass `--fleet <path>` before the verb.

```bash
# Check the fleet, the CLI, DNS, SSH, the vault password and the boxes
bay doctor

# Validate config files — YAML/schema, inventory, vault keys
bay validate

# Provision server (first time — hardens SSH, installs Docker, firewall)
bay provision production
# On a fresh box with only root, provision falls back to root on its own:
# it tests the SSH connection as ansible_user first and uses root when that fails.

# Deploy services
bay deploy production
```

`bay doctor` checks your **environment** (the fleet it picked, the installed CLI, DNS resolution, SSH reachability,
vault password present, each box's receipt, the app repos). `bay validate` checks your **config** (YAML
syntax, the compiled services schema, inventory, vault keys) and also runs
automatically before every deploy, so running it here is optional — useful
for iterating on config without waiting for a full deploy.

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

A fleet (the repo you own, at `~/.config/bay/fleets/<name>`):

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

An app that has its own repo keeps its `bay.toml` there. The fleet holds only its lock.

The main roles (the full set is under `roles/`):

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

- `bay deploy production`: it checks rig state. If the framework version or the fleet's rig config (the last commit that touched `group_vars/`, `hosts/` or `files/`) changed since the last rig, or the box has no rig state yet, it runs every role, the infrastructure roles included. Otherwise it skips the infra roles for a fast app-only deploy.
- `bay deploy --rig production`: it forces all roles to run, including infrastructure. Writes updated rig state on success.
- `bay deploy production --tags deploy_stack`: manual tag override, bypasses rig logic. Every role that carries the tag runs, infra roles included, and the rig state is not written.

The rig state file contains:
```json
{"rigged_at": "2026-10-07T12:24:07Z", "bay_version": "2.1.13", "consumer_ref": "0281637"}
```

### Deploy privilege separation

The deploy playbook runs in three phases to minimize root usage:

1. **Root bootstrap**: creates the stack directory under `/opt` (requires root), sets ownership to `<app_user>:docker`, and creates the ACME cert file (`root:root 0600`, required by Traefik)
2. **App deploy**: everything else runs as the app account via `become_user`. The fleet names that account in `app_user` (`bay` in `example/group_vars/all/main.yml`; no role sets a default). The app account has `docker` group membership, so it can manage containers and images without root. A task that writes a system file (a systemd unit, a file under `/etc`, the firewall) switches to root for that task only (`become_user: root`).
3. **System services**: the CrowdSec allowlist, monitoring, cron jobs and boot safety (requires root for systemd/logrotate)

This reduces blast radius if a container is compromised: the container work runs as the app account, and root is used only for directories and system files.

## Apps and shared resources

- **An app** is a `bay.toml`: what it is (image or build, port, health, needs, secrets, mounts) and where it deploys (`[deploy.<env>]`). The app gets Traefik routing, SSL and access control. An app with a repo keeps the file in that repo. An app with no repo is `projects/<name>/bay.toml` in the fleet.
- **A shared resource** (a Postgres, a Redis) is a `[resources.*]` table in `bay.fleet.toml`. An app asks for it with `needs`.
- **`services.yml` is compiled.** `bay up` (and `bay compile`) write `group_vars/all/services.yml` from the fleet and the pinned `bay.toml` files. It has a hash header. A hand edit makes the next compile refuse.

See **[docs/bay-toml.md](docs/bay-toml.md)** for the app schema, access modes, env, secrets and mounts, and **[docs/services.md](docs/services.md)** for the compiled form. The sections below that show `services.yml` keys describe that compiled form: the same setting comes from `bay.toml` or `bay.fleet.toml`.

## Secrets management

Secrets are managed with `ansible-vault`. The setup:

1. **`group_vars/production/secrets.yml`** (in your fleet) holds all secret values under a `secrets:` dict
2. An app lists secret names in `bay.toml` (`secrets = ["DB_PASSWORD"]`). The compiled `services.yml` carries the names as `env.secret`. Values are never in either file.
3. At deploy time, the `deploy_stack` role resolves secrets and writes per-service `.env` files
4. The password of the encrypted secrets file lives in `.vault_pass` in the fleet root. Keep it out of git. `bay fleet init <name>` adds it to the new `.gitignore`. A fleet cloned with `bay fleet init --from` keeps its own `.gitignore`, so add the line there. Bay reads exactly that file. See [docs/install.md](docs/install.md#the-vault-password).

Manage secrets with `bay vault` (edit, view, encrypt, decrypt, set) and generate values with `bay secret` — see `bay vault --help` and `bay secret --help` for examples and the secrets key-casing convention.

## Using Bay

Bay is a command you install once per machine. It does not live inside your project. Your real config (boxes, secrets, app definitions) lives in a **fleet**, a repo that Bay keeps at `~/.config/bay/fleets/<name>` (or any path you pass with `--fleet`). Bay provides the roles, playbooks and the CLI. See [Quick start](#quick-start) to set up your first fleet.

### Fleet structure

```
~/.config/bay/fleets/prod/
├── bay.fleet.toml           # Fleet settings
├── projects/                # One folder per project: its lock, and bay.toml for an app with no repo
├── plans/                   # Plan records
├── group_vars/              # Real configuration and secrets
└── hosts/                   # Real inventory
```

Bay picks the fleet in one fixed order: `--fleet <path>`, `BAY_FLEET`, the `fleet =` line of the `bay.toml` you stand in, `BAY_FLEET_NAME`, and, for `plan`, `up`, `approve`, `rollback`, `remove`, `doctor` and `show <name>` only, the fleet directory you stand in. With none of these, the command stops and lists them. The full rule is in [docs/install.md](docs/install.md#pick-a-fleet).

[SKILL.md](SKILL.md) is the framework's orientation document for an AI agent working in a fleet: the rules that bite, the whole command inventory (compiled from the CLI itself), and the doc map. `bay --skill` prints it raw for piping anywhere else.

### Versioning

Bay releases are git tags. A fresh install is the default branch. `bay self update` moves the checkout to a tag.

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

**Before updating, read [CHANGELOG.md](CHANGELOG.md)** — it lists what changed between releases, with an *Upgrade notes* section for anything needing manual action (a provision run, a renamed variable, a migration). `bay self update` moves you to the latest tag; the changelog is how you find out what that brings.

#### Runtime compatibility check

A fleet can optionally set `bay_minimum_version` in its `group_vars` to gate deploys on a minimum framework version:

```yaml
# group_vars/all/main.yml
bay_minimum_version: "0.1.0"
```

If the framework version is older than the minimum, the playbook aborts with a clear error message before any roles execute.

### CLI commands

The CLI help is the command reference — it is kept accurate against the code and every command carries copy-pasteable examples:

```bash
bay --help              # all commands, grouped by area
bay deploy --help       # per-command flags, quirks, and examples
bay gateway --help      # sub-apps (fleet, self, route, toml, gateway, vault, secret, backup, build, alerts, service, server, region) list their own commands
```

### DNS

Set a wildcard A record for your domain:

```
*.example.com  →  <server-ip>
```

An app's domain is `domain` in its `[deploy.<env>]` table, else `<name>.<default_domain>` from `bay.fleet.toml` (e.g., `status.example.com`). With the wildcard record, no DNS change is needed when you add an app.

### SSL

Traefik uses Let's Encrypt **HTTP-01 challenge**: certs are issued automatically per domain on first request. No DNS provider API is needed. The optional tailnet ingress is the one exception: it uses a DNS-01 wildcard cert ([docs/tailnet-ingress.md](docs/tailnet-ingress.md)).

Set the ACME email in your fleet's `group_vars/production/domains.yml`:

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

Bay uses [restic](https://restic.net/) for deduplicated, encrypted backups to S3-compatible storage. Each backed-up shared resource gets its own restic repository, systemd timer, and retention policy. Backups are off until the fleet sets `backup_enabled: true` in `group_vars/all/main.yml`.

Write the backup in `bay.fleet.toml`, on the shared resource. `method` is required (`pg_dump`, `mysql`, `redis` or `file`):

```toml
[resources.postgres]
kind = "postgres"
image = "postgres:17"
[resources.postgres.backup]
method = "pg_dump"
schedule = "0 3 * * *"    # optional, five-field cron
retain = 7                # optional
```

`bay compile` writes it into `services.yml` as the accessory's `backup:` block (compiled form, do not edit). Volume backups of an app (`backup` on a `[[mounts]]` volume, and the `[backup]` table of `bay.toml`) deploy since 2.3.0. `backup` defaults to `true` on a volume mount, so every volume mount gets a backup entry unless you write `backup = false` on it. The backup runs when `backup_enabled` is true in the box env: see [docs/bay-toml.md](docs/bay-toml.md#mounts) and [docs/backups.md](docs/backups.md).

Restore one accessory with `bay backup restore production postgres`. It takes a `pre-restore` snapshot first.

See **[docs/backups.md](docs/backups.md)** for full setup instructions, S3 provider reference, retention options, restore procedures, and monitoring.

## Alerting

Bay alerts on container crashes, build failures, deploy outcomes, disk pressure, and backup failures. Every alert has an ID and a severity (`alerts/registry.yml`). Alerts go to a list of **recipients**. Each recipient is an adapter (`telegram` or `webhook`), its config and a severity floor (`min_level`). A webhook recipient can post to Campfire, Slack, or anything that accepts an HTTP POST, without patching the framework's templates.

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

The vault key must be **lowercase** — it is an Ansible role var, and Bay's convention is that UPPERCASE `secrets:` keys are container env vars. An UPPERCASE spelling silently resolves undefined.

Every recipient is best-effort: a dead alert endpoint can never fail a deploy, a backup, or a build. An empty list (the default) means alerts are made but sent nowhere. The older two-sink variables (`alert_webhook_url`, `docker_monitor_telegram_*`) still work. `bay alerts list` shows every alert and who gets it.

See **[docs/alerting.md](docs/alerting.md)** for the format adapters, the full list of what gets sent, the legacy migration, and troubleshooting.

## Container auto-updates

Bay uses [Watchtower](https://github.com/nicholas-fedor/watchtower) (nickfedor fork, Docker 29+ compatible) to monitor container images for updates. By default, Watchtower runs in **monitor-only mode**: it checks for new images daily at 4 AM UTC and sends Telegram notifications, but does not pull or restart anything. Apps opt in to automatic updates with `update = "auto"` in `bay.toml` (a shared resource takes the same key in `bay.fleet.toml`). The compiled `services.yml` carries it as `update: auto` (compiled form, do not edit).

See **[docs/services.md](docs/services.md#container-auto-updates)** for the `update` key reference. Override watchtower defaults in `group_vars`:

```yaml
watchtower_enabled: true              # Enable/disable entirely (default: true)
watchtower_schedule: "0 0 4 * * *"    # 6-field cron (default: 4 AM daily)
watchtower_monitor_only: true         # Global default mode (default: true)
watchtower_cleanup: true              # Remove old images after update (default: true)
```

## Multi-region deployments

Bay supports deploying the same stack to multiple regional servers from a single fleet with zero framework changes. Define regions as Ansible inventory groups, override per-region configuration (secrets, VPN peers) via `group_vars/<region>/`, set each app's box and domain per deploy env in its `bay.toml` (`[deploy.<env>]`), and target individual regions or all at once with the standard CLI commands.

```bash
bay deploy production -- --limit eu   # Deploy to the EU region only
bay deploy production -- --limit na   # Deploy to the NA region only
bay deploy production                 # Deploy to all regions
```

`bay deploy eu` (the group name as the env) also runs, but the box then writes its receipt as `eu.json`, not `production.json`, and `bay status`, `bay plan` and `bay rollback` read only `production.json`. Keep the box env and add `-- --limit <group>`.

See **[docs/multi-region.md](docs/multi-region.md)** for the full setup guide — inventory structure, group_vars layering, per-region domains, per-region secrets, and operational workflows.

## Build from source (GitHub deploy)

Services can be built from a Git repository instead of pulling from a registry. A `bay.toml` with no `image` builds from source (`[build]` sets the Dockerfile and the rest), and the framework clones the repo, builds the Docker image, and tags it with the commit SHA. The build strategy is `local` (the default, build on the box), `remote` (a build server pushes to a registry) or `registry` (your CI builds and pushes). `push` is a deprecated alias of `remote`. Set it per app in `[build] strategy` or for the fleet in `[defaults] build_strategy` ([docs/build-strategies.md](docs/build-strategies.md)). A webhook receiver listens for GitHub push events and triggers automatic rebuilds, without exposing the Docker socket.

You write two things. The app's `bay.toml` says what to build and which branch each env follows. The fleet's `bay.fleet.toml` names the webhook:

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

The repo URL comes from the project's lock (`bay init` writes it from `git remote get-url origin`). A project that lives in the fleet may set `[build] repo` in its `bay.toml` instead, and the compile prefers that key over the lock. `bay adopt` checks `[build] repo` against `origin`, writes it into the lock, and leaves it out of the `bay.toml` it moves to the app repo. `bay compile` turns all of this into the `build:` and `webhook:` blocks below. That is the compiled form: do not edit it, write `bay.toml` or `bay.fleet.toml` instead.

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

Run that once per box env that runs a build app, and again after you add a `local` build app: only this deploy clones a `local` repo and makes its deploy key. `bay up` keeps the rest current. It renders `rebuild.sh`, the receiver's list of build containers, the receiver image and the build triggers, and it stops the trigger of a removed container ([docs/plan.md](docs/plan.md#bay-up)). `bay up` does not build the first image of a new build app ([docs/plan.md](docs/plan.md#the-first-image)). On the box it makes one SSH deploy key per build container whose repo has no token: `/opt/<stack>/builds/<container>/.deploy_key.pub`. Bay does not register the key. Add it to the GitHub repo yourself (Settings > Deploy keys, read-only). With an SSH repo URL, the first run stops at the clone until the key is on GitHub: add it and run the deploy again. Then add the hook in the repo settings: payload URL `https://<webhook domain>/webhook/<container name>`, the value of the `[webhook] secret`, content type `application/json`, push events only.

See **[docs/services.md](docs/services.md#build-from-source)** for the full `build:` schema, image tagging, and webhook configuration.

Auto-builds include a circuit breaker (stops after 5 consecutive failures by default, set by `git_deploy_cb_max_failures`), health checks with rollback, build timeouts, and notification dedup. See **[docs/build-strategies.md](docs/build-strategies.md#circuit-breaker)** for details. Reset with `bay --fleet <path> build reset <service>`.

## Architecture notes

### Access gateways

Bay supports two VPN gateway backends for apps with `mode = "tailnet"` in `bay.toml` (compiled form: `access: vpn`): **WireGuard** (manual peer configuration, static IPs) and **Headscale** (self-hosted Tailscale coordination server with automatic tunnel management and OIDC self-service enrollment). Set `access_gateway` in `group_vars/all/access_gateway.yml` to select a backend: `wireguard`, `headscale` or `none` (no VPN at all; a deploy with a `tailnet` app then fails). If the fleet does not set it, the deploy uses `wireguard`, the default in `roles/access_gateway/defaults/main.yml`. Both gateways feed into the same downstream pipeline (nftables, CrowdSec, Traefik IPAllowList), so service definitions work identically with either option.

VPN services are seamlessly accessible via their public domain when the client is on the tailnet. Headscale's MagicDNS split-DNS automatically resolves VPN service domains to the server's tailnet IP for enrolled clients, so requests travel through the tunnel and pass the IPAllowList — no `/etc/hosts` hacks or tailnet IP bookmarks needed. Non-tailnet clients still get blocked with 403.

See **[docs/access-gateways.md](docs/access-gateways.md)** for traffic flow diagrams, split-DNS details, configuration examples, and a detailed comparison.

### Headscale quick start

1. **Configure** — set `access_gateway: headscale` and `headscale_domain` in `group_vars/all/access_gateway.yml`
2. **DNS** — point `hs.example.com` (A record) to your server IP
3. **Provision + deploy** — `bay provision production && bay deploy production`
4. **Create user + pre-auth key** — `bay gateway add-user alice && bay gateway key alice`
5. **Enroll device** — install the Tailscale app, run `tailscale up --login-server https://hs.example.com --authkey <key>`
6. **Verify** — `bay gateway nodes` — the device should appear in the node list

See **[docs/access-gateways.md](docs/access-gateways.md#headscale-quick-start)** for the detailed walkthrough.

### Traefik with host networking

Traefik runs with `network_mode: host` to see real client IPs (no Docker NAT). App containers live on one named bridge network per box, `services` (`traefik_docker_network`, created by the `traefik` role). Every env on the box shares it. A container of an env other than `primary_env` carries the env in its name (`shop-staging`). So use the `<NAME>_URL` that Bay injects, never a container name: a name without the env reaches the `primary_env` container. Traefik discovers the containers via the Docker socket API and routes to their bridge IPs.

### CrowdSec integration

CrowdSec reads Traefik access logs and SSH auth logs. It shares decisions with the nftables bouncer, which adds offending IPs to an nftables blocklist set. VPN IPs are whitelisted in both CrowdSec and Traefik.

### Deploy lock

The `deploy_stack` role acquires a file-based deploy lock before deploying, preventing concurrent deploys. Stale locks (older than 1 hour) are automatically ignored.

## Renaming `stack_name`

The `stack_name` variable (set in `group_vars/all/main.yml`) controls more than the project label. Changing it has cascading effects across the deployment:

- **Volume name prefixes**, all persistent named volumes are named `{stack_name}_*`
- **Container names do not change**. A container carries the project name (`shop`, `shop-worker`), not the stack name. The `services` Docker network keeps its name too.
- **Local image tag**. The local tag of an image built on the box contains the stack name, so a rename also makes a new tag
- **Stack directory**, `/opt/{stack_name}/` on the server (Compose file, env files, configs), when `stack_dir` is derived from `stack_name` as in the example fleet
- **Headscale user**. If you use the headscale gateway, the user that owns the server nodes defaults to `stack_name` (`headscale_server_user`)
- **MagicDNS domain**. `headscale_magic_dns_domain` defaults to `<stack_name>.tailnet.internal`
- **Node hostnames** — tailnet nodes are registered under the stack namespace
- **Config/env file paths** — everything under `/opt/{stack_name}/`

### Volume data loss risk

Deploying with a new `stack_name` creates a fresh set of empty volumes (`newname_*`) while all existing data remains in the old volumes (`oldname_*`). Databases, vaultwarden vaults, monitoring history — everything appears lost until volumes are manually migrated.

### Safe migration procedure

1. **Stop and remove the old containers** (keep volumes). The old and the new containers share their names, so find them by the volumes they mount:
   ```bash
   for v in $(docker volume ls -q --filter name=^oldname_); do
     docker ps -aq --filter volume="$v"
   done | sort -u | xargs -r docker rm -f
   ```
2. **Deploy the new stack** so the new containers and volumes are created:
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
   Repeat for every volume (`docker volume ls | grep oldname_`).
5. **Start new containers**:
   ```bash
   bay deploy production
   ```
6. **Verify** services are healthy and data is intact, then clean up:
   ```bash
   docker volume rm $(docker volume ls -q --filter name=oldname_)
   rm -rf /opt/oldname
   ```

### Headscale namespace

If using `access_gateway: headscale`, changing `stack_name` also changes the default Headscale user that owns the server nodes. A fleet that sets `headscale_server_user` keeps that user. After renaming, run `bay gateway migrate-namespace` to rename the Headscale user and update node hostnames in the tailnet. Without this, enrolled devices lose connectivity to VPN-protected services.

For the default migration from the legacy `server` user (pre-v0.40.0):

```bash
bay gateway migrate-namespace --dry-run    # preview
bay gateway migrate-namespace              # server -> stack_name
```

For custom renames (e.g. after changing `stack_name` from `oldapp` to `newapp`):

```bash
bay gateway migrate-namespace --from oldapp --to newapp --dry-run
bay gateway migrate-namespace --from oldapp --to newapp
```

The command renames the Headscale user and all node hostnames under it. Node hostnames follow the `{user}-{region}` convention. Safe to run multiple times — already-migrated resources are skipped.

## Framework development

### Test suites

Run `make install` first in a fresh clone. It installs the Galaxy roles and
collections this framework depends on into `vendor/`, and it points git at
`.githooks/` (`core.hooksPath`) so the pre-commit and pre-push gates run.
`core.hooksPath` is per-clone config, so every checkout needs it once. Without
`make install` the suite runs against an incomplete `vendor/` tree — the
framework test writes mock Galaxy role stubs to fill the gap, so it can go
green against stubs instead of the real dependencies.

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

The bootstrap test uses `BAY_REPO=<local path>` so it clones from the working tree — no GitHub access needed.

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

Never tag or push a release by hand — `make release` bumps `version.yml`, commits, tags and pushes in one step, and a hand-written tag leaves `version.yml` behind. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, the checks a change must pass, and the release process.

## License

Bay is released under the [MIT License](LICENSE).
