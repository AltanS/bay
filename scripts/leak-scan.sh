#!/usr/bin/env bash
# Identity leak scan — guards the pre-open-source scrub.
#
# The framework repo is intended to be published. Consumer names, operator
# machine names, real server IPs, and credentials belong in the consumer repos
# or the private workspace, never here. This script fails CI if any reappear.
#
# Docs must use RFC 5737 documentation ranges (192.0.2.0/24, 198.51.100.0/24,
# 203.0.113.0/24) and example.com / example.org for hostnames.
#
# History: the first version of this script silently passed everything. Its
# denylist was lowercase but it grepped case-sensitively, and every real
# occurrence was `SPRQVNTRS` / `Sprqvntrs` — so section 1 never matched
# anything from the day it was written. It also had no credential check at
# all, which is how a live CrowdSec bouncer API key sat in tests/ reported as
# "clean". Sections 3 and 4 below are allowlist-based for that reason: an
# unknown domain or secret-shaped blob must FAIL and be explicitly blessed,
# rather than relying on a denylist to have predicted it.
#
# Run locally: bash scripts/leak-scan.sh
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

SELF="scripts/leak-scan.sh"
EXCLUDES=(':!vendor' ':!.venv' ":!$SELF")
# uv.lock is machine-generated and full of legitimate hashes; scanning it for
# entropy produces nothing but noise. It is still scanned for identifiers.
NO_LOCK=("${EXCLUDES[@]}" ':!uv.lock')
status=0

fail() {
    status=1
    echo "::error::$1"
}

# ── 1. Consumer / operator identifiers ──────────────────────────────────────
# Assembled from fragments so this file does not match its own denylist.
# MUST stay case-insensitive — see the history note above.
TERMS=(
    'sprq''vntrs' 'sport''sight' 'goal''facts' 'selfhosted''world'
    'lowcarb''check' 'blue''fin' 'golden''boy' 'col''lie' 'Alt''anS'
    'thegreat''lanista' 'klara''case' 'test''lab' '\bshw\b'
    'alt''an' 'sari''sin'
)
pattern=$(IFS='|'; echo "${TERMS[*]}")

# The repo SLUG is an intentional occurrence of the org name. Strip just that
# SUBSTRING and re-test the line — dropping the whole line (the old behaviour)
# would swallow a real identifier that happened to share a line with the URL.
# NOTE: re-point this when the public repo slug is decided.
# Matches both the pre-rename and current repo slug -- old URLs still resolve
# via GitHub's redirect and remain a leak vector in historical docs/history.
#
# The `github.com` host prefix is OPTIONAL, and that is load-bearing. It used
# to be mandatory, which is why this section went red on v1.0.0: the slug also
# appears BARE, without a host, in `src/bay_cli/commands/migrate.py` — the
# migrator has to name the real old and new remotes in order to rewrite a
# consumer's `origin`, so those literals are legitimate and must be scrubbed
# too. Only the org/repo pair is stripped; a bare `SPRQVNTRS` anywhere else
# still fails — except on LICENSE's own copyright/licensor lines, where
# naming the holder is the entire point of the file. That exception listed
# only the BSL parameter keys until the 2026-08-24 switch to MIT, which
# states the holder on a `Copyright (c)` line instead.
# Both the private org slug and the public account slug are stripped before
# the identifier check. Naming the repo that hosts the project is not a leak
# — it is the clone URL every adopter needs — but a bare `SPRQVNTRS` or a
# bare account name ANYWHERE ELSE still fails, which is the point.
REPO_SLUG_RE='(github\.com[:/])?(SPRQVNTRS|AltanS)/(argo|bay)(\.git)?'  # legacy-argo: old slug still a leak vector
hits=$(git grep -IinE "$pattern" -- "${EXCLUDES[@]}" 2>/dev/null \
        | sed -E "s#${REPO_SLUG_RE}##g" \
        | grep -iE "$pattern" \
        | grep -vE '^LICENSE:[0-9]+:\s*(Copyright \(c\)|Licensor:|The Licensed Work is)')
