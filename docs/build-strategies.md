# Build Strategies

How Docker images get built, distributed, and deployed across servers.

## Choosing a Strategy

- **Do you build images externally (GitHub Actions, GitLab CI)?** Use `registry`.
- **Is the build too heavy for your app server (memory, CPU, slow builds)?** Use `remote` with a dedicated build server.
- **Single server, builds are fast?** Use `local` (the default).

All three strategies support webhook-triggered auto-deploy except `registry`, which relies on `bay deploy` or Watchtower for updates.

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

- `docker buildx build --push` exports both `:sha` and `:latest` to the
  registry in one pass. The image is **not** loaded into the build server's
  local Docker daemon, so do not expect `docker images` there to show it.
- Image built once on build server, shared across regions
- One pull signal per unique image per region (not per service)
- Deployment servers expand image ref to container restarts via `image-map.json`
- `bay deploy` also pulls remote-built images (manual recovery path)

### Registry layer cache (opt-in)

`git_deploy_registry_cache` (default `false`) makes remote builds keep their
BuildKit layer cache in the registry instead of only on the build server:

```yaml
# group_vars/all/main.yml
git_deploy_registry_cache: true
```

When enabled, the remote build gains two flags:

```
--cache-to   type=registry,ref=<image repo>:buildcache,mode=max
--cache-from type=registry,ref=<image repo>:buildcache
```

The cache ref is derived from the image repo (the `image:` value with its tag
stripped), so `registry.example.com/acme/storefront:latest` caches to
`registry.example.com/acme/storefront:buildcache`.

**Why you would turn it on.** The build server prunes its local BuildKit cache
every 6 hours (`roles/cronjobs`, `docker_prune_builder_*`). That cadence is
deliberate — an unpruned cache grows without bound. The cost is that the first
build after each prune starts from layer zero. A registry-side cache survives
the prune, so only the layers that actually changed are rebuilt. Worth enabling
when builds are layer-heavy and slow, or when the build server is disk-tight
and pruned hard.

**Prerequisites.**

- A reachable registry that accepts OCI cache manifests. The self-hosted Zot
  rig role qualifies. Some hosted registries do not — check before enabling.
- The build server must already authenticate to the registry. It does, because
  it pushes there; no extra credentials are involved.

**Cost and behaviour.**

- An extra `:buildcache` tag appears next to your image tags, roughly the size
  of one full layer set. Operators watching registry storage should expect it.
- A cold cache is the normal first run. BuildKit tolerates a missing
  `:buildcache` ref and simply builds from scratch — `--cache-from` never fails
  the build.
- The flag applies to the remote strategy only, on both the `bay deploy`
  path and the webhook auto-build path. The local strategy is unaffected.

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

Image built externally (CI/CD pipeline). Pulled during `bay deploy` only.

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
      |  bay deploy
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

## Remote builder over the tailnet

### Purpose

The build server can send builds to a BuildKit daemon on another machine. That
machine can have more CPU, more RAM or a warmer cache than the build server.
Bay uses the buildx `remote` driver for this, with mutual TLS (mTLS: both
sides prove their identity with a certificate). The connection runs over the
tailnet only.

The remote builder is optional. When it does not answer, the build uses the
local builder. Builds do not stop because the remote machine is offline.

The feature applies to every build that runs on the build server. That
includes webhook auto-builds (`rebuild.sh`) and `bay deploy` remote builds
(`remote_build.yml`). Hosts that are not the build server always use their
local builder.

### Variables

Set these in `group_vars/all`, not per host:

```yaml
# group_vars/all/main.yml
git_deploy_remote_builder_endpoint: "tcp://100.64.0.8:1234"   # empty = off (default)
git_deploy_remote_builder_name: bay-remote                    # default
git_deploy_remote_builder_probe_timeout: 5                    # seconds, default
git_deploy_remote_builder_ca: "{{ secrets.git_deploy_remote_builder_ca }}"
git_deploy_remote_builder_cert: "{{ secrets.git_deploy_remote_builder_cert }}"
git_deploy_remote_builder_key: "{{ secrets.git_deploy_remote_builder_key }}"
```

