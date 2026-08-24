# services.yml Reference

`group_vars/all/services.yml` is the single source of truth for your deployment. It drives Traefik labels, Docker Compose generation, env files, access control, backups, and container update policies.

- **Services** are app containers that get Traefik routing, SSL, and access control
- **Accessories** are infrastructure (databases, caches) deployed alongside services

## Service Schema

```yaml
services:
  <name>:
    # ── Required ──────────────────────────────────────────────────
    access: public | vpn             # Access mode
    image: <image:tag>               # Docker image
    # OR (mutually exclusive with image)
    build:                           # Build from git repo
      repo: git@github.com:user/app.git  # Git repository URL (required)
      branch: main               # Branch to track (default: main)
      strategy: local            # local | remote | registry (default: git_deploy_build_strategy; 'push' is deprecated alias for 'remote')
      mem_limit: 2g              # Override server-level build memory cap
      dockerfile: Dockerfile     # Dockerfile path (default: Dockerfile)
      context: .                 # Build context (default: .)
      args:                      # Build arguments (optional)
        NODE_ENV: production
      secrets:                   # Docker build secrets (optional, BuildKit)
        npmrc: "{{ vault_npmrc }}"  # id → vault value, mounted via --secret
      token: "{{ secrets.GIT_TOKEN }}"   # Access token for private repos (optional)
      paths:                         # Path filtering for webhook triggers (optional)
        exclude:                     # Skip rebuild if ALL changed files match
          - "*.md"
          - "docs/**"
          - ".github/**"
    domains:                         # Domains routed to this service
      - app.example.com
    ports:
      internal: <port>               # Container port Traefik routes to

    # ── Optional ──────────────────────────────────────────────────
    env:                             # Environment variables
      clear:                         #   Plaintext config, committed to VCS
        KEY: value
      secret:                        #   Values resolved from vault at deploy time
        - SECRET_NAME                #   list: vault key = SERVICE_NAME_SECRET_NAME
        ENV_VAR: VAULT_KEY           #   dict: vault key used directly (no prefix)

    database:                        # Database binding (auto-provisions + injects env vars)
      accessory: postgres            #   Which accessory to use
      name: myapp                    #   Database name (default: service name)
      user: myapp                    #   Database user (default: service name)

    healthcheck:                     # Traefik health check
      path: /health
      interval: 30s

    config_files:                    # Config files to deploy
      - gatus/config.yaml            #   source: files/<path>, dest: config/<path>

    volumes:                         # Bind mounts or named volumes
      - ./config:/config:ro

    depends_on:                      # Accessory dependencies
      - postgres

    public_routes:                   # VPN-mode only: paths open to everyone
      - /webhooks/
      - /api/health

    vpn_routes:                      # Public-mode only: paths restricted to VPN
      - /admin
      - /endpoints/secret

    regions:                         # Region filter (multi-region only)
      - eu                           #   Absent or empty = deploy everywhere
      - na                           #   List of region/group names

    links:                           # Cross-region service links (multi-region only)
      postgres:                      #   Target service/accessory name
        region: eu                   #   Region where target is deployed

    mem_limit: 512m                  # Docker memory limit (e.g., 256m, 1g)

    log_rotation:                    # Per-service docker log retention (overrides log_rotation_defaults)
      driver: json-file              #   json-file | local | none
      max_size: 200m                 #   digits + optional k/m/g
      max_file: 10                   #   positive integer
    # log_rotation: false            # Alternative form: opt out entirely (inherit daemon log-opts)

    log_retention:                   # Opt-in host-side archive (survives container recreation)
      days: 7                        #   Prune archives older than N days (00:00 UTC)
      max_total_size: 2g             #   Size ceiling (oldest-first prune if exceeded)
      compress: true                 #   Default; gzip rotated archives
      mode: normal                   #   normal (0640 bay:argo-logreaders) | sensitive (0600 root:root)  # legacy-argo: unix group on hosts

    update: monitor | auto | false   # Watchtower update policy (default: monitor)
    zero_downtime: true              # Enable canary zero-downtime deploy (default: false)
    replicas: 1                      # Container replicas (default: 1)
    command: <override>              # Override container CMD

    middleware:                       # Per-service middleware config
      security_headers: true         #   Opt-out of global headers (default: true)
      compress: true                 #   Opt-out of global compress (default: true)
      rate_limit:                    #   Rate limiting per source IP
        average: 100                 #     Requests per period
        burst: 50                    #     Max burst above average
        period: 1s                   #     Time window
      in_flight_req:                 #   Max concurrent connections
        amount: 100
      basic_auth:                    #   HTTP basic auth
        credentials:                 #     Cleartext credentials (hashed at deploy)
          - username: myuser
            password: mypassword
        users:                       #     OR pre-hashed htpasswd entries
          - "user:$$apr1$$..."       #     $$ escaping for Docker Compose
        realm: "Restricted"          #     Auth realm (optional)
        removeheader: true           #     Strip auth header from upstream (default: true)
      circuit_breaker:               #   Circuit breaker
        expression: "NetworkErrorRatio() > 0.5"
      retry:                         #   Retry on failure
        attempts: 4
```

## Accessory Schema

```yaml
accessories:
  <name>:
    # ── Required ──────────────────────────────────────────────────
    image: <image:tag>               # Docker image

    # ── Optional ──────────────────────────────────────────────────
    port: "127.0.0.1:host:container" # Port binding, localhost-only
    network_mode: host               # Use host networking instead of bridge

    volumes:                         # Named volumes for persistence
      - data_vol:/var/lib/data

    config_files:                    # Config files to deploy
      - redis/redis.conf             #   source: files/<path>, dest: config/<path>

    env:                             # Same clear/secret split as services
      clear:
        KEY: value
      secret:
        - SECRET_NAME                #   list (auto-prefixed) or dict (direct)

    healthcheck:                     # Docker-native health check
      test: ["CMD", "pg_isready"]
      interval: 5s
      retries: 5

    regions:                         # Region filter (multi-region only)
      - eu                           #   Absent or empty = deploy everywhere

    mem_limit: 512m                  # Docker memory limit (e.g., 256m, 1g)

    log_rotation:                    # Per-accessory docker log retention (same shape as services)
      driver: json-file              #   json-file | local | none
      max_size: 10m                  #   accessories often want smaller caps
      max_file: 3

    log_retention:                   # Opt-in host-side archive (same shape as services)
      days: 14
      max_total_size: 500m

    update: monitor | auto | false   # Watchtower update policy (default: monitor)

    backup:                          # Backup configuration
      method: pg_dump | mysql | redis | file  #   Backup strategy
      schedule: "0 3 * * *"          #   Cron schedule
      retain: 7                      #   Days to keep
```

## Build from Source

Services can be built from a Git repository instead of pulling from a registry. The `build:` block replaces `image:` — specifying both is a validation error (except for the `registry` strategy, see below).

