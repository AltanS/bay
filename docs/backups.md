# Backups

Bay uses [restic](https://restic.net/) for deduplicated, encrypted backups to S3-compatible storage. Each accessory with backup enabled gets its own restic repository, systemd timer, and retention policy.

## How it works

1. **Per-accessory scripts** — the backup role generates a `backup-<name>.sh` script for each accessory
2. **Stdin piping** — dump commands pipe directly into `restic backup --stdin` (no intermediate files on disk)
3. **Systemd timers** — replace cron with `Persistent=true` (catch up after downtime) and staggered scheduling
4. **Retention** — `restic forget --prune` runs after each backup to enforce retention policies
5. **Weekly maintenance** — `restic check` verifies repository integrity, `restic cache --cleanup` removes stale cache
6. **Deploy coordination** — backup scripts create lock files; deploys wait for active backups to finish before restarting containers

## Enabling backups

Backups are **disabled by default**. To enable, add `backup_enabled: true` to your `group_vars/all/main.yml`:

```yaml
backup_enabled: true
```

Without this, the backup role is a no-op — no scripts, timers, or S3 connections are created.

## Prerequisites

- `backup_enabled: true` in `group_vars/all/main.yml`
- An S3-compatible storage bucket (AWS S3, Hetzner Object Storage, Backblaze B2, MinIO, etc.)
- API credentials with object-level permissions (`PutObject`, `GetObject`, `ListBucket`, `DeleteObject`)

You create the bucket manually — Bay only needs object-level permissions, not admin/CreateBucket access.

## Setup

### 1. Enable backups

Add `backup_enabled: true` to `group_vars/all/main.yml`.

### 2. Add credentials to vault

```bash
bin/bay vault edit production
```

Add these variables inside the `secrets:` dict:

```yaml
secrets:
  # Shared S3 credentials (reused by backup role and other S3-aware services)
  S3_ENDPOINT: "s3.eu-central-1.amazonaws.com"
  S3_BUCKET: "my-bucket"
  S3_ACCESS_KEY_ID: "AKIA..."
  S3_SECRET_ACCESS_KEY: "your-secret-key"

  # Restic encryption (generate with: bin/bay secret)
  backup_restic_password: "a-strong-random-password"
```

The backup role reads `secrets.backup_s3_*` variables, which fall back to the shared `S3_*` vars automatically. You only need `secrets.backup_s3_*` overrides if backups should use different credentials or a different bucket than other services.

### 3. S3 endpoint reference

| Provider | Endpoint format | Example |
|----------|----------------|---------|
| **AWS S3** | `s3.<region>.amazonaws.com` | `s3.eu-central-1.amazonaws.com` |
| **Hetzner** | `<location>.your-objectstorage.com` | `fsn1.your-objectstorage.com` |
| **Backblaze B2** | `s3.<region>.backblazeb2.com` | `s3.us-west-004.backblazeb2.com` |
| **MinIO** | `<host>:<port>` | `minio.example.com:9000` |
| **DigitalOcean** | `<region>.digitaloceanspaces.com` | `fra1.digitaloceanspaces.com` |
| **Wasabi** | `s3.<region>.wasabisys.com` | `s3.eu-central-1.wasabisys.com` |

**AWS S3 tip:** Your endpoint is based on the bucket's region. Find it in the AWS Console under S3 > your bucket > Properties > "AWS Region". Then use `s3.<region>.amazonaws.com`. Common regions: `us-east-1`, `eu-central-1`, `eu-west-1`, `ap-southeast-1`.

### 4. Enable backup on an accessory

In `services.yml`, add `backup: true` or an explicit config:

```yaml
accessories:
  postgres:
    image: postgres:17
    backup: true              # auto-detects pg_dump from image name
    volumes:
      - postgres_data:/var/lib/postgresql/data
    env:
      clear:
        POSTGRES_USER: app
        POSTGRES_DB: app
      secret:
        - POSTGRES_PASSWORD

  redis:
    image: redis:7-alpine
    backup: true              # auto-detects redis method

  # Explicit config (when image name doesn't match)
  vectordb:
    image: pgvector/pgvector:pg18
    backup:
      method: pg_dump         # required — image doesn't contain "postgres"
      schedule: "0 4 * * *"   # override: 4 AM daily (default: 3 AM)
      retain: 60              # override: keep 60 days (default: 30)
    volumes:
      - vectordb_data:/var/lib/postgresql/data
    env:
      clear:
        POSTGRES_USER: app
        POSTGRES_DB: vectors
```

### 5. Deploy

```bash
bin/bay deploy production
```

This installs restic, initializes per-accessory repos, generates backup scripts, and enables systemd timers.

## Auto-detection

When `backup: true`, the method is detected from the image name:

| Image contains | Method | Dump command |
|---------------|--------|-------------|
| `postgres` | `pg_dump` | `docker compose exec -T <name> pg_dump -U <user> <db>` |
| `mysql` or `mariadb` | `mysql` | `docker compose exec -T <name> mysqldump -u <user> <db>` |
| `redis` | `redis` | BGSAVE + `docker compose cp <name>:/data/dump.rdb` |

If the image doesn't match any pattern and no explicit `method:` is set, the deploy **fails with an error** — it never silently falls back.

For `file` method (raw volume backup), you must set it explicitly with a `source_path`:

```yaml
backup:
  method: file
  source_path: /data
```

## Rig infrastructure: Headscale

The accessory loop above only covers things declared in `services.yml`. **Headscale**
— the tailnet coordination server — is rig infrastructure, not an accessory, so it was
historically never backed up. The backup role now captures its state automatically when
all of the following hold (no `services.yml` entry required):

- `backup_enabled: true`
- `access_gateway: headscale`
- the host is the control region (`headscale_server` is true / `region == headscale_control_region`)

What's backed up is the container's `/var/lib/headscale` (bind-mounted from
`/opt/headscale/data`): the sqlite node DB (`db.sqlite`) plus the noise and DERP private
keys (`noise_private.key`, `derp_server_private.key`). Losing these loses the tailnet
identity, every node's assigned IP, and the keys. The config dir (`/etc/headscale`,
`config.yaml` + `extra-records.json`) is re-rendered on every deploy and is intentionally
**not** backed up.