if [ -n "$hits" ]; then
    fail "Consumer/operator identifier found in framework repo:"
    echo "$hits" >&2
fi

# ── 2. Real public IP addresses ─────────────────────────────────────────────
# Allowlist: loopback, broadcast, RFC1918, RFC5737 docs, well-known public
# resolvers, obvious placeholders, and the Hetzner provider defaults that the
# netplan role ships (documented as overridable).
#
# CGNAT/Tailscale (100.64.0.0/10) is allowlisted wholesale, deliberately. The
# docs legitimately teach the CIDR and individual node addresses, so policing
# this range by VALUE is futile — the real leak that got through review was
# `klaracase.de -> 100.64.0.7`, and the IP half of that pair is indistinguish-
# able from a documentation example. What made it a leak was the DOMAIN. That
# is section 4's job, which is why section 4 is allowlist-based.
ALLOW='^(0\.0\.0\.0|127\.|255\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.'
ALLOW+='|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.'
ALLOW+='|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|100\.100\.100\.100'
ALLOW+='|1\.1\.1\.1|8\.8\.8\.8|8\.8\.4\.4|9\.9\.9\.9'
ALLOW+='|1\.2\.3\.4|5\.6\.7\.8|9\.8\.7\.6|2\.2\.2\.2|3\.3\.3\.3|999\.'
ALLOW+='|185\.12\.64\.[12])'