```yaml
services:
  myapp:
    access: public
    build:
      repo: git@github.com:user/myapp.git
      branch: main
      strategy: local
      dockerfile: Dockerfile
      context: .
      args:
        NODE_ENV: production
      secrets:
        npmrc: "{{ vault_npmrc }}"
      token: "{{ secrets.GITHUB_TOKEN }}"
    domains:
      - myapp.example.com
    ports:
      internal: 3000
```

| Field | Default | Description |
|-------|---------|-------------|
| `repo` | (required) | Git repository URL (SSH or HTTPS) |
| `branch` | `main` | Branch to track for builds and webhook triggers |
| `strategy` | `git_deploy_build_strategy` | Build strategy: `local`, `remote`, or `registry` (`push` is a deprecated alias for `remote`) |
| `dockerfile` | `Dockerfile` | Path to Dockerfile relative to repo root |
| `context` | `.` | Docker build context relative to repo root |
| `args` | (none) | Build arguments passed as `--build-arg` |
| `secrets` | (none) | Docker build secrets mounted via `--secret` (BuildKit) |
| `token` | (none) | Vault reference to an access token for private repos |

### Build Strategy

The `build.strategy` field controls where images are built. Set a server-level default in `group_vars`:

| Strategy | Where it builds | When to use |
|----------|----------------|-------------|
| `local` | On the app server via buildx container driver | Default. Simple setup, no extra infrastructure. Buildkit memory-capped at `git_deploy_buildkit_memory_max` (defaults to `git_deploy_build_mem_limit`). |
| `remote` | On a dedicated build server, pushed to a registry | Offload heavy builds from production servers. Requires `build_server` variable, a Docker registry (e.g., Zot), and `docker_registries` config. |
| `registry` | Does not build | CI/CD builds externally, pushes to registry. Requires `image:` field alongside `build:`. |
| `push` | _(deprecated alias for `remote`)_ | Use `remote` instead. Will be removed in a future release. |

Server-level defaults in `group_vars`:

```yaml
git_deploy_build_strategy: local         # default for all build: services
git_deploy_build_mem_limit: "2G"         # systemd MemoryMax on bay-build@.service (wrapper cgroup; integer-only, uppercase)
git_deploy_buildkit_memory_max: "2.5G"   # docker --memory cap on buildx_buildkit_argo-builder0 (defaults to the wrapper var when unset)  # legacy-argo: live buildx builder name on hosts, migrate separately
git_deploy_build_lock_timeout: 3600      # seconds before a queued build aborts waiting for the build lock
```

Per-service `build.strategy` overrides the server default.

**Remote strategy** builds Docker images on a dedicated build server, pushes them to a private registry, and deployment servers pull them. Set `build_server` to the inventory hostname of your build server, configure `docker_registries` in `group_vars`, and set the service's `image:` to the registry path:

```yaml
# group_vars/all/main.yml
build_server: build.example.com  # inventory hostname of your build server

# group_vars/all/registry.yml
docker_registries:
  - domain: registry.example.com
    username: admin
    password: "{{ vault_registry_password }}"

# services.yml
services:
  myapp:
    build:
      repo: git@github.com:user/myapp.git
      strategy: remote
    image: registry.example.com/myapp:latest
```

The build server needs Docker and a user matching `app_user` with docker group access. The framework automatically creates a persistent clone directory (`/opt/<stack>/push-builds/`), sets up a buildx builder instance (`argo-builder`), and manages build secrets on the build server.  <!-- legacy-argo: live buildx builder name on hosts, migrate separately -->

For operators who want to build on their local machine, set `build_server: localhost`.

**Registry strategy** -- the `build:` block is informational (used by CI/documentation). The server pulls `image:` instead of building. Requires both `build:` and `image:` on the service:

```yaml
services:
  dashboard:
    access: public
    build:
      repo: git@github.com:myorg/dashboard.git
      strategy: registry
    image: ghcr.io/myorg/dashboard:latest
    domains:
      - dashboard.example.com
    ports:
      internal: 3000
```

### Build Safety

Builds run with multiple layers of OOM protection to prevent a runaway build from taking down application containers:

- **Memory-capped buildx container** -- the buildx container driver runs in its own cgroup with a hard memory limit (`git_deploy_buildkit_memory_max`, defaults to whatever `git_deploy_build_mem_limit` is set to so existing single-knob overrides keep unified caps). This is where the real memory pressure lives -- the wrapper script's own cgroup typically uses 30-100 MB. Set the buildkit var explicitly when you need asymmetric caps (e.g. wrapper at 2G, buildkit at 2.5G for headroom). When buildkit exceeds the cap, the build is killed and `bay-build-alert@.service` fires a Telegram OOM alert.
- **Wrapper cgroup cap** -- `bay-build@.service` carries `MemoryMax={{ git_deploy_build_mem_limit }}` (default `2G`) as defense-in-depth on the rebuild.sh wrapper itself. Does not catch buildkit OOMs (separate cgroup), but contains anything spawned by `rebuild.sh` directly. systemd accepts only integer suffixes here -- lowercase or decimals are silently rejected.
- **Cross-service serialization** -- `rebuild.sh` acquires an exclusive `flock` on `git_deploy_build_lock_path` (default `/run/bay-build.lock`) before doing any work, so at most one build runs at a time per host. Concurrent path-unit firings (common with monorepo pushes that match several services' globs) queue instead of competing for the shared buildkit container. Bounded by `git_deploy_build_lock_timeout` (default 3600s) -- if a queued build can't acquire the lock in that window it aborts with a `⏱️ Build lock timeout` Telegram alert pointing at `journalctl -u bay-build@*` for triage. **Important:** `After=` / `Wants=` between path units does NOT serialize concurrent path-unit firings -- systemd starts each instance independently. The `flock` is the only thing that prevents concurrent buildkit invocations.
- **OOMScoreAdjust=1000** on the systemd build service (`bay-build@.service`) -- under system-wide memory pressure, builds are the first processes the kernel OOM-killer targets.
- **OOMScoreAdjust=-900** on the boot safety service (`bay-infra-boot.service`) -- after an OOM event or hard reboot, infrastructure containers (Traefik, etc.) are recovered automatically.
- **Scheduled cache prune** -- `/usr/local/bin/bay-docker-builder-prune` (installed by the `cronjobs` role) sweeps every builder in `docker_prune_builders` -- the `default` builder *and* the docker-container-driver builder named by `bay_buildx_builder`, retaining `docker_prune_builder_keep_storage` (default `2G`) of hot cache. `docker system prune` runs alongside it. Weekly by default; daily on the `build_server`. Nothing prunes cache before an individual build.

  Pruning the argo builder specifically is not optional bookkeeping: its cache lives in its own Docker volume (`buildx_buildkit_<builder>0_state`), which `docker builder prune` does not reach and `docker system prune -af --volumes` cannot remove while the buildkit container holds it open. `docker system df` reports only the *default* builder, so an unpruned host reads as healthy until the disk fills. The real number is `docker buildx du --builder argo-builder`. (Fixed in v0.111.2 -- GH#34.)  <!-- legacy-argo: live buildx builder name on hosts, migrate separately -->

  **buildx builder registrations are per-user.** The builder is created as `app_user`, so it appears in `~<app_user>/.docker/buildx` and `docker buildx ls` **as root shows only the default builder** -- the buildkit container and its cache volume are there, but the name resolves to nothing. Anything pruning or inspecting it from a root context (the cron, `bin/bay prune`, your own SSH session) must switch user first; the prune script resolves the owner from `docker_prune_builder_users` and runs both the probe and the prune as that user. A builder with a live buildkit container that no listed user can reach is logged as a `WARNING`, since a silent skip is indistinguishable from a healthy run. (v0.111.3.)

### Private repositories

Private repos require an access token with read scope. Store the token in vault and reference it via `build.token`:

```bash
bay vault set production GITHUB_TOKEN ghp_xxxxx
```

```yaml
build:
  repo: git@github.com:Org/private-app.git
  token: "{{ secrets.GITHUB_TOKEN }}"
```

The framework auto-converts SSH URLs to HTTPS and authenticates using the token. This works with any Git host that supports HTTPS token auth (GitHub, GitLab, Gitea, Forgejo, etc.). Each service can use a different token — store multiple tokens in vault for different providers or orgs.

Public repos don't need a token — omit `build.token` and the framework clones via SSH with an auto-generated deploy key.

### `build.token` reference

#### Required format

`build.token` must use the Jinja2 vault reference form:

```yaml
build:
  token: "{{ secrets.KEY_NAME }}"
```

This is the only supported format. The framework renders the resolved token value into `rebuild.sh` at deploy time. `bin/bay validate` checks the vault for the referenced key at deploy-time — a missing or empty key produces a validation error before any deploy proceeds.

Use `bin/bay service add --build-token KEY_NAME` to set this correctly when adding a new service, or set it manually using the form above.

#### PAT scope requirements

| Token type | Required scopes |
|------------|----------------|
| **Classic PAT** | `repo` scope (includes Contents + Metadata read; classic token type) |
| **Fine-grained PAT** | `Contents: Read` + `Metadata: Read` — scoped to the specific repository or all repositories in the org |

Fine-grained PATs are preferred for least-privilege access — scope them to only the repositories that need to be cloned.

#### Security model

The resolved token is rendered into `/opt/<stack>/bin/rebuild.sh` in **plaintext** on the build host. This is intentional — the build server must authenticate to GitHub at clone time — but operators must be aware:

- Anyone with shell access to the build server can read the token from `rebuild.sh`.
- Use a minimal-scope PAT dedicated to the specific repository or organization rather than a personal token with broad access.
- Rotate tokens using `bin/bay vault edit production`, then redeploy (`bin/bay deploy production --tags deploy_stack`) to re-render `rebuild.sh`.

#### Per-org pattern

When services span multiple GitHub organizations (e.g. `acmecorp/*` and `widgetco/*`), create one PAT per org and store them under distinct vault keys:

```bash
bin/bay vault edit production
# add:
#   ACMECORP_GIT_TOKEN: ghp_aaaaaa
#   WIDGETCO_GIT_TOKEN: ghp_bbbbbb
```

```yaml
services:
  api:
    build:
      repo: git@github.com:acmecorp/api.git
      token: "{{ secrets.ACMECORP_GIT_TOKEN }}"

  dashboard:
    build:
      repo: git@github.com:widgetco/dashboard.git
      token: "{{ secrets.WIDGETCO_GIT_TOKEN }}"
```

#### Validation

`bin/bay validate` checks:
- The vault key referenced by `build.token` exists in the decrypted vault
- The vault key value is non-empty

If the vault is not decryptable (passphrase missing), token presence is skipped with a visible warning — not silently ignored.

`bin/bay validate --check-token-scope` (opt-in) makes a live GitHub API probe to verify the PAT has sufficient scope for the referenced repository.

### Image tagging

Images are tagged as `bay-<stack>-<service>:<sha>` (immutable) and `bay-<stack>-<service>:latest` (mutable). The previous `:latest` is preserved as `:previous` for rollback.

## Webhook Configuration

Bay can automatically rebuild and redeploy services when code is pushed to GitHub. The flow:

1. **GitHub** sends an HTTP POST to your webhook URL when code is pushed
2. **Bay's webhook receiver** (deployed automatically) validates the payload, checks the branch and path filters, and writes a trigger file
3. **systemd** detects the trigger and runs `rebuild.sh`, which fetches the latest code, builds the Docker image, and restarts the container

This works with any Git hosting that supports webhook delivery (GitHub, GitLab, Gitea, etc.). Bay handles the server side — you just need to register the webhook URL in your Git host.

### Setup

Add a top-level `webhook:` key in `services.yml`:

```yaml
webhook:
  domain: deploy.example.com
  secret: "{{ vault_webhook_secret }}"
```

| Field | Description |
|-------|-------------|
| `domain` | Domain for the webhook receiver (routed by Traefik) |
| `secret` | HMAC-SHA256 secret for validating GitHub payloads |

Then register the webhook in your Git host:

1. Go to the repository's **Settings → Webhooks → Add webhook**
2. Set **Payload URL** to `https://deploy.example.com/webhook`
3. Set **Content type** to `application/json`
4. Set **Secret** to the same value as `webhook.secret` in your config
5. Select **Just the push event**

The webhook receiver validates push events, filters by branch and path, and triggers rebuilds via systemd. It runs without Docker socket access for security.

### Build strategies and webhooks

How webhooks interact with build strategies:

| Strategy | Webhook behavior |
|----------|-----------------|
| `local` | Webhook triggers a full build on the deployment server |
| `remote` | Webhook on the build server triggers build + push to registry, then notifies deployment servers to pull *(planned — see below)* |
| `registry` | No build — webhooks are not used (image comes from external CI) |

**Note:** `strategy: remote` webhook support requires the webhook receiver to be deployed on the build server. Until that is configured, pushes to remote-strategy services will trigger a notification but not an automatic build. Use `bin/bay deploy` to build and deploy manually.

Run `bin/bay webhook <env>` to deploy the receiver and get setup instructions.

### Path Filtering

By default, every push to a service's tracked branch triggers a rebuild. Add `build.paths` to skip rebuilds when only irrelevant files change:

```yaml
services:
  myapp:
    build:
      repo: git@github.com:user/myapp.git
      branch: main
      paths:
        exclude:
          - "*.md"
          - "docs/**"
          - ".github/**"
          - "LICENSE"
```

**Semantics:**

- **`exclude` only:** Rebuild unless ALL changed files match exclude patterns. ("skip docs-only commits")
- **`include` only:** Rebuild only if ANY changed file matches an include pattern. ("only rebuild on source changes")
- **Both:** Include is checked first (must have at least one match), then exclude removes matches. Rebuild if any files remain.

**Glob syntax:** Uses `.gitignore`-style matching via the `pathspec` library:

| Pattern | Matches |
|---------|---------|
| `*.md` | Any `.md` file at any depth (`README.md`, `docs/guide.md`) |
| `docs/**` | Everything under `docs/` |
| `Dockerfile` | Exact file at repo root |
| `.github/**` | Everything under `.github/` |

**Safety heuristics:**

- **Force push** (`git push --force`): Always rebuilds — file lists are unreliable after history rewrite.
- **Large push** (20+ commits): Always rebuilds — GitHub truncates the commit list at 20, so the file list may be incomplete.
- **Branch deletion**: Skipped entirely (no rebuild).

**Debugging:** Check the GitHub webhook delivery log — the response body includes the skip reason and file count. Container logs (`docker logs bay-webhook`) show the full file list and match result.

**Include example** (monorepo — only rebuild when `apps/web/` changes):

```yaml
services:
  webapp:
    build:
      repo: git@github.com:user/monorepo.git
      branch: main
      paths:
        include:
          - "apps/web/**"
          - "packages/shared/**"
```

## Access Modes

| Mode | Behavior |
|------|----------|
| `public` | Routed by Traefik to everyone. CrowdSec and nftables still protect. Use `vpn_routes` to restrict specific paths to VPN only. |
| `vpn` | Restricted to IPs in `vpn_access.yml` via Traefik IPAllowList. Use `public_routes` to exempt specific paths (webhooks, health endpoints). |

**What to run after changes:**

- Changed `access` mode, `public_routes`, or `vpn_routes` → `bin/bay deploy production`
- Changed `vpn_allowed_ips` → `bin/bay provision production` then `bin/bay deploy production`

## Environment Variables

Each service/accessory supports two kinds of env vars:

- **`env.clear`** — plaintext key-value pairs, committed to VCS
- **`env.secret`** — variable names whose values are resolved from `group_vars/<env>/secrets.yml` at deploy time

Two formats for secrets:

```yaml
# List — auto-namespaced: vault key = SERVICE_NAME + '_' + env var name
# e.g., in a service called "myapp":
secret:
  - DB_PASSWORD       # env var: DB_PASSWORD, vault key: MYAPP_DB_PASSWORD
  - SESSION_SECRET    # env var: SESSION_SECRET, vault key: MYAPP_SESSION_SECRET

# Dict — direct mapping: env var → vault key (no auto-prefix)
# Use for accessories where the env var already contains the service name:
secret:
  POSTGRES_PASSWORD: POSTGRES_PASSWORD    # env var and vault key are the same
  DB_PASSWORD: MYAPP_DB_PASSWORD          # env var differs from vault key
```

The list form auto-prefixes vault keys with the service/accessory name to prevent collisions (e.g., two services both needing `APP_SECRET`). The dict form gives full control over the vault key for cases where auto-prefixing would be redundant.

### Dollar signs in env values

Docker Compose uses `$` for variable interpolation — `$FOO` would be replaced with the value of `FOO`, silently corrupting secrets that contain `$` (Argon2id hashes, bcrypt hashes, htpasswd entries).  <!-- legacy-argo: unrelated hash algorithm name (Argon2id) -->

Bay handles this automatically: the `env.j2` template escapes every `$` to `$$` when writing `.env` files. No manual escaping needed.

## Basic Auth

Protect a service with HTTP basic auth:

```yaml
services:
  myapp:
    access: public
    image: myapp:latest
    domains:
      - myapp.example.com
    ports:
      internal: 8080
    middleware:
      basic_auth:
        credentials:
          - username: myuser
            password: mypassword
```

Passwords are hashed at deploy time — no need to pre-hash. The auth header is stripped before reaching your app by default (`removeheader: true`).

For pre-hashed htpasswd entries, use `users` instead of `credentials`:

```yaml
middleware:
  basic_auth:
    users:
      - "myuser:$apr1$xyz$hashedpassword"
```

## Container Auto-Updates

The `update` key controls [Watchtower](https://github.com/nicholas-fedor/watchtower) behavior per container:

| Value | Behavior |
|-------|----------|
| `monitor` (default) | Detect new images, send Telegram notification |
| `auto` | Detect + pull + restart automatically |
| `false` | Exclude from Watchtower entirely |

Framework-managed containers (Traefik, error-pages, Watchtower itself) are always excluded.

## Zero-Downtime Deploys

Services with `zero_downtime: true` use a canary swap pattern — a new container starts alongside the old one, passes health checks, then the old one is stopped. Traefik load-balances between both containers during the overlap, so traffic is never interrupted.

```yaml
services:
  myapp:
    access: public
    zero_downtime: true
    image: myapp:latest
    domains:
      - myapp.example.com
    ports:
      internal: 3000
```

**Default is `false` (opt-in).** Enable it per service after verifying your app meets the requirements below.

### Requirements

1. **Your app must handle SIGTERM.** When `docker stop` runs, it sends SIGTERM. Your app should shut down gracefully and exit with code 0. Without this, Docker waits the full stop timeout (30s) before SIGKILL — during that window, Traefik routes requests to a dying container, causing 502 errors.

   ```js
   // Node.js
   process.on('SIGTERM', () => {
     server.close(() => process.exit(0));
   });
   ```

   ```python
   # Python (Flask/Gunicorn handles this by default)
   # Most Python web servers handle SIGTERM correctly out of the box.
   ```

   ```go
   // Go
   ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM)
   defer stop()
   ```

   Most web frameworks and servers handle SIGTERM by default (Nginx, Apache, Gunicorn, Caddy, Go net/http). Node.js and some Java apps need explicit handling.

2. **No exclusive file locks on shared volumes.** During the canary overlap (~5-10 seconds), both old and new containers mount the same volumes. If your app holds exclusive locks (SQLite databases, file-based queues), the canary may fail to start. For these services, keep `zero_downtime: false`.

### What happens during deploy

```
1. Create canary container (same Traefik labels as old)
2. Traefik discovers canary → load-balances between old + new
3. Wait for canary health check to pass
4. Stop old container → SIGTERM → app exits → Traefik shifts to canary
5. Remove old container, rename canary to final name
```

If the canary fails (crash loop, health timeout), the framework automatically falls back to standard recreate (stop old, start new) and prints a warning. The deploy succeeds either way.

### Verifying SIGTERM handling

```bash
# Start your container, then send SIGTERM:
docker kill --signal=SIGTERM <container>

# Check exit code (should be 0, not 137):
docker inspect <container> --format='{{.State.ExitCode}}'
```

If the exit code is 137 (SIGKILL) or 143 (SIGTERM propagated as exit code), your app is not handling SIGTERM properly.

## Log Rotation

Each container renders a compose-level `logging:` block so Docker's JSON log
driver rotates per-service instead of relying on whatever daemon-level
`docker_daemon_options.log-opts` happens to be in force.

**Framework defaults** (applied to every service, accessory, and infra
container unless overridden):

```yaml
# roles/deploy_stack/defaults/main.yml
log_rotation_defaults:
  driver: json-file
  max_size: "50m"
  max_file: 3
```

Override globally in `group_vars/all/main.yml`:

```yaml
log_rotation_defaults:
  driver: json-file
  max_size: "100m"
  max_file: 10
```

Override per-service or per-accessory in `services.yml`:

```yaml
services:
  blog:
    # keep ~2 weeks of access/app logs for forensics
    log_rotation:
      max_size: "200m"
      max_file: 10

accessories:
  postgres:
    # postgres logs are chatty and rarely useful after the first few files
    log_rotation:
      max_size: "10m"
      max_file: 3
```

Partial overrides merge key-by-key onto `log_rotation_defaults`; unspecified
keys inherit the default.

**Opt out entirely** (inherit daemon-level `log-opts`):

```yaml
services:
  debug-noisy:
    log_rotation: false
```

**Supported drivers:** `json-file` (default), `local`, `none`.

**Infrastructure containers** (traefik, watchtower, webhook receiver, zot,
headscale, error-pages) also pick up `log_rotation_defaults`. Traefik — whose
access logs have the highest forensic value — honours a dedicated
`traefik_log_rotation` override:

```yaml
# group_vars/all/main.yml
traefik_log_rotation:
  max_size: "500m"
  max_file: 20
```

## Log Retention

`log_rotation` tunes how much Docker keeps inside the container's own storage
— but when that container is recreated (Ansible deploy, webhook-driven
rebuild, manual `docker rm`, or crash-and-restart), Docker's log files are
deleted with it. On 2026-04-21 a signup-forensics investigation on
blog stalled because ~5.5h of access logs had evaporated after a
container recreation. `log_retention` is the durability complement to
`log_rotation`: it archives each opted-in service's stdout to an
operator-controlled path that survives container recreation, with time-based
pruning and GDPR erasure tooling.

**Both can coexist on one service.** Rotation keeps Docker's live storage
small; retention keeps a durable copy for forensics.

**Opt in per service or accessory** in `services.yml`:

```yaml
services:
  blog:
    log_retention:
      days: 7                # prune archives older than 7 days at 00:00 UTC
      max_total_size: "2g"   # size ceiling (oldest-first if exceeded)
      compress: true         # default; gzip rotated files
      mode: normal           # normal | sensitive

accessories:
  postgres:
    log_retention:
      days: 14
      max_total_size: "500m"
```

Accessories support the same key. Infrastructure containers (traefik,
watchtower, bay-webhook) are **out of scope for v1**.

### Required consumer variable

Any host with at least one `log_retention`-enabled container **must** set
`log_retention_disk_bytes` in `group_vars/<env>/main.yml` — the sum of every
service's `max_total_size` on that host is checked against
`log_retention_budget_fraction` (default `0.30`) of this value at
`bin/bay validate` time. No default is provided because server disks vary
too widely for a safe one.

```yaml
# group_vars/production/main.yml
log_retention_disk_bytes: 107374182400   # 100 GiB
# log_retention_budget_fraction: 0.30    # optional — override the default ceiling
```

An over-budget configuration fails validate with the list of services to
trim. Over-budget deploys never start.

### Host layout

For each service with `log_retention`, the role provisions:

```
/opt/<stack>/logs/services/<svc>/
├── live.log                        # active write target, appended every 60s
├── today.log                       # symlink → live.log
├── 2026-04-21.log.gz               # date-named archive (UTC)
├── 2026-04-21.log.gz.sha256        # integrity sidecar
├── ...
├── .retention                      # mode/days/max_total_size/compress
├── .cursor                         # last-archived RFC 3339 timestamp
├── .prune-log                      # append-only audit of every prune/scrub
└── .malformed/YYYY-MM-DD.log       # sidecar for unparseable lines
```

Filenames reflect the **UTC date they cover**, not the rotation timestamp —
rotation fires at `00:00:00 UTC` so `2026-04-21.log.gz` contains the 24h
window of 2026-04-21 UTC.

### Permission model

Two modes, chosen per service:

| Mode | Directory | Files | Group access |
|---|---|---|---|
| `normal` (default) | `0750 bay:argo-logreaders` | `0640 bay:argo-logreaders` | members of `argo-logreaders` can read |  <!-- legacy-argo: unix group on hosts -->
| `sensitive` | `0700 root:root` | `0600 root:root` | `argo-logreaders` is locked out |  <!-- legacy-argo: unix group on hosts -->

Use `mode: sensitive` for any service that logs detailed PII in clear text
(full request bodies, email addresses in paths, decoded tokens).

#### `debugbot` is NOT added to `argo-logreaders`  <!-- legacy-argo: unix group on hosts -->

The `argo-logreaders` group is created on first opt-in, and `app_user` is added  <!-- legacy-argo: unix group on hosts -->
to it automatically. **`debugbot` is not**. Operators must `usermod -aG
argo-logreaders debugbot` manually on hosts where a read-only SSH session  <!-- legacy-argo: unix group on hosts -->
should be able to reach archived logs. The default is opt-out because
`debugbot` is a low-friction read-only credential; auto-adding it would turn
it into a 7-day PII exfiltration vector on any consumer that enables
`log_retention` on a user-facing service.

### Daily rotation + prune-with-manifest

A per-service `bay-logrotate@<svc>.timer` fires at `00:00:00 UTC`:

1. Stops the archiver timer briefly so there's no write race on `live.log`.
2. Renames `live.log` → `YYYY-MM-DD.log` (yesterday's UTC date).
3. `gzip -9` the archive and writes a matching `.sha256` sidecar.
4. Restarts the archiver timer.
5. Prunes: files older than `days` (age) and — if set — oldest-first until
   total size fits under `max_total_size`.

Every deletion follows the **alert-before-prune-before-rm** contract:

1. Telegram alert fires first (gives the operator a window to copy files off)
2. An append-only line is written to `.prune-log`:
   ```
   2026-04-28T00:00:03Z blog/2026-04-21.log.gz 4352128 sha256:abc123... reason=age
   ```
3. Only then is the file removed.

If no files qualify, no alert is sent and no manifest line is written. This
is idempotent — re-running on a pruned directory is a no-op.

### Opt-in filesystem append-only

For extra tamper-evidence, set `log_retention_chattr: true` in group_vars
(default: `false`). Rotated `.log.gz` files and `.prune-log` get `chattr +a`
— writes can only append, not rewrite, until the prune/scrub script toggles
it off. Silently no-ops on filesystems that don't support it.

### CLI — debug trail

When `bin/bay healthcheck` reports a service FAIL, the next step is
typically:

```bash
# 1. Go straight to the archive on your local workstation
cd $(bin/bay logs blog --path --env production)

# 2. Or get a dry-run zcat pipeline for a date range
bin/bay logs blog --path --since 2026-04-20 --env production

# 3. Pure workstation view, no SSH needed for steps 1–2.
#    The path points at the archive on the target host; ssh when you need to read.
```

**Services most likely to enable `log_retention` first** are the
supervisor-pattern apps already declaring `healthcheck_path: /healthcheck`
(`storefront-*`, `blog`). Their nginx-up-Node-dead failure mode
leaves useful app-side evidence in stdout, and the archive survives the
container restart that typically clears it.

### CLI — GDPR erasure (`--scrub`)

```bash
# Dry run — shows per-file match counts, no files touched
bin/bay logs blog --scrub --pattern 'user@example\.com' --env production

# Destructive — requires --yes; removes matching lines from live.log AND
# every .log.gz; recomputes each archive's sha256 sidecar; writes a
# `reason=scrub` line to .prune-log with pattern, lines_removed, operator.
bin/bay logs blog --scrub --pattern 'user@example\.com' --yes --env production
```

The scrub is idempotent (second run finds no matches, writes no new audit
entries) and safe on `sensitive`-mode directories (the host-side script runs
as root).

### Disk trade-off: `log_rotation` + `log_retention`

With both active, **log bytes are stored twice** — Docker's rotating JSON
file and the archive directory. Once you enable `log_retention` on a
service, tighten its `log_rotation` to reduce Docker-side duplication:

```yaml
services:
  blog:
    log_rotation:
      max_size: "10m"    # was 200m — Docker keeps only recent burst
      max_file: 2
    log_retention:
      days: 7
      max_total_size: "2g"
```

The archive has everything; Docker's copy just needs enough to bridge a
60-second archiver lag.

### Timestamp semantics (not byte-identical)

`docker logs --timestamps` prepends an RFC 3339 UTC timestamp to **every**
line. Archived content is this prepended form, not the raw bytes the
application wrote to stdout.

- **Good for:** forensic timeline reconstruction (`awk '$1 >= "2026-04-22"'`
  filters by archive time).
- **Not good for:** proving "the app logged exactly X" to a third party —
  Docker's prepended timestamp is not part of the application's output.

If exact byte reproduction matters, use a logging library inside the
application that emits its own timestamps and ignore Docker's; `awk '{ for
(i=2; i<=NF; i++) printf "%s ", $i; print "" }'` strips the prepend.

### Malformed lines

If a container crashes mid-write it can leave a truncated JSON line or
non-UTF-8 bytes in stdout. Those lines are routed to
`.malformed/YYYY-MM-DD.log` instead of `live.log` so the main archive stays
clean enough that `grep` across it returns only complete, parseable lines.
The malformed sidecar has a header comment explaining what it contains and
why lines landed there.

### GDPR — Record of Processing Activities

Enabling `log_retention` creates a processing activity for personal data.
Application stdout typically contains access logs (IP addresses, user IDs,
email addresses in request paths, decoded session tokens on error paths) —
all personal data under GDPR.

Operators must:

1. Add this processing activity to their Record of Processing Activities
   (RoPA).
2. Set a retention period (`days:`) no longer than necessary for the stated
   purpose.
3. Use `bin/bay logs --scrub` to respond to data subject erasure requests
   (Article 17). The scrub command removes matching lines from live.log
   **and** every rotated archive, recomputes sha256 sidecars, and records a
   `reason=scrub` audit entry including the operator's git email.
4. Restrict access to archived logs:
   - Keep the default (`argo-logreaders` group)  <!-- legacy-argo: unix group on hosts -->
   - Only add `debugbot` (or other sub-argo-admin accounts) to the group on  <!-- legacy-argo: unix group on hosts -->
     hosts where read-only access is explicitly required
   - Use `mode: sensitive` for any service that logs PII in detail —
     archives become root-only at the filesystem level

Bay **does not** enforce these obligations — operators are the data
controllers and are responsible for GDPR compliance.

### What `log_retention` is not

- Not a log-shipping aggregator (no Loki, Vector, OpenObserve). Host-side
  `grep`/`zgrep` + the `--path` CLI is the primary interaction.
- Not applied to infrastructure containers (traefik, watchtower,
  bay-webhook) — v1 scope. If durable Traefik logs become necessary, a
  follow-up milestone extends the mechanism explicitly.
- Not a streaming `--since <duration>` wrapper. `bin/bay logs <svc>
  --since 1h` still forwards to `docker logs` (legacy behavior); date-shaped
  `--since YYYY-MM-DD` requires `--path` and produces a dry-run `zcat`
  hint for operator-side execution.
- Not cryptographically signed. Integrity is sha256 sidecars + the
  append-only `.prune-log` manifest — adequate for post-hoc tamper
  detection, not for legal-grade non-repudiation.

See also: [architecture decision record](adr/002-log-archival.md).

## Traefik Label Generation

The compose templates automatically generate Traefik labels from the service definition:

- **Chain-based middleware system:**
  - `public-chain` → secure-headers, compress, errors (each conditional on toggle)
  - `vpn-chain` → secure-headers, compress, vpn-only, errors
- **Public service** → single router with Host rule + `public-chain` + per-service middleware
- **Public service with `vpn_routes`** → two routers:
  - High-priority router for `vpn_routes` paths with `vpn-chain` (VPN required)
  - Low-priority catch-all with `public-chain` (open to everyone)
- **VPN service** → single router with `vpn-chain`
- **VPN service with `public_routes`** → two routers:
  - High-priority router for `public_routes` paths with `public-chain`
  - Low-priority catch-all with `vpn-chain`
- **Accessories** → no Traefik labels, no external routing
- **Per-service opt-out:** setting `security_headers: false` or `compress: false` creates a custom `{name}-chain` with only the enabled globals

Every listed domain is matched by every router the service gets — single- and
dual-router alike. Multiple domains are joined into one `Host(...) || Host(...)`
expression, and on the path-matched router that OR-group is parenthesised before
`&&` (`&&` binds tighter than `||`, so an unparenthesised group would attach the
path match to the last host only).

> Fixed in v0.107.9. Before
> that, a dual-router service routed only its **first** domain — the rest got no
> router and no certificate, and returned Traefik's default 404. If you added a
> domain to a `vpn_routes`/`public_routes` service and it never took effect, this
> was why; redeploy on ≥ v0.107.9.

## Domain Parameterization (Multi-Region)

For multi-region deployments, replace hardcoded domains with a variable so each region resolves to its own domain automatically.

**Before (single-region, hardcoded):**

```yaml
services:
  gatus:
    domains:
      - gatus.argo.example.com  # legacy-argo: DNS zone example, not a rename target
```

**After (multi-region, parameterized):**

```yaml
services:
  gatus:
    domains:
      - "gatus.{{ domain_base }}"
```

Set `domain_base` per-region in `group_vars/<region>/main.yml`:

```yaml
# group_vars/eu/main.yml
domain_base: eu.example.com

# group_vars/na/main.yml
domain_base: na.example.com
```

Jinja2 expressions in `group_vars` values are lazily evaluated by Ansible at runtime, so this works without any framework changes. Each host resolves `{{ domain_base }}` from its own group_vars hierarchy.

See [docs/multi-region.md](multi-region.md) for the full multi-region setup pattern.

## Cross-Region Links

For multi-region deployments, services can communicate across regions via the Headscale tailnet mesh using the `links:` field. This avoids routing traffic through the public internet.

```yaml
services:
  n8n:
    access: vpn
    image: n8nio/n8n:latest
    domains:
      - "n8n.{{ domain_base }}"
    ports:
      internal: 5678
    regions:
      - na
    links:
      postgres:
        region: eu
      platform:
        region: eu

  platform:
    access: vpn
    image: myorg/platform:latest
    domains:
      - "platform.{{ domain_base }}"
    ports:
      internal: 5100
      expose: tailnet      # ← required for cross-region link consumers
    regions:
      - eu

accessories:
  postgres:
    image: postgres:17
    port: "5432:5432"
    expose: tailnet         # ← required for cross-region link consumers
    regions:
      - eu
```

Each link entry specifies a target service or accessory and the region where it's deployed. At deploy time, Ansible resolves the target's tailnet IP and injects connection information as environment variables:

| Variable | Example | Description |
|----------|---------|-------------|
| `LINKS_POSTGRES_HOST` | `100.64.0.3` | Tailnet IP of the target host |
| `LINKS_POSTGRES_PORT` | `5432` | Port the target listens on |
| `LINKS_POSTGRES_URL` | `http://100.64.0.3:5432` | Full connection URL |

**Rules:**
- Links are **one-directional**: the linking service gets env vars, the target must declare its host binding (see below)
- **Same-region links are rejected** -- use `depends_on` for same-region dependencies
- Target must exist in `services:` or `accessories:` in services.yml
- Target must be deployed in the specified region (via `regions:` field, or no filter = everywhere)
- **The link target must declare host exposure**:
  - For an accessory target: `expose: tailnet` at the top level (alongside `port:`)
  - For a service target: `ports.expose: tailnet` (nested inside the `ports:` block)
  - Without it, the target has no host-port binding on its region's host. Pre-deploy validation (`bin/bay validate`) rejects this configuration.

> **Migration note (framework v0.84.0+):** Previously the framework auto-rewrote a link target's port from `127.0.0.1:` to `0.0.0.0:`. A later change severed that rewrite to make exposure declarative; a subsequent fix closed the resulting gap by adding `ports.expose:` for services and fixing cross-region port resolution. If you have an accessory or service that's a cross-region link target, declare `expose: tailnet` (or `ports.expose: tailnet` for services) explicitly.

> **Security note:** `expose: tailnet` (or `ports.expose: tailnet`) binds the target's port directly on the headscale tailnet interface. For a service, this **bypasses Traefik** -- no TLS, no IPAllowList, no middleware. The tailnet itself is the access boundary. Confirm the consumer authenticates appropriately (e.g. an internal API token in the request body).

> **Need trusted HTTPS over the tailnet?** `expose: tailnet` is plaintext (the tunnel is the boundary). To serve a tailnet service with a real, browser-trusted cert — e.g. a PWA / Web Push that requires a secure context — see [docs/tailnet-ingress.md](tailnet-ingress.md). It terminates TLS on the control host via a DNS-01 wildcard cert and can front services that live on **other** tailnet nodes via the `tailnet_proxies:` key.

See [docs/multi-region.md](multi-region.md) for the full multi-region setup pattern.

## Gotchas

### Accessories and `links:` — ports get bound publicly

- **`links:` rewrites accessory host IP from `127.0.0.1:` to `0.0.0.0:`** — The `container_base` macro in `roles/deploy_stack/templates/_macros.j2` detects when an accessory is referenced as a `links:` target by any service and strips the localhost prefix from the accessory's `port:` so cross-host access works. This means **an accessory written as `port: "127.0.0.1:6379:6379"` will render as `0.0.0.0:6379:6379`** and end up publicly exposed on the host — contrary to what the config literally says.
- **Don't add `links: <accessory>:` unless cross-host access is genuinely required** — Services and accessories on the same compose stack share the `services` docker network and can reach each other via container name (e.g., `redis:6379` from inside the gatus container). `links:` is only needed when an external host or a container outside this stack needs to reach the accessory.
- **If you truly need cross-host access**, don't expose to the internet: put the stack behind VPN (`access: vpn`), or bind to a tailnet IP via `ansible_host`/headscale instead of `0.0.0.0`. A public 0.0.0.0 bind on databases/caches is almost always wrong.
- **Incident** — 2026-04-22: sandbox's `redis:7-alpine` accessory was bound to `0.0.0.0:6379` on 203.0.113.13 because the gatus service had a stale `links: redis:` entry (gatus uses sqlite, never actually needed redis). Removed both the link and the accessory.

### Supervisor pattern (nginx + app in one container)

Many app images run nginx as PID 1 with the app process (Node, Python, Ruby) supervised under it (s6-overlay, supervisord, foreman, dumb-init+wrapper). When the inner process dies, **nginx stays alive and Docker reports the container as healthy** — the restart policy only fires when PID 1 exits. From the outside, `docker ps` is green, `bay healthcheck` probing `/` gets a 2xx from nginx's static or cached response, and the service is silently dead from the user's perspective.

Docker's per-container `healthcheck:` block helps when set, but most upstream images either don't define one or define one that exec's a shallow check (`curl -f http://127.0.0.1/ || exit 1`) which hits the same nginx that masks the failure. The `bin/bay healthcheck` probe runs from outside via HTTPS and is the canonical "is the user-visible URL serving" check — but only if it probes a path the inner process actually owns.

The fix is `healthcheck_path:` in `services.yml` (framework v0.84.0+):

```yaml
services:
  myapp:
    access: public
    image: registry.example.com/myapp:latest
    domains:
      - 'myapp.example.com'
    ports:
      internal: 80
    # Probe a route nginx proxies to the app — `/` returns nginx's static
    # front even when the app is dead.
    healthcheck_path: /healthcheck
```

**Rule: any new service where an inner process handles requests but is not PID 1 MUST declare `healthcheck_path` pointing to a route the inner process owns.** Without it, `bin/bay healthcheck` cannot detect app-level death.

**Incident** — 2026-04-22: a demo deploy audit reported "all services healthy" while two storefront locale Node backends had silently died inside their nginx supervisors. Probing `/healthcheck` (proxied to Node on :3001) would have surfaced the failure as a 502; probing `/` returned 200 from nginx. This triggered the addition of the `healthcheck_path:` schema field, probe wiring, output formatting, and migration of every nginx+Node service in demo.

#### Cold starts and the readiness window

`bin/bay healthcheck` retries a failing probe inside a **wall-clock budget**,
and the size of that budget depends on *how* the probe failed:

| Probe outcome | Reading | Budget |
|---|---|---|
| Connection refused / timeout, or `502`/`503`/`504` | Traefik is up, the upstream isn't accepting connections yet — a container still booting | 90s (`health_check_timeout`) |
| Any other status (`4xx`, `500`), or a TLS error | The app answered, or the connection failed in a specific way — waiting rarely changes it, but a recreate briefly drops the Traefik router (→ `404`), so a short retry stays | 10s |
| Hostname does not resolve | No amount of waiting invents a DNS record | none — fails immediately |

Backoff is 2s → 4s → 8s → 10s → 10s… A **healthy service passes on attempt 1
and costs nothing**, so a green deploy is not slowed down. A service that is
genuinely dead is still reported RED, within ~90s.

A service that needed the window prints how much of it it used, e.g.
`200 OK [pass] (ready after 44s, 7 attempts)`. Watch that number: the window is
wide enough to absorb a cold start silently, and this is what stops a service
creeping toward the ceiling from reading as instantly-green until the day it
tips over.

**Slower than 90s?** Raise the service's `health_check_timeout:` — the same key
`rebuild.sh` uses for its post-restart health poll (see `docs/build-pipeline.md`).
It can only *widen* the probe window; a value below 90 is ignored so the probe
never becomes stricter than the framework default.

**Incident** — 2026-07-27: a deploy recreated a Node/TypeScript service and the
post-deploy healthcheck reported `✗ https://beta.<redacted>/health 502 [FAIL]`.
The service was not broken — one minute later it served 200 on 6/6 probes with
`RestartCount=0`, and the next region's deploy passed the identical URL. The
old window was 3 attempts × 5s ≈ 10s, far short of the container's cold start.
The damage from a false RED is not the wasted minute; it is that it teaches the
operator to wave off healthcheck failures, so the next genuine one gets
dismissed too.

### Traefik (v3.6+ with host networking)

These are framework-level invariants — services don't usually need to think about them, but they explain "why is Traefik configured this way" when you read the role.

- **`external: true` for compose networks** — The `services` network is created by the Traefik role via `docker network create`. The compose template must declare it as `external: true`, not let Compose try to manage it.
- **Dummy port label on host-networked container** — Traefik's own container uses `network_mode: host` with `traefik.enable=true` (for global middleware labels). Without a `traefik.http.services.traefik-noop.loadbalancer.server.port=0` label, the "port is missing" error can block the entire Docker provider.
- **Unhealthy containers are filtered** — Traefik v3.6 silently skips containers with failing Docker healthchecks. Scratch-based images (like gatus) have no `wget`/`curl`, so the generated `wget --spider` healthcheck always fails. **Do not add a `healthcheck` block to scratch-based services in `services.yml`** — omit it entirely. The `render_healthcheck` macro adds `interval`/`timeout`/`retries` fields alongside `test`, which can interfere with `["NONE"]` disable syntax.
- **Docker API version compatibility** — Docker Engine v29.x requires API v1.44+. Traefik images older than v3.3 ship with a Docker SDK that negotiates at v1.24, which is rejected.
- **Traefik's internal entrypoint (:8080) conflicts with CrowdSec** — Traefik auto-creates an internal `traefik` entrypoint at `:8080` when `ping: {}`, `api: {}`, or `metrics: {}` is configured without an explicit entrypoint. CrowdSec LAPI listens on `127.0.0.1:8080`. Adding features that trigger the internal entrypoint will crash-loop Traefik on every consumer. If Traefik ever needs a ping/healthcheck endpoint, define it on an explicit entrypoint at a non-conflicting port.
- **Port-binding self-healing (v0.61.3+)** — `deploy_infra.yml` checks port 80 before deploying Traefik. If the container exists but port 80 isn't bound (crash loop, stale state), it removes the container so `docker_container state: started` recreates it fresh. `bay-infra-ensure.sh` does the same at boot time (waits 30s, recreates if not bound).
- **`--tags deploy_stack` includes config-rendering roles (v0.61.4+)** — The `traefik`, `watchtower`, and `access_gateway` roles carry the `deploy_stack` tag so their config files are always re-rendered before containers are (re)created. Running `--tags traefik` alone only updates config — it does NOT recreate the container (that's `deploy_stack`/`container_lifecycle`).

### Ansible / Jinja2

- **Use `.get()` for optional dict keys: `env.clear` / `env.secret` / `svc.update` / `acc.update`** — Use `item.value.env.get('clear', {})`, `env.get('secret', [])`, `svc.get('update', 'monitor')`, `acc.get('update', 'monitor')`. Dot notation resolves to Python dict methods (`.clear()`, `.update()`), not the YAML key — that's the original trap. Bracket notation (`env['clear']`) is **not enough**: when the key is absent, `dict['key']` raises KeyError, and Jinja2 silently falls back to `getattr(env, 'clear')`, which returns the bound `.clear()` method. `| default({})` does NOT save you — the value is technically defined (it's a callable), so `default` never fires, and the template crashes downstream with `object of type 'builtin_function_or_method' has no attribute 'items'`. `.get('clear', {})` calls the dict's real `.get` method, never triggers attribute fallback, and returns the explicit default on missing keys. Existence checks (`'clear' in env`) are also correct but more verbose — prefer `.get()`. Same applies to `svc['update']` / `acc['update']` when the `update:` key is absent — Watchtower labels silently default to `monitor` today by accident, but the pattern is fragile. Fixed framework-side in v0.85.3; see `roles/deploy_stack/tasks/main.yml:89,107` for the canonical pattern.
- **`playbook_dir` resolves to `.bay/`** — `import_playbook: .bay/deploy.yml` changes `playbook_dir` to the framework directory. To reference consumer files (e.g., `files/`), use `playbook_dir | dirname`.
- **Role defaults unavailable in pre-role plays** — The bootstrap play in `deploy.yml` runs before roles are loaded, so role defaults like `traefik_acme_path` are undefined there. Inline defaults with `| default()`.
- **Deploy lock must use `block/always`** — If a task fails after lock acquisition, linear execution skips the cleanup task. All post-lock tasks go inside `block:`, lock release goes in `always:`.