It uses the same `file` method as any volume backup (`docker cp headscale:/var/lib/headscale
| restic backup --stdin`), so it gets its own repo, systemd timer, retention, maintenance,
and Telegram alerting like every other target. Tune via `backup_headscale_*` (see Defaults),
or disable with `backup_headscale_state: false`.

```bash
bin/bay backup list headscale        # snapshots
bin/bay backup run headscale         # back up now
bin/bay backup status                # headscale appears alongside accessories
```

### Restoring Headscale (file-method restore)

`bin/bay backup restore` is database-oriented (it streams into a container and runs
pg/redis validation), so restore the `file`-method headscale snapshot manually. On the
control host, with the restic env loaded from `/opt/<stack>/backup/restic.env`:

```bash
set -a; . /opt/<stack>/backup/restic.env; set +a
export RESTIC_REPOSITORY="s3:$BACKUP_S3_ENDPOINT/$BACKUP_S3_BUCKET/$BACKUP_S3_PREFIX/$(hostname)/headscale"

restic snapshots                                   # pick the snapshot to restore
docker compose -f /opt/<stack>/docker-compose.yml stop headscale
restic dump latest headscale.tar | tar -x -C /opt/headscale/data --strip-components=1
docker compose -f /opt/<stack>/docker-compose.yml start headscale
bin/bay gateway nodes                             # confirm nodes + IPs came back
```

(`--strip-components=1` drops the leading `headscale/` directory that `docker cp` puts in
the tar so files land directly under `/opt/headscale/data`.)

## Retention

The `retain` setting controls how long snapshots are kept:

**Simple form** (integer = days):
```yaml
backup_retain: 30           # global default
# or per-accessory:
backup:
  retain: 60                # keep 60 days
```
Maps to: `restic forget --keep-within 30d --prune`

