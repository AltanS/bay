- `bay show --routes` says `unknown`, not `drift`, for a route whose box has not deployed since the pin. The receipt must be at least as new as the commit that last changed the compiled routes, and it must list routes. The check costs two git calls for the whole table. `--json` has a new `running_current` field.
- Removing the last tailnet route now removes `dynamic/tailnet-proxies.yml` from the ingress box. Traefik drops the routers, the receipt lists no routes, and the plan step converges. Check mode predicts the removal. A fleet with routes renders the file as before.

### Upgrade notes

- A fleet that removed its last route and still has the old route file on the box: the next `bay deploy <env> --tags traefik` (or any `bay up` on the ingress box env) deletes the file.
- Nothing changes for a fleet that still has routes.