- `git_deploy_remote_builder_endpoint`: the BuildKit address on the tailnet.
  An empty value turns the feature off.
- `git_deploy_remote_builder_name`: the buildx name of the remote builder. It
  must differ from `bay_buildx_builder`.
- `git_deploy_remote_builder_probe_timeout`: how long the probe waits for an
  answer before the build uses the local builder.
- `git_deploy_remote_builder_ca`, `_cert`, `_key`: PEM strings. Keep them in
  the vault. Use lowercase keys, because they are role variables and not
  container environment variables.

When the endpoint is set, all three PEM values must be set. If one is empty,
the `git_deploy` role stops the deploy with an error.

On the build server, the role does these steps:

1. It writes the PEM files to `/opt/<stack>/.buildkit/{ca,cert,key}.pem`. The
   owner is the app user, and the mode is `0600`.
2. It registers the builder for the app user:
   `docker buildx create --name bay-remote --driver remote <endpoint> --driver-opt cacert=...,cert=...,key=...`.
   It does not boot the builder, because the remote machine can be offline
   during a deploy. If the registered endpoint is different, the role removes
   the builder and creates it again.
3. It installs `/opt/<stack>/bin/select-builder.sh`, the script that picks the
   builder for each build.

To turn the feature off, set the endpoint to `""` and deploy. The script then
always picks the local builder. The `bay-remote` registration stays, but
nothing uses it. To remove it, run `docker buildx rm bay-remote` as the app
user on the build server.

### Pairing on the remote host

Run BuildKit as a rootless container. Publish its port on the tailnet address
only, never on a public address:

```bash
docker run -d --name buildkitd --restart unless-stopped \
  --security-opt seccomp=unconfined \
  --security-opt apparmor=unconfined \
  -p 100.64.0.8:1234:1234 \
  -v /etc/buildkit/certs:/certs:ro \
  -v /etc/buildkit/buildkitd.toml:/home/user/.config/buildkit/buildkitd.toml:ro \
  -v buildkit-state:/home/user/.local/share/buildkit \
  moby/buildkit:rootless \
  --oci-worker-no-process-sandbox \
  --addr tcp://0.0.0.0:1234 \
  --tlscacert /certs/ca.pem \
  --tlscert /certs/cert.pem \
  --tlskey /certs/key.pem
```

- `--addr tcp://0.0.0.0:1234` binds inside the container. The `-p` flag
  limits the host side to the tailnet address.
- `--tlscacert` makes BuildKit require a client certificate signed by your CA.
  A client without one cannot connect.
- Use one CA for both sides. The server certificate must contain the tailnet
  IP of the remote host as a subject alternative name (SAN). The client
  certificate goes into the vault for the build server.
- The certificate files must be readable by the rootless user in the container
  (uid 1000).

Bay does not prune the remote cache. Configure garbage collection (GC) on the
remote host instead, in `buildkitd.toml`:

```toml
[worker.oci]
  gc = true
  gckeepstorage = "20GB"
```

Newer BuildKit releases also accept `reservedSpace` for the same limit. The
cronjobs prune on the build server touches only its local builders.

### ACL rule

Under a default-deny Headscale ACL, the build server needs a rule to reach the
remote BuildKit port. Rules are directional. The build server opens the
connection, so it is the `src`:

```yaml
# group_vars/all/headscale_acl.yml (fleet)
headscale_acl_policy:
  hosts:
    infra: 100.64.0.5/32       # the build server
    buildbox: 100.64.0.8/32    # the remote BuildKit host
  acls:
    - { action: accept, src: [infra], dst: ["buildbox:1234"] }
```

No rule is necessary in the other direction. Tailscale allows the return
traffic of an accepted connection. Run `bay validate`, then
`bay deploy production --tags headscale`.

### Selection and fallback

