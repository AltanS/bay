Spec 04: volume backups that cannot be cut off

- The `file` backup method (every volume backup, the Headscale state and an accessory with `method: file`) no longer pipes `docker cp` into `restic backup --stdin`. A `docker cp` that failed mid-stream closed the pipe like a finished one, and restic stored the cut-off tar as the newest snapshot before the script sent `backup.failed`. restic now runs `docker cp` itself (`restic backup --stdin-from-command`) and stores no snapshot when the command exits non-zero. The script still exits 1 and sends `backup.failed`. The snapshot file name stays `<target>.tar`, so restores are unchanged.
- The backup role stops when the restic at `backup_restic_bin` is older than 0.17.0, the first version with `--stdin-from-command`. The role still installs `backup_restic_version` (0.17.3) only when no binary is there. It does not upgrade an existing one. Under `--check` the version is not read and the check is skipped.
- A deploy waits for a running volume backup (lock `<stack_name>_<volume>.lock`) before it recreates containers, in the same task and with the same 300 s timeout as for accessory backups. Only volumes whose container runs on the box are waited for.
- Docs: backups.md describes the new command, the failure behavior and the volume lock wait.

### Upgrade notes

- The new backup script reaches a box only when the `backup` tag runs. A plain `bay up` runs `deploy_stack` only. On each environment with `backup_enabled: true`, run `bay --fleet <path> deploy <env> --tags backup` once. The deploy wait for volume locks comes with the next `bay up` or `bay deploy`.
- No restic upgrade is needed and the role does none. All boxes with backups ran restic 0.17.3 at release time. A box with a restic older than 0.17.0 stops the `backup` tag with a message: remove the binary and run the tag again, and the role installs 0.17.3.
- Do not move `backup_restic_version` to 0.19 without a look at the snapshot size check: restic 0.19.0 prints a progress line on stdout before the JSON of `restic stats --json`, and the script reads that JSON with `jq`.