**Structured form** (fine-grained):
```yaml
backup:
  retain:
    daily: 7
    weekly: 4
    monthly: 12
```
Maps to: `restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune`

Retention runs after each successful backup. If pruning fails, the backup is still considered successful — pruning retries on the next run.

## CLI commands

`bin/bay backup --help` is the command reference (list, run, restore, status, check — each subcommand's `--help` carries examples).

## Restore

Restores stream data from restic directly into containers — no intermediate files (except Redis RDB which requires stop/copy/start).

Safety features:
- **Pre-restore backup** — automatically creates a snapshot tagged `pre-restore` before restoring
- **Confirmation required** — interactive prompt via CLI, or `-e confirm=yes` for direct ansible-playbook usage
- **Post-restore validation** — PostgreSQL checks table count > 0, Redis checks PONG response

Skip the pre-restore backup with `--skip-pre-backup` if you're restoring into an empty container.

## Monitoring

- **Telegram alerts** — backup failures, snapshot size warnings, and integrity check failures are sent via Telegram (uses the same `docker_monitor_telegram_bot_token` and `docker_monitor_telegram_chat_id` from the monitoring role)
- **Journald** — all backup output goes to `journalctl -u bay-backup@<name>`
- **Systemd timers** — check schedules with `systemctl list-timers 'bay-backup@*'`

## Repository layout

Each accessory gets its own isolated restic repository:

```
s3:<endpoint>/<bucket>/<prefix>/<hostname>/<accessory-name>/
```

The default prefix is `backups`, so a typical path looks like `s3:s3.eu-central-1.amazonaws.com/bay-ss/backups/web01/postgres/`. This keeps backup data separated from other objects in the same bucket.

This provides independent locking, independent retention, and failure isolation — a corrupted repo affects only one accessory.

## Defaults

| Variable | Default | Description |
|----------|---------|-------------|
| `backup_enabled` | `false` | Master switch — set to `true` in group_vars to enable |
| `backup_restic_version` | `0.17.3` | Restic binary version |
| `backup_s3_endpoint` | `{{ secrets.backup_s3_endpoint \| default(S3_ENDPOINT) }}` | Set in `secrets:` dict; falls back to shared `S3_ENDPOINT` var |
| `backup_s3_bucket` | `{{ secrets.backup_s3_bucket \| default(S3_BUCKET) }}` | Set in `secrets:` dict; falls back to shared `S3_BUCKET` var |
| `backup_s3_access_key_id` | `{{ secrets.backup_s3_access_key_id \| default(S3_ACCESS_KEY_ID) }}` | Set in `secrets:` dict; falls back to shared `S3_ACCESS_KEY_ID` var |
| `backup_s3_secret_access_key` | `{{ secrets.backup_s3_secret_access_key \| default(S3_SECRET_ACCESS_KEY) }}` | Set in `secrets:` dict; falls back to shared `S3_SECRET_ACCESS_KEY` var |
| `backup_s3_prefix` | `backups` | Prefix within the bucket (separates backup data from other objects) |
| `backup_schedule` | `0 3 * * *` | Default backup time (3 AM daily) |
| `backup_retain` | `30` | Default retention (30 days) |
| `backup_scripts_dir` | `/opt/<stack>/backup` | Backup scripts location |
| `backup_lock_dir` | `/opt/<stack>/backup/locks` | Lock files location |
| `backup_headscale_state` | `true` | Back up Headscale state on the control region (set `false` to disable) |
| `backup_headscale_container` | `headscale` | Container name backed up (also the restic repo/target name) |
| `backup_headscale_source_path` | `/var/lib/headscale` | In-container path captured (sqlite DB + noise/DERP keys) |
| `backup_headscale_schedule` | `{{ backup_schedule }}` | Headscale backup schedule (defaults to the global schedule) |
| `backup_headscale_retain` | `{{ backup_retain }}` | Headscale retention (defaults to the global retention) |
