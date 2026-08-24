# Security Policy

## Supported Versions

Only the latest tagged release of Bay is supported with security fixes.
Please upgrade to the latest release (`bin/bay update` from a consumer repo,
or check the [releases page](https://github.com/AltanS/bay/tags)) before
reporting an issue, and confirm it still reproduces there.

## Reporting a Vulnerability

Please do not open a public GitHub issue for a security vulnerability.
Public issues are visible to everyone, including potential attackers, before
a fix is available.

Instead, report it privately using one of these channels:

- **GitHub private security advisories** (the only channel) — use the
  "Report a vulnerability" option under the Security tab of the repository.
  This keeps the discussion and any fix contained until it is ready to
  disclose, and it notifies the maintainers directly.

There is deliberately no email address here. GitHub's advisory workflow is
private, authenticated, and gives you a tracked thread; an inbox gives you
none of those.

When reporting, please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce, or a proof of concept, if you have one
- The affected version(s) or commit
- Any suggested mitigation, if known

## What to Expect

- **Acknowledgement** — we aim to acknowledge a report within 72 hours.
- **Coordinated disclosure** — we aim to have a fix or mitigation ready
  within 90 days of a confirmed report. We will coordinate a disclosure
  timeline with you, and credit you in the release notes if you would like.
- We will keep you updated as we investigate and work on a fix.

## Scope

Bay is an Ansible + Python framework for provisioning internet-facing VPS
hosts. It sets up a hardened Docker host and deploys a stack including
Traefik (reverse proxy / TLS), CrowdSec (intrusion detection/prevention),
Headscale (self-hosted VPN coordination), Watchtower (image update
monitoring), and a Zot container registry.

**In scope:**

- Insecure defaults shipped by the framework itself — for example, a role
  that opens a port, weakens a firewall rule, or generates a weak secret by
  default.
- Vulnerabilities in the framework's own code (Ansible roles, the Python CLI,
  templates, generated configuration) that would let an attacker bypass
  access control, escalate privileges, or exfiltrate secrets on a host
  provisioned with Bay.

**Out of scope:**

- Misconfiguration by an operator — for example, an operator who sets
  `access: public` on a service that should be VPN-only, disables a
  firewall rule, reuses a weak vault password, or otherwise deviates from
  the framework's documented defaults and guidance.
- Vulnerabilities in third-party software Bay deploys (Traefik, CrowdSec,
  Headscale, Watchtower, Zot, the underlying OS) that are not caused by how
  Bay configures them — please report those upstream.
- Denial of service against a specific operator's infrastructure.

If you are unsure whether something is in scope, please report it anyway —
we would rather triage a borderline report than miss a real issue.
