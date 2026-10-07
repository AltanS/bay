<!-- M118 spec 05: bullets for the 2.3.0 entry of CHANGELOG.md. Fold in, then delete this file. -->

### Added

- The deploy receipt lists the tailnet routes the box serves. `bay_reconcile.receipt` reads the
  route file the traefik role rendered (`dynamic/tailnet-proxies.yml` in the stack directory) and
  writes it as `routes`: one `{name, domains, upstream, pass_host_header, identity_inject,
  entrypoint}` per route, `[]` on a box with no route file. The role passes `stack_dir` in the
  receipt meta. The field is additive: `receipt_version` stays 1 and `status_version` stays 2.
  `status.schema.json` declares it. `bay show --routes` now has a RUNNING column to compare.
- `bay status --env <env>` (no `--json`) prints one line per box of that env: the result, the deploy
  time, the container count and, for a receipt that lists routes, the route count. Plain
  `bay status` still reads no box. `--no-remote` skips the read.
- Route-only plan and up. When no project has `[deploy.<env>]` and `<env>` is the box env of
  `[tailnet] ingress_box`, `bay plan <env>` compiles the whole fleet at its pins and shows the route
  steps (and any other compile difference). The plan has no project and the note
  `route-only plan for <env>`. `bay up <env>` writes `services.yml`, commits `bay: up <env> (routes)`,
  deploys `<env>` (with `headscale,traefik` on a route step), reads the receipts and pushes the
  fleet. It pins nothing and writes no lock. A plan with zero steps still deploys. A step that
  belongs to a project blocks a route-only plan, because nothing would pin it. Any other env with
  no project keeps the old note and refusal.
- `bay validate` checks that `[tailnet] ingress_box` is the Headscale host: `access_gateway` must be
  `headscale`, and with `headscale_control_region` set, the ingress box's region (`region` from
  `group_vars/<box group>/`, else the box group, else the box name) must equal it.
- `bay validate` checks the boxes against the inventory: the box env needs a host or a group of
  that name in `hosts/<box env>` (error, with the `[<box env>:children]` hint); a box `group` must be
  a group there (error); a box name that is neither a host nor a group there is a warning.

### Changed

- One `access_gateway` default. `config.access_gateway_type()` returns the value of
  `group_vars/all/access_gateway.yml`, else the value in `roles/access_gateway/defaults/main.yml` of
  the framework checkout (`wireguard`). `bay doctor`, `bay gateway`, `bay region` and `bay validate`
  use it. Before, `bay doctor` read a fleet with no `access_gateway.yml` as `none`, and
  `bay gateway` assumed `headscale` in one place.
- `bay doctor`: with `wireguard`, an empty `vpn_allowed_ips` fails only when a service uses
  `access: vpn`. A fleet that serves everything public needs no list.

### Upgrade notes

- The first `bay up` (or `bay deploy`) of each box env after 2.3.0 ships the new `bay_reconcile`
  and rewrites that env's receipt with `routes`. Until then `bay show --routes` reports RUNNING as
  `unknown` and `bay status --json` shows no `routes`.
- A `bay up <ingress box env>` with no project on that env now deploys (route-only) instead of
  stopping with "nothing to deploy". Read its plan first: it deploys the whole box environment.
- `bay validate` (and the pre-deploy gate of `bay deploy` and `bay up`) can now fail on a fleet
  whose `ingress_box` is not the Headscale host, whose hosts file has no group named after the box
  env, or whose box `group` is missing from the hosts file. Fix the fleet file or the hosts file.
- `bay doctor` on a fleet with no `group_vars/all/access_gateway.yml` now reports `wireguard`
  (what the boxes deploy), not "no access gateway".
