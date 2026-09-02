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

Ansible framework for provisioning hardened Docker servers with VPN-aware reverse proxy and declarative service deployment.

> **[docs/features.md](docs/features.md)** -- full feature overview and competitive advantages.

## What it does

- **Provisions** a bare Ubuntu server into a hardened Docker host (users, SSH lockdown, nftables firewall, CrowdSec IDS)
- **Deploys** a Docker Compose stack with Traefik reverse proxy, automatic SSL, and per-service VPN access control
- **Backs up** application data with [restic](https://restic.net/) — deduplicated, encrypted backups to S3-compatible storage with per-accessory repos, systemd timers, and one-command restore
- **Monitors** container images for updates with [Watchtower](https://github.com/nicholas-fedor/watchtower) — notify-by-default with opt-in auto-update per service
- **Alerts** on crashes, build failures, deploy outcomes, disk pressure and backup failures — to Telegram and/or any webhook sink (Campfire, Slack, plain text)

Everything is driven by a single `services.yml` file — define your services and accessories there, and the roles generate Traefik labels, Compose files, env files, and access control automatically.

## Documentation

Full reference lives in **[docs/](docs/README.md)** — the docs index links every guide, grouped by topic. Highlights:

| Topic | Doc |
|-------|-----|
| What changed between releases | [CHANGELOG.md](CHANGELOG.md) |
| Feature overview & comparison | [docs/features.md](docs/features.md) |
| Setup wizard walkthrough | [docs/onboarding.md](docs/onboarding.md) |
| `services.yml` schema (the core config) | [docs/services.md](docs/services.md) |
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
- SSH access to the target (root or `bay-admin` — auto-detected)

Install uv if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Quick start

```bash
mkdir my-infra && cd my-infra
git clone https://github.com/AltanS/bay.git .bay
.bay/bootstrap.sh
bin/bay setup
```

`bootstrap.sh` pins the framework version, installs all dependencies, and creates the `bin/bay` wrapper. `bin/bay setup` then runs the interactive wizard and generates the project scaffold.

If you already have a scaffold's `Makefile`, `make bay:setup` is equivalent — it clones the framework and calls `.bay/bootstrap.sh`. Override its `BAY_REPO` variable to clone from somewhere else, over SSH or from a fork.

The wizard walks you through:

1. **Project name** — used as the stack name and `/opt/<name>` directory
2. **Deployment mode** — single server or multi-region
3. **Server address** — IP or hostname (or per-region IPs for multi-region)
4. **Base domain** — e.g. `example.com` → services get subdomains like `status.example.com`
5. **SSL email** — for Let's Encrypt automatic certificates
6. **SSH keys** — fetch from GitHub or paste manually
7. **Access gateway** — Headscale (recommended), WireGuard, or none
8. **Services** — pick from the catalog (Gatus, Vaultwarden, n8n, Plausible, Umami) and accessories (PostgreSQL, Redis, MariaDB)
9. **Vault password** — generated or manual, for encrypting secrets

After confirming, the wizard generates all config files under `group_vars/`, `hosts/`, and project root. See **[docs/onboarding.md](docs/onboarding.md)** for the full file list.

#### Non-interactive setup (for agents/scripts)

```bash
# Fully non-interactive — all required flags, no wizard prompts
bin/bay setup \
  --name myapp \
  --server-ip 1.2.3.4 \
  --domain example.com \
  --gateway headscale \
  --headscale-domain hs.example.com \
  --services gatus,vaultwarden
```

Partial flags pre-fill the wizard; `--defaults` skips prompting entirely. `bin/bay setup --help` is the flag reference.

#### Pre-flight check and deploy

```bash
# Validate DNS, SSH, vault password, gateway config
bin/bay doctor

# Validate config files — YAML/schema, inventory, vault keys
bin/bay validate

# Provision server (first time — hardens SSH, installs Docker, firewall)
bin/bay provision production
# First provision needs root; if ansible_user isn't root yet, override the SSH user:
bin/bay provision production -- -u root

# Deploy services
bin/bay deploy production
```

`bin/bay doctor` checks your **environment** (DNS resolution, SSH reachability,
vault password present). `bin/bay validate` checks your **config** (YAML
syntax, the services schema, inventory, vault keys) and also runs
automatically before every deploy, so running it here is optional — useful
for iterating on config without waiting for a full deploy.

#### Edit existing config

Re-run `bin/bay setup` on an existing project to modify settings. Current values are shown as defaults — press Enter to keep them. Modified files are backed up with `.bak` suffix.

### Shell alias (optional)

Add an alias so you can run `bay` instead of `bin/bay` from anywhere in the project:

```bash
# bash (~/.bashrc)
alias bay='bin/bay'

# zsh (~/.zshrc)
alias bay='bin/bay'

# fish (~/.config/fish/config.fish)
alias bay 'bin/bay'
```

After reloading your shell, you can use `bay deploy production` directly.

## Project structure

```
bay/
  bootstrap.sh                 # New project scaffolding script
  bay.mk                     # Thin Makefile aliases (backwards compat)
  version.yml                  # Framework version declaration (bay_version)
  ansible.cfg                  # Ansible settings (inventory, vault, roles path)
  provision.yml                # Server hardening playbook
  deploy.yml                   # Service deployment playbook (two-phase)
  webhook.yml                  # Webhook setup playbook (deploy keys + receiver)
  restore.yml                  # Backup restore playbook
  requirements.yml             # External Galaxy role dependencies
  pyproject.toml               # Python deps (Typer, Rich, Ansible)
  src/bay_cli/                # Python CLI package
    cli.py                     # Typer app, command registration, entry point
    runner.py                  # Subprocess runner with Rich spinners
    git.py                     # Git operations (fetch, checkout, tags)
    ansible.py                 # Ansible operations (galaxy, playbooks, vault)
    guards.py                  # Pre-flight checks (version drift, git health)
    paths.py                   # Path resolution (.bay/, .bay-version)
    console/                   # Rich output, banner, theme
    commands/                  # Subcommand modules
      framework.py             # setup, install, update, status
      ops.py                   # deploy, provision, restore
      vault.py                 # vault edit/view/encrypt/decrypt
      secret.py                # secret generation and password hashing
      backup.py                # backup list/run/restore/status/check
      test.py                  # infrastructure tests
      webhook.py               # webhook setup and GitHub instructions
  example/                     # Consumer project template (copied by `bin/bay setup`)
    Makefile                   # Sets BAY_REPO, includes .bay/bay.mk
    ansible.cfg                # Points roles_path to .bay/
    deploy.yml                 # Wrapper → imports .bay/deploy.yml
    provision.yml              # Wrapper → imports .bay/provision.yml
    restore.yml                # Wrapper → imports .bay/restore.yml
    .bay-version              # Framework version lock (e.g., v0.1.0)
    .gitignore                 # Ignores .bay/, .vault_pass, etc.
    README.md                  # Getting started guide
    hosts/
      production               # Example production inventory
    group_vars/
      all/
        main.yml               # Core config (users, docker, project identity)
        security.yml           # Firewall, CrowdSec, SSH hardening
        services.yml           # Service and accessory definitions (the key file)
        users.yml              # User accounts and SSH keys
        vpn_access.yml         # VPN IP whitelist
      production/
        main.yml               # Production-specific overrides
        domains.yml            # Domain names, Let's Encrypt email
        secrets.yml            # Vault-encrypted credentials
  roles/
    common/                    # System baseline (apt packages, swap, unattended upgrades)
    users/                     # User and SSH key management
    sshd_hardening/            # SSH drop-in hardening (MaxStartups, LoginGraceTime)
    nftables/                  # Firewall rules
    traefik/                   # Reverse proxy, SSL, routing labels
    deploy_stack/              # Stack deployment orchestration (env, config, containers, db)
    build_image/               # Image build strategies (registry, local, cloud)
    git_deploy/                # Git clone, Docker build, deploy keys, webhook receiver
    backup/                    # Restic backups (pg_dump, mysql, redis, file → S3)
    watchtower/                # Container image update monitoring and auto-update
    access_gateway/            # VPN backend orchestration (wireguard or headscale)
    headscale/                 # Headscale coordination server (self-hosted Tailscale)
    tailscale_node/            # Tailscale daemon — VPS joins its own tailnet
    docker_monitor/            # Container crash monitor (systemd service)
    cronjobs/                  # Maintenance cron jobs
  docs/                        # Full documentation — see docs/README.md for the index
    README.md                  # Docs hub: every guide grouped by topic
    features.md                # Feature overview and competitive advantages
    services.md                # services.yml schema reference (the core config)
    onboarding.md              # Setup wizard walkthrough
    access-gateways.md         # VPN gateways (WireGuard vs Headscale)
    tailnet-ingress.md         # Tailnet HTTPS ingress, ACL, identity
    build-pipeline.md          # Webhook → build → deploy reference
    backups.md                 # Backup setup, S3 config, retention, restore
    multi-region.md            # Multi-region deployment guide
    crowdsec.md                # CrowdSec IDS/IPS
    reconciler.md              # Server-side deploy reconciler
    adr/                       # Architecture Decision Records
    ...                        # + more — see docs/README.md
  vendor/roles/                # External Galaxy roles (gitignored)
```

## Playbooks

| Playbook | Purpose | Usage |
|---|---|---|
| `provision.yml` | One-time server hardening: users, SSH, firewall, CrowdSec, Docker | `bin/bay provision production` |
| `deploy.yml` | Repeatable deployment: build/pull images, deploy containers, write rig state | `bin/bay deploy production` |
| `webhook.yml` | Webhook setup: deploy keys, receiver container, systemd triggers | `bin/bay webhook production` |
| `restore.yml` | Restore an accessory from backup | `bin/bay restore production` |

All playbooks require a target environment as the first argument.

### Deploy modes: rig vs fast

Deploy separates **infrastructure roles** (nftables, traefik, watchtower, access_gateway, backup, docker_monitor, cronjobs) from **app roles** (build_image, git_deploy, deploy_stack). A rig state file on the server (`{{ stack_dir }}/.rig-state`) tracks when infrastructure was last configured:

- `bin/bay deploy production` — checks rig state. If the framework version or consumer config changed since the last rig, runs a full deploy. Otherwise skips infra roles for a fast app-only deploy.
- `bin/bay deploy --rig production` — forces all roles to run, including infrastructure. Writes updated rig state on success.
- `bin/bay deploy production --tags deploy_stack` — manual tag override, bypasses rig logic.

The rig state file contains:
```json
{"rigged_at": "2026-03-19T22:24:07Z", "bay_version": "0.49.2", "consumer_ref": "0281637"}
```

### Deploy privilege separation

The deploy playbook runs in three phases to minimize root usage:

1. **Root bootstrap** — creates the stack directory under `/opt` (requires root), sets ownership to `app:docker`, and creates the ACME cert file (`root:root 0600`, required by Traefik)
2. **App deploy** — everything else runs as the `app` user via `become_user`. The `app` user has `docker` group membership, so it can manage containers and images without root.
3. **System services** — monitoring and cron jobs (requires root for systemd/logrotate)

This reduces blast radius if a container is compromised — the deploy pipeline never runs as root except for directory creation and system service setup.

## Services and accessories

Everything is defined in a single `group_vars/all/services.yml` — it drives Traefik labels, Compose generation, env files, access control, backups, and update policies.

- **Services** are app containers that get Traefik routing, SSL, and access control
- **Accessories** are infrastructure (databases, caches) deployed alongside services

See **[docs/services.md](docs/services.md)** for the full schema reference, access modes, environment variables, basic auth, and middleware options.

## Secrets management

Secrets are managed with `ansible-vault`. The setup:

1. **`group_vars/production/secrets.yml`** (in your consumer repo) holds all secret values under a `secrets:` dict
2. Services reference secrets by name in `services.yml` → `env.secret: [DB_PASSWORD, ...]`
3. At deploy time, the `deploy_stack` role resolves secrets and writes per-service `.env` files
4. The vault password lives in `.vault_pass` (gitignored), configured in `ansible.cfg`

Manage secrets with `bin/bay vault` (edit, view, encrypt, decrypt, set) and generate values with `bin/bay secret` — see `bin/bay vault --help` and `bin/bay secret --help` for examples and the secrets key-casing convention.

## Using Bay as a framework

Bay is designed to be used as a framework included by a site-specific consumer repo. The consumer holds real config (inventory, secrets, service definitions) while Bay provides the roles and playbooks. See [Quick start](#quick-start) to scaffold a new consumer project.

### Consumer repo structure

```
my-infra/
├── .bay/                   # Framework (cloned by git, set up by .bay/bootstrap.sh, gitignored)
├── bin/bay                 # CLI wrapper (calls uv run --project .bay bay)
├── Makefile                 # Bootstrap target + backwards-compat aliases
├── ansible.cfg              # Points roles_path to .bay/
├── deploy.yml               # Wrapper → imports .bay/deploy.yml
├── provision.yml            # Wrapper → imports .bay/provision.yml
├── restore.yml              # Wrapper → imports .bay/restore.yml
├── group_vars/              # Real configuration and secrets
└── hosts/                   # Real inventory
```

The primary CLI interface is `bin/bay` — a thin wrapper that calls `uv run --project .bay bay`. The Makefile provides `make bay:*` aliases for backwards compatibility.

[SKILL.md](SKILL.md) is the framework's orientation document for an AI agent working in a consumer repo: the rules that bite, the whole command inventory (compiled from the CLI itself), and the doc map. `install` and `update` link it to `.claude/skills/bay/SKILL.md` so a skill router finds it, and the link points through `.bay/` so the content always matches the pinned framework version. `bin/bay --skill` prints it raw for piping anywhere else.

### Versioning

Consumers pin to a specific framework release via `.bay-version` — a single-line file containing a git tag (e.g., `v0.5.0`). Both `setup` and `install` read this file and checkout the pinned ref.

```bash
# Install the version pinned in .bay-version
bin/bay install

# Update to the latest framework release (bumps .bay-version)
bin/bay update

# See current version status
bin/bay status
```

**Before updating, read [CHANGELOG.md](CHANGELOG.md)** — it lists what changed between releases, with an *Upgrade notes* section for anything needing manual action (a provision run, a renamed variable, a migration). `bin/bay update` moves you to the latest tag; the changelog is how you find out what that brings.

If `.bay-version` is missing, setup and install resolve to the latest tag and create the file automatically.

Operational commands (`deploy`, `provision`, `restore`) check for version drift before running — if `.bay-version` doesn't match the checked-out framework, the command fails with a clear error and instructions to run `bin/bay install`.

After operations complete, a notice is shown if a newer framework version is available locally.

#### Runtime compatibility check

Consumers can optionally set `bay_minimum_version` in their `group_vars` to gate deploys on a minimum framework version:

```yaml
# group_vars/all/main.yml
bay_minimum_version: "0.1.0"
```

If the framework version is older than the minimum, the playbook aborts with a clear error message before any roles execute.

### CLI commands

The CLI help is the command reference — it is kept accurate against the code and every command carries copy-pasteable examples:

```bash
bin/bay --help              # all commands, grouped by area
bin/bay deploy --help       # per-command flags, quirks, and examples
bin/bay gateway --help      # sub-apps (gateway, vault, backup, build, service, server) list their own commands
```

Make aliases (`make bay:deploy production`, etc.) are still available for backwards compatibility.

### DNS

Set a wildcard A record for your domain:

```
*.example.com  →  <server-ip>
```

Every service in `services.yml` picks a subdomain (e.g., `status.example.com`). No DNS changes needed when adding new services.

### SSL

Traefik uses Let's Encrypt **HTTP-01 challenge** — certs are issued automatically per service on first request. No wildcard certs, no DNS provider API needed.

Set the ACME email in your consumer's `group_vars/production/domains.yml`:

```yaml
letsencrypt_email: you@example.com
```

## Testing

```bash
make test                # Run all tests
make test-framework      # Framework unit tests only
make test-bootstrap      # Bootstrap end-to-end test only
make lint                # ansible-lint with production profile
```

See [Framework development](#framework-development) for details on each test suite.

## Backups

Bay uses [restic](https://restic.net/) for deduplicated, encrypted backups to S3-compatible storage. Add `backup: true` to any accessory in `services.yml` and the backup method is auto-detected from the image name (PostgreSQL, MySQL/MariaDB, Redis). Each accessory gets its own restic repository, systemd timer, and retention policy.

```yaml
accessories:
  postgres:
    image: postgres:17
    backup: true          # auto-detects pg_dump
```

See **[docs/backups.md](docs/backups.md)** for full setup instructions, S3 provider reference, retention options, restore procedures, and monitoring.

## Alerting

Bay alerts on container crashes, build failures, deploy outcomes, disk pressure, and backup failures. Telegram is built in; a **generic webhook sink** can fire alongside it so you can route alerts to Campfire, Slack, or anything that accepts an HTTP POST — without patching templates inside `.bay/`.

```yaml
# group_vars/production/main.yml
alert_webhook_url: "{{ secrets.alert_webhook_url }}"
alert_webhook_format: campfire       # campfire | slack | raw
```

The vault key must be **lowercase** — it is an Ansible role var, and Bay's convention is that UPPERCASE `secrets:` keys are container env vars. An UPPERCASE spelling silently resolves undefined.

Both sinks are best-effort: a dead alert endpoint can never fail a deploy, a backup, or a build. The webhook is off by default and inert when off — no outbound calls, and no container recreation for consumers who don't enable it.

See **[docs/alerting.md](docs/alerting.md)** for the format adapters, the full list of what gets sent, and troubleshooting.

## Container auto-updates

Bay uses [Watchtower](https://github.com/nicholas-fedor/watchtower) (nickfedor fork, Docker 29+ compatible) to monitor container images for updates. By default, Watchtower runs in **monitor-only mode** — it checks for new images daily at 4 AM UTC and sends Telegram notifications, but does not pull or restart anything. Services opt in to automatic updates via `update: auto` in `services.yml`.

See **[docs/services.md](docs/services.md#container-auto-updates)** for the `update` key reference. Override watchtower defaults in `group_vars`:

```yaml
watchtower_enabled: true              # Enable/disable entirely (default: true)
watchtower_schedule: "0 0 4 * * *"    # 6-field cron (default: 4 AM daily)
watchtower_monitor_only: true         # Global default mode (default: true)
watchtower_cleanup: true              # Remove old images after update (default: true)
```

## Multi-region deployments

Bay supports deploying the same stack to multiple regional servers from a single consumer repo with zero framework changes. Define regions as Ansible inventory groups, override per-region configuration (domains, secrets, VPN peers) via `group_vars/<region>/`, and target individual regions or all at once with the standard CLI commands.

```bash
bin/bay deploy eu                 # Deploy to EU region only
bin/bay deploy na                 # Deploy to NA region only
bin/bay deploy production         # Deploy to all regions
```

See **[docs/multi-region.md](docs/multi-region.md)** for the full setup guide — inventory structure, group_vars layering, domain parameterization, per-region secrets, and operational workflows.

## Build from source (GitHub deploy)

Services can be built from a Git repository instead of pulling from a registry. Replace `image:` with a `build:` block in `services.yml`, and the framework clones the repo, builds the Docker image locally, and tags it with the commit SHA. A webhook receiver listens for GitHub push events and triggers automatic rebuilds — without exposing the Docker socket.

```yaml
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
  secret: "{{ vault_webhook_secret }}"
```

```bash
bin/bay webhook production        # Deploy webhook infra + show GitHub setup instructions
bin/bay webhook production --keys-only  # Just show deploy keys
```

See **[docs/services.md](docs/services.md#build-from-source)** for the full `build:` schema, image tagging, and webhook configuration.

Auto-builds include a circuit breaker (stops after 3 failures), health checks with rollback, build timeouts, and notification dedup. See **[docs/build-strategies.md](docs/build-strategies.md#circuit-breaker)** for details. Reset with `bin/bay build reset <service>`.

## Architecture notes

### Access gateways

Bay supports two VPN gateway backends for services with `access: vpn`: **WireGuard** (manual peer configuration, static IPs) and **Headscale** (self-hosted Tailscale coordination server with automatic tunnel management and OIDC self-service enrollment). Set `access_gateway: wireguard` (default) or `access_gateway: headscale` in `group_vars` to select a backend. Both gateways feed into the same downstream pipeline (nftables, CrowdSec, Traefik IPAllowList), so service definitions work identically with either option.

VPN services are seamlessly accessible via their public domain when the client is on the tailnet. Headscale's MagicDNS split-DNS automatically resolves VPN service domains to the server's tailnet IP for enrolled clients, so requests travel through the tunnel and pass the IPAllowList — no `/etc/hosts` hacks or tailnet IP bookmarks needed. Non-tailnet clients still get blocked with 403.

See **[docs/access-gateways.md](docs/access-gateways.md)** for traffic flow diagrams, split-DNS details, configuration examples, and a detailed comparison.

### Headscale quick start

1. **Configure** — set `access_gateway: headscale` and `headscale_domain` in `group_vars/all/main.yml`
2. **DNS** — point `hs.example.com` (A record) to your server IP
3. **Provision + deploy** — `bin/bay provision production && bin/bay deploy production`
4. **Create user + pre-auth key** — `bin/bay gateway add-user alice && bin/bay gateway key alice`
5. **Enroll device** — install the Tailscale app, run `tailscale up --login-server https://hs.example.com --authkey <key>`
6. **Verify** — `bin/bay gateway nodes` — the device should appear in the node list

See **[docs/access-gateways.md](docs/access-gateways.md#headscale-quick-start)** for the detailed walkthrough.

### Traefik with host networking

Traefik runs with `network_mode: host` to see real client IPs (no Docker NAT). App containers live on a named bridge network (`services`). Traefik discovers them via the Docker socket API and routes to their bridge IPs.

### CrowdSec integration

CrowdSec reads Traefik access logs and SSH auth logs. It shares decisions with the nftables bouncer, which adds offending IPs to an nftables blocklist set. VPN IPs are whitelisted in both CrowdSec and Traefik.

### Deploy lock

The `deploy_stack` role acquires a file-based deploy lock before deploying, preventing concurrent deploys. Stale locks (older than 1 hour) are automatically ignored.

## Renaming `stack_name`

The `stack_name` variable (set during `bin/bay setup` or in `group_vars/all/main.yml`) controls more than the project label. Changing it has cascading effects across the deployment:

- **Container and volume name prefixes** — all containers and volumes are named `{stack_name}-*` / `{stack_name}_*`
- **Volume name prefixes** — all persistent volumes are named `{stack_name}_*`
- **Stack directory** — `/opt/{stack_name}/` on the server (Compose file, env files, configs)
- **Headscale user namespace** — if using the headscale gateway, `stack_name` is the Headscale user (since v0.40.0)
- **Node hostnames** — tailnet nodes are registered under the stack namespace
- **Config/env file paths** — everything under `/opt/{stack_name}/`

### Volume data loss risk

Deploying with a new `stack_name` creates a fresh set of empty volumes (`newname_*`) while all existing data remains in the old volumes (`oldname_*`). Databases, vaultwarden vaults, monitoring history — everything appears lost until volumes are manually migrated.

### Safe migration procedure

1. **Stop old containers** (keep volumes):
   ```bash
   docker stop $(docker ps -q --filter name=oldname-)
   docker rm $(docker ps -aq --filter name=oldname-)
   ```
2. **Deploy the new stack** so the new containers and volumes are created:
   ```bash
   bin/bay deploy production
   ```
3. **Stop new containers**:
   ```bash
   docker stop $(docker ps -q --filter name=newname-)
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
   bin/bay deploy production
   ```
6. **Verify** services are healthy and data is intact, then clean up:
   ```bash
   docker volume rm $(docker volume ls -q --filter name=oldname_)
   rm -rf /opt/oldname
   ```

### Headscale namespace

If using `access_gateway: headscale`, changing `stack_name` also changes the Headscale user namespace. After renaming, run `bin/bay gateway migrate-namespace` to rename the Headscale user and update node hostnames in the tailnet. Without this, enrolled devices lose connectivity to VPN-protected services.

For the default migration from the legacy `server` user (pre-v0.40.0):

```bash
bin/bay gateway migrate-namespace --dry-run    # preview
bin/bay gateway migrate-namespace              # server -> stack_name
```

For custom renames (e.g. after changing `stack_name` from `oldapp` to `newapp`):

```bash
bin/bay gateway migrate-namespace --from oldapp --to newapp --dry-run
bin/bay gateway migrate-namespace --from oldapp --to newapp
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
| `make test-bootstrap` | `tests/test_bootstrap.sh` | End-to-end bootstrap: creates a temp project from `bootstrap.sh` using the local repo, then runs the consumer test suite against it |
| `make test` | Both | Runs framework + bootstrap tests |
| `make lint` | — | `ansible-lint` with production profile (see `.ansible-lint`) |

The bootstrap test uses `BAY_REPO=<local path>` so it clones from the working tree — no GitHub access needed. It validates the full consumer lifecycle: scaffold files, ansible.cfg paths, wrapper playbook imports, YAML integrity, required variables, and playbook resolution (`--list-tasks` on all three playbooks).

### Consumer test suite

Every bootstrapped consumer ships with `tests/test_infra.sh` (from `example/tests/`). Consumers run it via `bin/bay test`. It validates the same checks as the bootstrap test but against the real consumer project.

### Workflow

```bash
# Edit roles, templates, playbooks...
make test                    # Verify nothing is broken
make lint                    # Check style
git commit                   # Commit framework changes
make release VERSION=0.5.1   # Maintainers only: bump version.yml, tag, push

# In a consumer repo:
bin/bay update           # Bump to latest release (updates .bay-version)
bin/bay test             # Verify consumer still works
git add .bay-version && git commit -m "chore: bump bay to v0.5.1"
```

Never tag or push a release by hand — `make release` bumps `version.yml`, commits, tags and pushes in one step, and a hand-written tag leaves `version.yml` behind. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, the checks a change must pass, and the release process.

## License

Bay is released under the [MIT License](LICENSE).
