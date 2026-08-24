# Tailnet Naming & Authorization Classes

How to name things on a self-hosted Headscale tailnet, and when a name should be
replaced by a **tag**. This is the companion to
[Tailnet HTTPS Ingress](tailnet-ingress.md): that document covers certs, routing
and the ACL that locks an upstream down; this one covers the identifiers those
rules are written in terms of, and the failure modes that come from confusing
them.

Everything here assumes a **default-deny** tailnet — `headscale_acl_policy` is
defined. On an allow-all tailnet naming is cosmetic. Under default-deny, a name is
the only thing standing between a node and being unreachable.

## The three name layers

A node on a Headscale tailnet carries three independent identifiers. They are
created at different times, by different commands, and mixing them up is the most
common source of a policy that validates cleanly and grants nothing.

| Layer | Created by | Answers | Used by |
|---|---|---|---|
| **user** | `gateway enroll --user <n>` | *Who owns this?* | `tagOwners`, ACL `groups`, headscale bookkeeping |
| **node `given_name`** | join time (`--hostname`, or the device's own) | *Which machine is this?* | `nodes list`, MagicDNS, the injected identity header |
| **`hosts:` alias** | the ACL policy, by hand | *What do I call this CIDR in a rule?* | `src:` / `dst:` in `acls:` |

They are not synonyms and nothing keeps them in sync. A node can be named
`app-eu`, owned by user `alice`, and referred to in the policy as `eu` — all three
at once, all three correct.

### Users are people

**One human = one user.** A user is an ownership principal, not a machine slot; do
not create a user per device. `alice` owns `alice-laptop` and `alice-phone`. When a
user is referenced in `tagOwners` or in an ACL `group`, it must be written in the
trailing-at form — `alice@`, not `alice`. See
[tagOwners gotchas](#tagowners-and-groups-gotchas).

Non-human classes are the exception that proves the rule: a fleet of CI runners
has no human owner, which is exactly the signal that it wants a **tag**, not a
user per box.

### Machines are named by `given_name`

`given_name` is what `headscale nodes list` shows, what MagicDNS resolves, and
what appears in a `tailscale status` line. Pin it at enrollment with `--hostname`
so the tailnet's naming is yours rather than whatever the laptop happens to call
itself:

```bash
bin/bay gateway enroll --user alice --hostname alice-laptop
```

Pick names that read as a class when they belong to one — `ci-runner-1`,
`ci-runner-2`, `app-eu`, `app-na`. Convention here is not decoration: it's what
makes a later migration to a tag obvious rather than archaeological.

### `hosts:` aliases are labeled CIDRs

A `hosts:` entry is a comment with teeth — a human-readable label bound to a `/32`:

```yaml
headscale_acl_policy:
  hosts:
    laptop: 100.64.0.3/32
    phone: 100.64.0.4/32
    infra: 100.64.0.5/32
```

It carries **no** authorization by itself and no link to the node object. It maps a
string to an address, nothing more — which is also its weakness: if the node's
tailnet IP is ever reassigned, the alias silently points at the wrong machine, or
at nothing.

## `given_name` is load-bearing under `tailnet_identity`

When `tailnet_identity_enabled` is on, the injected `X-Tailnet-Device` header **is**
the node's `given_name`. The name stops being a label and becomes an
authentication subject that downstream apps key their allowlists on.

Two consequences, both covered in detail in
[The downstream contract](tailnet-ingress.md#the-downstream-contract-two-obligations-bay-cannot-enforce):

- **A rename is an identity change.** Any downstream allowlist keyed on the device
  name must change in the same operation. Bay cannot propagate it, and the typical
  failure is silent — the app keeps serving the now-unrecognised device with
  reduced privileges rather than erroring.
- **Name it right at enrollment.** The cheapest moment to choose a node's identity
  is before anything depends on it. On an identity-injecting tailnet, treat
  `--hostname` as a schema decision, not a nicety.

## Tags are authorization classes, not names

A tag (`tag:agent`, `tag:prod-app`) does not name a node — it declares what kind of
node it is. Rules written against a tag grant the **whole class**, and a node joins
the class by carrying the tag, with no policy edit.

### When a tag is right

- **The set grows.** Agent boxes, CI runners, app VPSes per region — anything where
  "we'll add another one next month" is true. Adding node number seven should not
  require an ACL edit, a deploy, and a review.
- **The members are interchangeable.** Every member wants the same grants. If you
  find yourself wanting a carve-out for one member, the set is not a class (see the
  [hybrid pattern](#the-hybrid-pattern-recommended-default)).
- **Onboarding should be one command.** A tag-stamped pre-auth key means the node is
  authorized from its first netmap — see [Onboarding flows](#onboarding-flows).

### When a `hosts:` alias is right

- **Singletons.** One laptop, one phone, one workstation. A class of one is a name
  wearing a costume.
- **Anything the audit must cover.** `bin/bay gateway acl audit` reasons about
  reachability per node; a `hosts:`-named node is individually accounted for. A tag
  resolves to whichever nodes currently carry it, which is a weaker statement about
  any *particular* node.
- **Nodes with per-node carve-outs.** Anything with an identity-protected port, or
  any port deliberately excluded from a broader range, needs its own `dst` rules —
  a class rule cannot express the exclusion (see below).
- **The anti-lockout anchor.** The control host's reachability must **never** depend
  on a tag having been applied correctly. Keep the anchor rule — the one that lets
  every node reach the control/ingress hosts — written against `hosts:` aliases:

  ```yaml
  # anti-lockout: this must not be expressible as "if the tag stuck"
  - { action: accept, src: ["*"], dst: ["eu:*", "na:*", "infra:*"] }
  ```

  A mistyped or unapplied tag on a class rule costs you one node. The same mistake
  on the anchor rule costs you the tailnet, including the path you would use to fix
  it. Tags are a convenience; the anchor is a guarantee, and guarantees do not get
  indirection.

## The hybrid pattern (recommended default)

**Tag the classes; keep aliases for the singletons and the anchor.**

```yaml
headscale_acl_policy:
  tagOwners:
    "tag:agent":    ["alice@"]     # note the trailing @ — these are USERS
    "tag:prod-app": ["alice@"]
  hosts:
    laptop: 100.64.0.3/32
    phone:  100.64.0.4/32
    infra:  100.64.0.5/32          # control + ingress host
  acls:
    # anti-lockout anchor — aliases only, never a tag
    - { action: accept, src: ["*"], dst: ["infra:*"] }

    # class rules — a new agent box joins by carrying the tag, no edit here
    - { action: accept, src: ["tag:agent"], dst: ["tag:prod-app:443"] }
    - { action: accept, src: ["laptop", "phone"], dst: ["tag:agent:22"] }

    # per-node carve-out — cannot live on a class dst
    - { action: accept, src: ["infra"], dst: ["laptop:8787"] }
```

Why this rather than either extreme:

- **Per-node aliases alone don't scale.** Every new box is a policy edit plus a
  deploy plus a review, on a set whose whole point is that it grows. The edits are
  mechanical, which is exactly when they get made carelessly.
- **A full-tag migration costs auditability.** Once the singletons are inside a
  class, the audit can tell you the *class* is reachable, but the question you
  actually ask at 2am — "can my laptop reach this?" — no longer has a rule with your
  laptop's name in it.
- **A class `dst` cannot express a per-node exception.** Tailscale/Headscale ACLs are
  **accept-only**: there is no deny rule, so `tag:agent:*` minus one port on one node
  is unsayable. Granting a class grants every member. A node with an
  identity-protected port must therefore keep its own `dst` rules and stay out of the
  class on that side — see
  [Adding a proxy under default-deny](tailnet-ingress.md#adding-a-proxy-under-default-deny)
  for the same trap in its port-range form.

A node may legitimately appear both ways: carrying `tag:agent` for the class grants
it shares, and named in `hosts:` for the one port it does not.

## Onboarding flows

### Class device — key-stamped tag (preferred)

```bash
bin/bay gateway enroll --user ci-runner-1 --tag tag:agent --expiry 24h
```

The tag is stamped on the **pre-auth key**, so the node joins already tagged and
class rules apply from its first netmap — no policy edit, no post-join command, no
window of exposure. Ownership of a tagged node moves to the synthetic
`tagged-devices` user in headscale's own bookkeeping; that is expected and applies
to key-stamped and force-applied tags alike.

`--tag` only helps if rules naming that tag **already exist**. A tag no rule names
is as dead on arrival as no grant at all.

### Repair path — force a tag after join

For a node that already joined untagged:

```bash
headscale nodes tag -i <node-id> -t tag:agent
```

Same end state, same ownership move to `tagged-devices` — but there is a window in
which the node is online and ungranted. Treat it as remediation, not as the normal
path.

### Zero-node tags are inert

A tag that no live node carries grants nothing. The policy is syntactically
perfect, `policy check` passes, the deploy is clean, and access goes to no one.
`acl audit` names the tag explicitly:

```
tag:agent matches no node — rules granting it are inert
```

`--json` reports the same set in `inert_tags`. A typo'd tag in a rule surfaces here
rather than as silence.

## Verification

Three checks, in order — they answer different questions and none substitutes for
another.

1. **`bin/bay gateway acl audit`** — resolves `tag:` targets against the live node
   list, on both sides of a rule, and flags nodes no accept rule can reach plus any
   inert tags. Two limits to hold in mind: it is **dst-side only** (a half-listed
   node that can be reached but cannot initiate reads as `reachable`), and it
   reflects **live state** — a tag resolves to whoever carries it right now, so an
   audit run before the node joins says something different from one run after.

2. **Absence from `tailscale status`.** Under default-deny an ungranted peer is not
   distributed as a peer at all, so it is **absent entirely** from other nodes'
   status output. A policy gap and a failed enrollment look identical. Check the
   policy before debugging the connection.

3. **Peer probe, from a peer.** Never probe from the host that serves the port — a
   request originating there never crosses the packet filter and always succeeds.
   From a second tailnet device:

   ```bash
   timeout 5 bash -c '</dev/tcp/100.64.0.9/22'; echo "exit=$?"
   ```

   - **exit 124** — timed out: the ACL is **denying** the flow (accept-only ACLs
     drop, they never refuse).
   - **exit 1** — connection refused: the ACL **allows** it and nothing is listening.
   - **exit 0** — allowed and open.

   Remember rules are **directional**. Verifying `A → B` says nothing about `B → A`;
   every flow a node initiates needs its own rule with that node as `src`.

## Rollback: `git revert`, never delete the file

To back out an ACL change, **revert the commit** and redeploy:

```bash
git revert <sha>
bin/bay validate
bin/bay deploy production --tags headscale
```

Do **not** roll back by deleting `headscale_acl_policy`. Removing the policy does
not restore the previous restrictions — it switches the tailnet to **allow-all**,
which is a far larger blast radius than the change being undone. Once the ACL has
replaced host-level pins (sshd `AllowUsers` / `ListenAddress` bound to tailnet IPs),
it is the *only* source restriction left; deleting it strips that and, on an
identity-injecting tailnet, also voids the "only the ingress may reach the upstream"
guarantee that makes `X-Tailnet-Device` unforgeable. A revert returns you to a known
policy. A delete returns you to no policy.

## tagOwners and groups: gotchas

- **`tagOwners` members need the trailing `@`.** `["alice@"]`, not `["alice"]`. The
  at-form is how a user is written in policy; without it the entry does not resolve
  to the user you meant.
- **`policy check` is static.** It validates syntax and internal references — it will
  happily pass a policy naming a user or tag that does not exist on the tailnet.
  **Create the user before deploying a policy that references it**, or you ship a
  policy that checks green and grants nothing.
- **`groups` are groups of USERS, not hosts.** Every member must contain `@`. To group
  machines, reference `hosts:` aliases directly in `src`/`dst`, or give them a tag.
- **An invalid policy crash-loops headscale.** The tailnet coasts on its cached policy
  until the container actually dies, so the failure surfaces long after the deploy
  that caused it. Always `bin/bay validate` first; the role also stages and
  `policy check`s before installing (see
  [Locking the upstream](tailnet-ingress.md#locking-the-upstream-headscale-acl-headscale_acl_policy)).
- **Enrolling ≠ authorizing.** `enroll` never touches the policy. Only `--tag` against
  pre-existing class rules makes onboarding a single command.
