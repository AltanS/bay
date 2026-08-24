# Build Strategies

How Docker images get built, distributed, and deployed across servers.

## Choosing a Strategy

- **Do you build images externally (GitHub Actions, GitLab CI)?** Use `registry`.
- **Is the build too heavy for your app server (memory, CPU, slow builds)?** Use `remote` with a dedicated build server.
- **Single server, builds are fast?** Use `local` (the default).

All three strategies support webhook-triggered auto-deploy except `registry`, which relies on `bin/bay deploy` or Watchtower for updates.

---

## Strategy: Local

Build images on the deployment server itself. Simplest setup, no registry needed.

```yaml
# services.yml
services:
  myapp:
    build:
      repo: git@github.com:user/myapp.git
      branch: main
      # strategy: local  (default, can be omitted)
    domains:
      - myapp.example.com
    ports:
      internal: 3000
```

### How it works

```
GitHub Push
    |
    v
+------------------------------------------+
|         Deployment Server                |
|                                          |
|  Webhook --> Trigger --> systemd          |
|                            |             |
|                       rebuild.sh         |
|                            |             |
|                  git pull + buildx build  |
|                            |             |
|                  docker stop/rm/run       |
|                            |             |
|                  Container live           |
+------------------------------------------+
```

- Repository cloned to `/opt/<stack>/builds/shared/<slug>/repo`
- Image tagged as `bay-<stack>-<service>:<sha>` + `:latest`
- Previous `:latest` preserved as `:previous`
- Webhook triggers build + deploy in one step
- Multiple services sharing the same repo+branch+dockerfile are deduplicated: primary builds, aliases retag

---

## Strategy: Remote

Build on a dedicated build server, push to a self-hosted OCI registry, deployment servers pull.

```yaml
# group_vars/all/main.yml
build_server: 203.0.113.14  # inventory hostname of build server

docker_registries:
  - domain: registry.infra.example.com
    username: admin
    password: "{{ secrets.REGISTRY_PASSWORD }}"

# services.yml
services:
  myapp:
    build:
      repo: git@github.com:user/myapp.git
      branch: main
      strategy: remote
    image: registry.infra.example.com/project/myapp:latest
    domains:
      - myapp.example.com
    ports:
      internal: 3000
```

### How it works

**Phase 1: Build + Push (build server)**

```
GitHub Push
    |
    v
+--------------------------------------+
|          Build Server                |
|                                      |
|  Webhook --> Trigger --> systemd      |
|                            |         |
|                       rebuild.sh     |
|                            |         |
|                  git fetch + buildx   |
|                            |         |
|                  push :sha + :latest  |
|                            |         |     +------------+
|                  push to registry ---------> Zot (OCI)  |
|                            |         |     +------------+
|  Image-level pull signals  |         |
|  (one per image per region)|         |
|                            |         |
|     POST /webhook/pull-image         |
|     {"image": "registry/.../app"}    |
|            |           |             |
+------------|-----------|-------------+
             |           |
             v           v
```

**Phase 2: Pull + Restart (deployment servers)**

```
  Pull signal arrives at /webhook/pull-image
             |           |
             v           v
+-------------+   +-------------+
|  EU Server  |   |  NA Server  |
|             |   |             |
|  app.py     |   |  app.py     |
|  looks up   |   |  looks up   |
|  image-map  |   |  image-map  |
|      |      |   |      |      |
|  Triggers   |   |  Triggers   |
|  for ALL    |   |  for ALL    |
|  services   |   |  services   |
|  using img  |   |  using img  |
|      |      |   |      |      |
|  Per-service|   |  Per-service|
|  rebuild.sh:|   |  rebuild.sh:|
|  pull, stop,|   |  pull, stop,|
|  rm, run    |   |  rm, run    |
+-------------+   +-------------+
```

- Image built once on build server, shared across regions
- One pull signal per unique image per region (not per service)
- Deployment servers expand image ref to container restarts via `image-map.json`
- `bin/bay deploy` also pulls remote-built images (manual recovery path)

### Shared images across services

Multiple services can reference the same `image:`. Only the builder service needs a `build:` block; other services are automatically detected as image consumers.

```yaml
services:
  # Builder service (has build: block)
  storefront-de:
    build:
      repo: git@github.com:user/storefront.git
      strategy: remote
      paths:
        include: ['apps/remix-storefront/**']
    image: registry.example.com/storefront:latest
    regions: [eu]

  # Image consumers (no build: block, same image)
  storefront-es:
    image: registry.example.com/storefront:latest
    regions: [eu]

  storefront-com:
    image: registry.example.com/storefront:latest
    regions: [na]
```

When `storefront-de` is pushed:
1. Build server builds and pushes `storefront:latest`
2. Sends pull signal to EU and NA (one each)
3. EU webhook looks up image-map: `storefront:latest -> [storefront-de, storefront-es]`
4. NA webhook looks up image-map: `storefront:latest -> [storefront-com]`
5. All 3 containers pull and restart independently

---

## Strategy: Registry

Image built externally (CI/CD pipeline). Pulled during `bin/bay deploy` only.

```yaml
services:
  dashboard:
    build:
      repo: git@github.com:user/dashboard.git
      strategy: registry
    image: ghcr.io/user/dashboard:latest
    domains:
      - dashboard.example.com
    ports:
      internal: 3000
```

### How it works

```
External CI/CD (GitHub Actions, etc.)
    |
    |  docker push
    v
+-----------+
| Registry  |  (Docker Hub, GHCR, etc.)
+-----+-----+
      |
      |  bin/bay deploy
      |  (docker pull)
      v
+---------------------+
|  Deployment Server  |
|                     |
|  build_image role   |
|  pulls the image    |
|                     |
|  container_lifecycle|
|  creates container  |
+---------------------+
```