bad_ips=$(git grep -IhoE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' -- "${EXCLUDES[@]}" 2>/dev/null \
            | sort -u | grep -vE "$ALLOW" || true)

if [ -n "$bad_ips" ]; then
    fail "Non-documentation IP address found — use RFC 5737 ranges in docs:"
    echo "$bad_ips" >&2
    for ip in $bad_ips; do
        git grep -In --fixed-strings "$ip" -- "${EXCLUDES[@]}" >&2
    done
fi

# ── 3. Credentials ──────────────────────────────────────────────────────────
# (a) Provider token formats, pinned to their real lengths so the repo's
#     deliberately-fake fixtures (ghp_FAKE_TOKEN, ghp_xxx, ...) do not trip it.
#     A real GitHub PAT is ghp_ + exactly 36 chars; the fakes are all shorter.
CRED_PATTERNS='ghp_[A-Za-z0-9]{36}'
CRED_PATTERNS+='|github_pat_[A-Za-z0-9_]{60,}'
CRED_PATTERNS+='|gh[osu]_[A-Za-z0-9]{36}'
CRED_PATTERNS+='|glpat-[A-Za-z0-9_-]{20}'
CRED_PATTERNS+='|AKIA[0-9A-Z]{16}'
CRED_PATTERNS+='|AIza[0-9A-Za-z_-]{35}'
CRED_PATTERNS+='|sk-[A-Za-z0-9]{32,}'
CRED_PATTERNS+='|xox[baprs]-[A-Za-z0-9-]{12,}'
CRED_PATTERNS+='|tskey-auth-[A-Za-z0-9-]{16,}'
CRED_PATTERNS+='|-----BEGIN [A-Z ]*PRIVATE KEY-----'
CRED_PATTERNS+='|\$2[aby]\$[0-9]{2}\$[A-Za-z0-9./]{53}'

if hits=$(git grep -InE "$CRED_PATTERNS" -- "${NO_LOCK[@]}" 2>/dev/null); then
    if [ -n "$hits" ]; then
        fail "Credential-shaped token found:"
        echo "$hits" >&2
    fi
fi

# (b) Bare high-entropy blobs. This is what a `cscli bouncers add` key looks
#     like: 32+ chars of base64 with upper, lower AND digits. Requiring all
#     three character classes excludes hex digests (docker image sha256s) and
#     ordinary prose, which is where the false positives live.
ent=$(git grep -IhoE '\b[A-Za-z0-9+]{32,}={0,2}\b' -- "${NO_LOCK[@]}" 2>/dev/null \
        | grep -E '[A-Z]' | grep -E '[a-z]' | grep -E '[0-9]' | sort -u || true)
if [ -n "$ent" ]; then
    fail "High-entropy string found — looks like a secret:"
    echo "$ent" >&2
    for blob in $ent; do
        git grep -In --fixed-strings "$blob" -- "${NO_LOCK[@]}" >&2
    done
fi

# ── 4. Hostnames ────────────────────────────────────────────────────────────
# Allowlist of registrable domains, NOT a denylist of shapes. The previous
# version matched a fixed `<label>.(ts|infra).<label>.<tld>` pattern, which
# could not match any of the real domains that actually leaked.
#
# Entries below are public vendor/project domains, RFC-2606 example domains,
# and placeholders. Traefik label fragments (routers.app, middlewares.app)
# and internal suffixes are here because the extraction regex cannot tell a
# label path from a hostname.
ALLOWED_DOMAINS='example\.(com|de|org|net)'
ALLOWED_DOMAINS+='|github\.com|docker\.com|google\.com|slack\.com|telegram\.org'
ALLOWED_DOMAINS+='|tailscale\.com|ts\.net|astral\.sh|readthedocs\.io'
ALLOWED_DOMAINS+='|amazonaws\.com|backblazeb2\.com|digitaloceanspaces\.com'
ALLOWED_DOMAINS+='|wasabisys\.com|your-objectstorage\.com'
ALLOWED_DOMAINS+='|stirlingpdf\.com'                # public image in a registry-detection test
ALLOWED_DOMAINS+='|evil\.io|x\.com'                 # SSRF canary / placeholder
ALLOWED_DOMAINS+='|blogco\.com|storefront\.de'      # test placeholders
ALLOWED_DOMAINS+='|tailnet\.internal|ports\.internal|internal\.local'
ALLOWED_DOMAINS+='|routers\.app|services\.app|middlewares\.app'
ALLOWED_DOMAINS+='|contributor-covenant\.org'   # CODE_OF_CONDUCT.md source
ALLOWED_DOMAINS+='|conventionalcommits\.org'    # CONTRIBUTING.md commit spec

bad_hosts=$(git grep -IhoE '\b[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)+\.(com|de|net|io|org|dev|sh|ai|me|co|cloud|app|internal|local)\b' \
              -- "${NO_LOCK[@]}" 2>/dev/null \
              | sed -E 's/.*\.([a-z0-9-]+\.(com|de|net|io|org|dev|sh|ai|me|co|cloud|app|internal|local))$/\1/' \
              | sort -u | grep -vE "^($ALLOWED_DOMAINS)$" || true)

if [ -n "$bad_hosts" ]; then
    fail "Unrecognised domain — use example.com, or add to the allowlist if public:"
    echo "$bad_hosts" >&2
fi

# ── 5. Email addresses ──────────────────────────────────────────────────────
# A systemd *instance* name is indistinguishable from an email to the regex
# above: `bay-backup@pg.timer` is `<template>@<instance>.<unit-type>`. The
# rename-migration role and its tests must quote real instance names (they
# parse `systemctl list-units` output), so this narrow, anchored exclusion
# covers exactly that form — a templated unit type after the final dot, and
# nothing else. A real address like `someone@company.com` still fails.
SYSTEMD_UNIT_RE='^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]*\.(service|timer|path|socket|mount|target)$'
bad_mail=$(git grep -IhoE '\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b' \
             -- "${NO_LOCK[@]}" 2>/dev/null \
             | grep -viE '@([a-z0-9-]+\.)*(example|test)\.(com|org|net|de)$|@github\.com$' \
             | grep -vE "$SYSTEMD_UNIT_RE" \
             | sort -u || true)
if [ -n "$bad_mail" ]; then
    fail "Real-looking email address found — use example.com:"
    echo "$bad_mail" >&2
fi

if [ "$status" -eq 0 ]; then
    echo "leak-scan: clean"
else
    echo "leak-scan: FAILED — see errors above" >&2
fi
exit "$status"