Before each build, `select-builder.sh` runs
`timeout <probe_timeout> docker buildx inspect --bootstrap bay-remote`. This
probe dials the endpoint with the mTLS files and waits for BuildKit to answer.
A plain TCP check would also pass on a wrong certificate, so Bay does not use
one.

- If the probe succeeds, the build uses `bay-remote`.
- If the probe fails or times out, the build uses the local builder. This is
  silent: there is a log line, but no alert.
- If the endpoint is empty, the build uses the local builder and the probe
  does not run.

In webhook auto-builds (`rebuild.sh`), a build that fails on the remote
builder gets one more check:

- The script runs the probe again.
- If the remote now does not answer, the remote was lost during the build.
  The script logs this, sends the `build.remote_fallback` alert, and runs the
  build again, one time, on the local builder.
- If the remote still answers, the failure comes from the build itself. The
  script does not retry.

Only the result of the last attempt counts for the circuit breaker. A
fallback is never counted as a failure. `build.remote_fallback` has the
lowest severity (`debug`) and is off by default, because the build continues.

In `bay deploy`, there is no retry. The deploy uses the builder that the
probe picked. A failed deploy-time build is visible to the operator already.

### Verify

On the build server, every build logs one `builder=` line for each attempt:

```bash
journalctl -u bay-build@<svc> --since "1 hour ago" | grep 'builder='
# [rebuild] [<corr-id>] builder=bay-remote
```

The probe writes its reason to the same journal, in one line that starts with
`select-builder:`. To run the probe by hand, as the app user:

```bash
sudo -u <app_user> /opt/<stack>/bin/select-builder.sh
```

In `bay deploy`, the output shows a `Report selected builder` task with
the same information.

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

1. **`bay deploy`** (Ansible) -- Uses the `container_lifecycle` role with `community.docker.docker_container`. Supports zero-downtime canary deploys for services with `zero_downtime: true`. This is the authoritative path.

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

# Circuit breaker status (from your fleet: bay build status)
cat /opt/<stack>/state/<service>.json

# Manual build trigger
touch /opt/<stack>/triggers/<service>.trigger

# Manual pull trigger (pull-only services)
echo pull > /opt/<stack>/triggers/<service>.trigger

# Image map (which services use which images)
cat /opt/<stack>/webhook/image-map.json
```

### Circuit breaker

Auto-builds stop after `git_deploy_cb_max_failures` consecutive failures (default: 5). While the breaker is OPEN, pushes are silently ignored by `rebuild.sh` even though the webhook keeps logging "triggered". Inspect and reset from your fleet with `bay build status` / `bay build reset` (see `bay build --help`). The state schema, alert rate-limiting, and manual fallback live in [build-pipeline.md](build-pipeline.md#circuit-breaker-state-rebuildsh).

### Health check and rollback (v0.75.0+)

After `docker run`, `rebuild.sh` polls container health before reporting success:

- **Containers with HEALTHCHECK:** waits for `healthy` / `unhealthy` status
- **Containers without:** polls `State.Running` 3 consecutive times (2s intervals) to catch crash loops

On failure, automatic rollback to `:previous` image tag. Three Telegram alert types:
- No previous image available → manual intervention required
- Rollback image also unhealthy → both images failed
- Rollback succeeded → service running on previous image

**Configuration:** `git_deploy_health_check_timeout: 30` (seconds, override in the fleet's group_vars)

### Build timeout (v0.75.0+)

Systemd kills hung builds after `git_deploy_build_timeout` seconds (default 1200 / 20 minutes). The `OnFailure=bay-build-alert@%i.service` unit sends a Telegram alert for systemd-level kills only (timeout, OOM, signal). Normal exit-code failures are handled by `rebuild.sh` itself — no double-notification.

**Alert types:** timeout, OOM-kill (`MemoryMax` exceeded), signal (external SIGKILL)

**Configuration:** `git_deploy_build_timeout: 1200` (seconds, override in the fleet's group_vars)

### Build duration (v0.75.0+)

Success notifications include a `Duration: Xm Ys` field showing wall-clock time from script start to completion.