- No build infrastructure deployed on servers
- No webhook auto-deploy (server has nothing to trigger)
- For auto-updates without redeploying, set `update: auto` in `services.yml` to enable Watchtower polling
- The `build:` block is required (with `strategy: registry`) so the framework knows this service has a build pipeline, even though it's external

---

## Comparison

|                        | Local              | Remote                        | Registry             |
|------------------------|--------------------|-------------------------------|----------------------|
| Build location         | Deployment server  | Dedicated build server        | External CI/CD       |
| Registry needed        | No                 | Yes (Zot/OCI)                 | External             |
| Webhook auto-deploy    | Yes                | Yes (image-level fan-out)     | No                   |
| Multi-region           | Per-server builds  | One build, fan-out to regions | Manual deploy         |
| Shared images          | Build dedup only   | Image-level pull signals      | N/A                  |
| Config required        | `build:`           | `build:` + `image:` + `build_server` | `build: {strategy: registry}` + `image:` |
| Recovery               | Re-push or `bay deploy` | `bay deploy` re-pulls  | `bay deploy`        |

## Update Mechanisms

In addition to the build strategies above, Bay supports **Watchtower** for image update detection:

- `update: auto` -- Watchtower pulls new images and restarts containers automatically (polling-based)
- `update: monitor` (default) -- Watchtower detects new images and sends Telegram alerts, but does not auto-update
- `update: false` -- Watchtower ignores the container

Watchtower is complementary to webhook auto-deploy. For `registry` strategy services without webhook infrastructure, `update: auto` provides automated updates with a polling delay.

---

## Container Creation Paths

There are two distinct paths for creating/restarting containers:

1. **`bin/bay deploy`** (Ansible) -- Uses the `container_lifecycle` role with `community.docker.docker_container`. Supports zero-downtime canary deploys for services with `zero_downtime: true`. This is the authoritative path.

2. **Webhook auto-build** (`rebuild.sh`) -- Uses `docker stop/rm/run` directly with all labels, volumes, and env baked in at deploy time. Brief downtime during restart. No canary logic.

Both paths derive container specs from the same `services.yml` source, producing identical containers. See [ADR-001](adr/001-docker-run-over-compose.md) for why `docker run` is used instead of Docker Compose.

---

## Operational Reference

### Key paths

| Path | Purpose |
|------|---------|
| `/opt/<stack>/triggers/<svc>.trigger` | Trigger file (empty = build, "pull" = pull-only) |
| `/opt/<stack>/bin/rebuild.sh` | Rendered build script (0700, owner-only) |
| `/opt/<stack>/state/<svc>.json` | Circuit breaker state |
| `/opt/<stack>/builds/shared/<slug>/repo/` | Cloned repos (local strategy) |
| `/opt/<stack>/push-builds/<svc>/repo/` | Cloned repos on build server (remote) |
| `/opt/<stack>/webhook/config.json` | Webhook service config |
| `/opt/<stack>/webhook/image-map.json` | Image-to-services mapping |
| `/opt/<stack>/env/<svc>.env` | Container env files |

### Debugging commands

```bash
# Webhook logs
docker logs bay-webhook --tail 50

# Build logs
journalctl -u bay-build@<service>.service -n 50

# Circuit breaker status (from the consumer: bin/bay build status)
cat /opt/<stack>/state/<service>.json

# Manual build trigger
touch /opt/<stack>/triggers/<service>.trigger

# Manual pull trigger (pull-only services)
echo pull > /opt/<stack>/triggers/<service>.trigger

# Image map (which services use which images)
cat /opt/<stack>/webhook/image-map.json
```

### Circuit breaker

Auto-builds stop after `git_deploy_cb_max_failures` consecutive failures (default: 5). While the breaker is OPEN, pushes are silently ignored by `rebuild.sh` even though the webhook keeps logging "triggered". Inspect and reset from the consumer with `bin/bay build status` / `bin/bay build reset` (see `bin/bay build --help`). The state schema, alert rate-limiting, and manual fallback live in [build-pipeline.md](build-pipeline.md#circuit-breaker-state-rebuildsh).

### Health check and rollback (v0.75.0+)

After `docker run`, `rebuild.sh` polls container health before reporting success:

- **Containers with HEALTHCHECK:** waits for `healthy` / `unhealthy` status
- **Containers without:** polls `State.Running` 3 consecutive times (2s intervals) to catch crash loops

On failure, automatic rollback to `:previous` image tag. Three Telegram alert types:
- No previous image available → manual intervention required
- Rollback image also unhealthy → both images failed
- Rollback succeeded → service running on previous image

**Configuration:** `git_deploy_health_check_timeout: 30` (seconds, override in consumer group_vars)

### Build timeout (v0.75.0+)

Systemd kills hung builds after `git_deploy_build_timeout` seconds (default 1200 / 20 minutes). The `OnFailure=bay-build-alert@%i.service` unit sends a Telegram alert for systemd-level kills only (timeout, OOM, signal). Normal exit-code failures are handled by `rebuild.sh` itself — no double-notification.

**Alert types:** timeout, OOM-kill (`MemoryMax` exceeded), signal (external SIGKILL)

**Configuration:** `git_deploy_build_timeout: 1200` (seconds, override in consumer group_vars)

### Build duration (v0.75.0+)

Success notifications include a `Duration: Xm Ys` field showing wall-clock time from script start to completion.
