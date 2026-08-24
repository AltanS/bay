# swap

Provisions a swapfile as an OOM safety net.

On memory-constrained servers (≤ 8 GiB), a runaway build or memory spike
can kill `sshd`, `traefik`, and `tailscale` — wedging the host before
operators can intervene. A swapfile lets the kernel degrade gracefully
under pressure instead.

This role is **provision-time** (one-time setup) — wire it into
`provision.yml`, not `deploy.yml`. It is idempotent and safe to re-run.

## Variables

| Name | Default | Notes |
|---|---|---|
| `swap_enabled` | `true` | Set `false` on hosts with network-mounted root disks (NFS, storage-backed VMs without direct block access) — a swapfile on slow remote storage hurts more than it helps. |
| `swap_size` | `"2G"` | Must use uppercase `G`/`M` suffix (systemd convention, matched by `human_to_bytes`). Changing this triggers a `swapoff` + recreate on the next run. |
| `swap_path` | `/swapfile` | |
| `swap_swappiness` | `10` | Written to `/etc/sysctl.d/99-swap.conf`. Low value because swap is a safety net, not a hot-path cache. |
| `swap_priority` | `-2` | fstab priority. Deprioritizes this swap relative to any future zram/partition-backed swap. |

## Idempotency

- Compares `stat -c %s` to `human_to_bytes(swap_size)`. If sizes match, no-op.
- On size mismatch: `swapoff` → delete file → reallocate → `mkswap` → `swapon`.
- fstab line format: `{{ swap_path }} none swap sw,pri={{ swap_priority }} 0 0`.

## Usage

Run once per host:

```bash
bin/bay provision <env> --tags swap
```

To disable on a host, set `swap_enabled: false` and re-run — the role
cleanly removes the swapfile, fstab entry, and deactivates swap.
