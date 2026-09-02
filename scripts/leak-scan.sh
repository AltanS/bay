#!/usr/bin/env bash
# Identity leak scan — guards the pre-open-source scrub.
#
# The framework repo is intended to be published. Consumer names, operator
# machine names, real server IPs, and credentials belong in the consumer repos
# or the private workspace, never here. This script fails CI if any reappear.
#
# Docs must use RFC 5737 documentation ranges (192.0.2.0/24, 198.51.100.0/24,
# 203.0.113.0/24), the RFC 3849 IPv6 documentation prefix (2001:db8::/32),
# and example.com / example.org for hostnames.
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
# Sections:
#   1. Consumer / operator identifiers (denylist, case-insensitive)
#   2. Real public IPv4 addresses (allowlist of doc/private ranges)
#   2b. Real public IPv6 addresses (allowlist of doc/private/ULA ranges;
#       filters out decimal-only false positives — timestamps, Docker
#       ip:port bindings, MAC addresses — that share the colon-group shape)
#   3. Credentials:
#      (a) provider token formats pinned to their real lengths
#      (b) bare high-entropy blobs (mixed-case + digit, base64/base64url
#          alphabet including `/ _ -`)
#      (b2) lowercase+digit blobs (32+ chars) whose Shannon entropy clears
#          LOWER_ENTROPY_MIN_BITS — (b) REQUIRES an uppercase character, so a
#          key drawn from lowercase and digits only (the shape of a great many
#          API keys, and of the lowercase half of a Telegram bot token) walked
#          straight through it
#      (c) long bare hex strings (32+ chars, case-insensitive) — catches
#          hex-only secrets that (b)'s three-character-class rule misses
#   4. Hostnames (allowlist of registrable domains, case-insensitive,
#      2-label apex and longer chains both matched)
#   5. Email addresses (allowlist + systemd instance-name exclusion)
#   6. Tracked junk (git ls-files denylist — .pyc, __pycache__/, .retry,
#      .env, .swp/.swo, *~, .DS_Store)
#
# `vendor/` is excluded ONLY from 3(b)/3(c) (entropy/hex) — vendored
# third-party content is where a legitimate high-entropy or hex blob lives,
# but an identifier or address leaking there is still a real leak, so
# sections 1, 2, 2b, 4, and 5 still scan it.
#
# Run locally: bash scripts/leak-scan.sh
#
# Optional first argument: a git REF (commit or tree) to scan INSTEAD of the
# working tree, e.g. `bash scripts/leak-scan.sh HEAD~3`. The pre-push hook
# uses it to scan every commit it is about to publish — a leak that was
# introduced and then fixed is still in the history that gets pushed.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

REF="${1:-}"

# git grep, with the REF spliced in immediately before the `--` pathspec
# separator. With no REF this is plain `git grep` over the worktree/index.
ggrep() {
    if [ -z "$REF" ]; then
        git grep "$@"
        return
    fi
    local args=() a
    for a in "$@"; do
        if [ "$a" = "--" ]; then args+=("$REF"); fi
        args+=("$a")
    done
    git grep "${args[@]}"
}

# The tracked-file LIST for section 6. With a REF that is the ref's tree, not
# the index — same reasoning as ggrep.
gls() {
    if [ -z "$REF" ]; then
        git ls-files
    else
        git ls-tree -r --name-only "$REF"
    fi
}

# When a REF is given, `git grep -n` prefixes each line with `REF:`. Strip it
# so downstream filters that anchor on `^path:line:` keep matching. `-h`
# output carries no prefix at all, so this is only needed on the -n calls.
strip_ref() {
    if [ -n "$REF" ]; then sed -e "s#^${REF}:##"; else cat; fi
}

SELF="scripts/leak-scan.sh"
# `vendor` is EXCLUDED ONLY from the entropy/hex checks (VENDOR_NO_LOCK,
# below) — vendored third-party content is where a legitimate high-entropy
# or long-hex blob is most likely to live, and it's not something this repo
# authored. It is NOT excluded from the denylist, IP/IPv6, hostname, or
# email checks: an identifier or a real address leaking into a vendored file
# is exactly as real a leak as one in our own code, so those sections still
# scan it.
EXCLUDES=(':!.venv' ":!$SELF")
VENDOR_EXCLUDES=("${EXCLUDES[@]}" ':!vendor' ':!roles/git_deploy/files/github_known_hosts')
# github_known_hosts is a file of PUBLIC ssh host keys, pinned on purpose
# (see roles/git_deploy/defaults/main.yml). Base64 public key material is
# high-entropy by construction and is not a secret, so it is excluded from
# the entropy/hex sections only — identifiers, IPs, hostnames and emails
# are still scanned there.
# uv.lock is machine-generated and full of legitimate hashes; scanning it for
# entropy produces nothing but noise. It is still scanned for identifiers.
NO_LOCK=("${EXCLUDES[@]}" ':!uv.lock')
VENDOR_NO_LOCK=("${VENDOR_EXCLUDES[@]}" ':!uv.lock')
status=0

if [ -n "$REF" ]; then
    echo "leak-scan: scanning ref $REF"
fi

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
hits=$(ggrep -IinE "$pattern" -- "${EXCLUDES[@]}" 2>/dev/null \
        | strip_ref \
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
# this range by VALUE is futile — the real leak that got through review was a
# real domain paired with a tailnet IP, and the IP half of that pair is
# indistinguishable from a documentation example. What made it a leak was the
# DOMAIN. That is section 4's job, which is why section 4 is allowlist-based.
ALLOW='^(0\.0\.0\.0|127\.|255\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.'
ALLOW+='|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.'
ALLOW+='|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|100\.100\.100\.100'
ALLOW+='|1\.1\.1\.1|8\.8\.8\.8|8\.8\.4\.4|9\.9\.9\.9'
ALLOW+='|1\.2\.3\.4|5\.6\.7\.8|9\.8\.7\.6|2\.2\.2\.2|3\.3\.3\.3|999\.'
ALLOW+='|185\.12\.64\.[12])'

bad_ips=$(ggrep -IhoE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' -- "${EXCLUDES[@]}" 2>/dev/null \
            | sort -u | grep -vE "$ALLOW" || true)

if [ -n "$bad_ips" ]; then
    fail "Non-documentation IP address found — use RFC 5737 ranges in docs:"
    echo "$bad_ips" >&2
    for ip in $bad_ips; do
        ggrep -In --fixed-strings "$ip" -- "${EXCLUDES[@]}" | strip_ref >&2
    done
fi

# ── 2b. Real public IPv6 addresses ──────────────────────────────────────────
# Case-insensitive; matches 3-8 hex groups joined by ':' (covers both full
# and "::"-compressed forms). Allowlist: unspecified/loopback (::, ::1),
# unique-local (fc00::/8, fd00::/8 — includes the Tailscale ULA range
# fd7a:115c:a1e0::/48), link-local (fe80::/10), and the RFC 3849
# documentation prefix (2001:db8::/32).
#
# The raw shape also matches decimal-only colon-separated sequences that are
# NOT addresses at all: `HH:MM:SS` timestamps, Docker `ip:hostport:port`
# bindings, and `NN:NN:NN:NN:NN:NN` MAC addresses. A real IPv6 address either
# uses "::" compression or contains at least one a-f hex letter; none of
# those three decimal shapes do, so requiring a letter or "::" separates them
# without an allowlist per timestamp/port/MAC value.
IPV6_ALLOW='^::1?$'
IPV6_ALLOW+='|^f[cd][0-9a-f]{2}:'
IPV6_ALLOW+='|^fe80:'
IPV6_ALLOW+='|^2001:0?db8:'
bad_ipv6=$(ggrep -IihoE '\b([0-9a-f]{1,4}:){2,7}[0-9a-f]{0,4}\b' -- "${EXCLUDES[@]}" 2>/dev/null \
             | tr 'A-Z' 'a-z' | sort -u | grep -E '::|[a-f]' | grep -viE "($IPV6_ALLOW)" || true)

if [ -n "$bad_ipv6" ]; then
    fail "Non-documentation IPv6 address found — use the 2001:db8::/32 documentation prefix:"
    echo "$bad_ipv6" >&2
    for ip6 in $bad_ipv6; do
        ggrep -Iin --fixed-strings "$ip6" -- "${EXCLUDES[@]}" | strip_ref >&2
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

#     Two literals in this repo's HISTORY are credential-SHAPED but carry no
#     secret: they were test fixtures written out in full before the
#     assemble-from-fragments discipline landed. They live in commits that are
#     already written and cannot be un-published without a history rewrite, so
#     they are allowlisted BY EXACT VALUE here. See LOWER_ENTROPY_FIXTURES
#     below for the other one.
#
#       -----BEGIN OPENSSH PRIVATE KEY-----   tests/test_wizard_ssh_keys.py
#         The PEM banner only. No key body ever accompanied it; a real key's
#         base64 body is caught independently by tier (b).
#
#     NEW fixtures must be ASSEMBLED AT RUNTIME from fragments (see the header
#     of tests/test_leak_scan.py) — do not grow this list.
#
#     The literal is stripped as a SUBSTRING and the line re-tested, the same
#     way section 1 handles the repo slug. Dropping the whole line would let a
#     real credential hide by sharing a line with the fixture.
CRED_FIXTURE_LITERALS='-----BEGIN OPENSSH PRIVATE KEY-----'

if hits=$(ggrep -InE "$CRED_PATTERNS" -- "${NO_LOCK[@]}" 2>/dev/null | strip_ref); then
    hits=$(printf '%s\n' "$hits" \
            | sed -E "s#${CRED_FIXTURE_LITERALS}##g" \
            | grep -E "$CRED_PATTERNS" || true)
    if [ -n "$hits" ]; then
        fail "Credential-shaped token found:"
        echo "$hits" >&2
    fi
fi

# (b) Bare high-entropy blobs. This is what a `cscli bouncers add` key looks
#     like: 32+ chars of base64 with upper, lower AND digits. Requiring all
#     three character classes excludes hex digests (docker image sha256s) and
#     ordinary prose, which is where the false positives live. `/`, `_` and
#     `-` are included in the character class too — base64url and
#     path-embedded tokens (`aB3/xY9...`) are still high-entropy secrets and
#     the plain base64 alphabet alone missed them.
#
#     Widening the class to `/` also pulls in URL paths (a mixed-case repo
#     slug plus a digit anywhere makes a long GitHub URL "high-entropy") and
#     absolute filesystem paths (GeoLite2 db paths). Those are allowlisted by
#     PATTERN below rather than by whole-file path exclusion, so the file is
#     still scanned for anything else. Pytest test-id names that embed a
#     ticket/issue number (`test_..._M90_GH_13`) are identifiers, not
#     secrets, and are excluded the same way.
ENTROPY_ALLOW='^com/AltanS/bay/blob/main/'                 # Documentation= URLs
ENTROPY_ALLOW+='|^com/integrations/BOTKEY/rooms/'           # alert_channel example webhook URL
ENTROPY_ALLOW+='|^var/lib/crowdsec/data/GeoLite2-'          # doc'd GeoLite2 db paths
ENTROPY_ALLOW+='|^test_[A-Za-z0-9_]+$'                      # pytest test-id identifiers
ENTROPY_ALLOW+='|^ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789$'  # the base64 alphabet itself (bcrypt/base64 tables in bay_filters.py)

# Extra allowances for the lowercase+digit tier (b2) only. These are values
# that clear the entropy bar but are structurally not secrets.
LOWER_ENTROPY_ALLOW='^[a-f0-9]{7,40}-'                      # short-sha-prefixed names
LOWER_ENTROPY_ALLOW+='|^sha256$'
# A test fixture, not a secret. `tests/test_leak_scan.py` used to write this
# 40-char [a-z0-9] value out in full in its docstring, as the worked example of
# the shape tier (b2) was built to catch. It is a throwaway value that never
# authenticated anything, and it sits in commits of the 0.3.0 series that are
# already written — so it is allowlisted by EXACT VALUE rather than removed by
# a history rewrite. HEAD assembles the example at runtime instead.
# NEW fixtures must be assembled at runtime; do not grow this list.
# Anchored ^...$ against a single extracted candidate, so a DIFFERENT secret on
# the same line is a separate candidate and is still reported.
LOWER_ENTROPY_ALLOW+='|^kaqs5jwr4rzaug5jlv5cvj9agu8olh4s05clrx5t$'

ent=$(ggrep -IhoE '\b[A-Za-z0-9+/_-]{32,}={0,2}\b' -- "${VENDOR_NO_LOCK[@]}" 2>/dev/null \
        | grep -E '[A-Z]' | grep -E '[a-z]' | grep -E '[0-9]' | sort -u \
        | grep -vE "($ENTROPY_ALLOW)" || true)
if [ -n "$ent" ]; then
    fail "High-entropy string found — looks like a secret:"
    echo "$ent" >&2
    for blob in $ent; do
        ggrep -In --fixed-strings "$blob" -- "${VENDOR_NO_LOCK[@]}" | strip_ref >&2
    done
fi

# (b2) Lowercase-plus-digit blobs. (b) above requires an uppercase character,
#      which is a character-class test, not an entropy measurement — a secret
#      drawn from [a-z0-9] only never trips it. That is the shape of a great
#      many API keys. Two filters, because neither works alone:
#
#      1. The alphabet is [a-z0-9] with NO separators. Entropy alone cannot
#         separate a key from a path: `etc/crowdsec/parsers/s02-enrich/...`
#         scores 3.97 and a real 32-char key scores 3.9-4.5, so the ranges
#         overlap and any threshold that catches the key drowns in file paths,
#         URLs and snake_case identifiers. Dropping `/ _ - +` from the class
#         removes that entire family, and a lowercase API key has none of them.
#      2. Shannon entropy over the candidate's own characters, so a long
#         repetitive run (`aaaa...`) is not reported. A random 32-char
#         lowercase+digit key sits between 3.9 and log2(36) = 5.17.
#
#      Pure-hex candidates are dropped here: uniform hex scores 4.0 and would
#      flood this tier with the image digests and commit SHAs that section (c)
#      below already handles with a context allowlist.
LOWER_ENTROPY_MIN_BITS="${LOWER_ENTROPY_MIN_BITS:-3.6}"
lower_cands=$(ggrep -IhoE '\b[a-z0-9]{32,}\b' -- "${VENDOR_NO_LOCK[@]}" 2>/dev/null \
        | grep -E '[a-z]' | grep -E '[0-9]' | sort -u \
        | grep -vE '^[0-9a-f]+$' \
        | grep -vE "($ENTROPY_ALLOW|$LOWER_ENTROPY_ALLOW)" || true)
lower_hits=$(printf '%s\n' "$lower_cands" | awk -v min="$LOWER_ENTROPY_MIN_BITS" '
    NF == 0 { next }
    {
        n = length($0)
        delete freq
        for (i = 1; i <= n; i++) {
            c = substr($0, i, 1)
            freq[c]++
        }
        h = 0
        for (c in freq) {
            pr = freq[c] / n
            h -= pr * log(pr) / log(2)
        }
        if (h >= min) print
    }')
if [ -n "$lower_hits" ]; then
    fail "High-entropy lowercase string found — looks like a secret:"
    echo "$lower_hits" >&2
    for blob in $lower_hits; do
        ggrep -In --fixed-strings "$blob" -- "${VENDOR_NO_LOCK[@]}" | strip_ref >&2
    done
fi

# (c) Long bare hex strings (32+ chars, case-insensitive). This catches
#     secrets encoded as hex (API keys, raw key material) that (b) misses
#     because they only use one character class ([0-9a-f]) and never trip
#     the "all three classes" requirement above.
#
#     The obvious false positives are legitimate hex: `sha256:` image
#     digests and git commit SHAs quoted in the changelog, and uv.lock's
#     `sha256 = "..."` per-package hashes — thousands of them,
#     machine-generated, already excluded from the entropy check above for
#     the same reason, so this section reuses NO_LOCK rather than
#     allowlisting each hash by value. A `-o` extraction throws away the
#     `sha256:` prefix that a value-anchored allowlist pattern would need,
#     so the digest case is allowlisted by re-checking each candidate's
#     context (`sha256:<hex>` on the same line) instead of its bare value.
#
#     `PRIVATE_ROOTS="<sha>"` and `PUBLIC_ROOTS="<sha>"` in .githooks/pre-push
#     are the same shape: git commit SHAs, not secrets. So is
#     `backup_restic_checksum` — a published upstream release digest, pinned so
#     the download fails closed. All three are allowlisted by context rather
#     than by value: the values change when a pin is bumped, so pinning them
#     here would rot.
HEX_CONTEXT_ALLOW='(sha256:|(PRIVATE|PUBLIC)_ROOTS="|backup_restic_checksum: ")'
hex_raw=$(ggrep -IihoE '\b[0-9a-f]{32,}\b' -- "${VENDOR_NO_LOCK[@]}" 2>/dev/null \
             | tr 'A-Z' 'a-z' | sort -u || true)
hex_hits=""
for hx in $hex_raw; do
    if ! ggrep -qiE "${HEX_CONTEXT_ALLOW}${hx}\b" -- "${VENDOR_NO_LOCK[@]}" 2>/dev/null; then
        hex_hits="${hex_hits}${hx}"$'\n'
    fi
done
hex_hits=$(printf '%s' "$hex_hits" | sed '/^$/d')
if [ -n "$hex_hits" ]; then
    fail "Long hex string found — looks like a secret or digest:"
    echo "$hex_hits" >&2
    for hx in $hex_hits; do
        ggrep -Iin --fixed-strings "$hx" -- "${VENDOR_NO_LOCK[@]}" | strip_ref >&2
    done
fi

# ── 4. Hostnames ────────────────────────────────────────────────────────────
# Allowlist of registrable domains, NOT a denylist of shapes. The previous
# version matched a fixed `<label>.(ts|infra).<label>.<tld>` pattern, which
# could not match any of the real domains that actually leaked.
#
# Case-insensitive, and matches 2-label apex domains (e.g. `example.com`) as
# well as longer subdomain chains — the earlier version required 3+ labels
# and could not have caught a bare apex domain.
#
# Entries below are public vendor/project domains, RFC-2606 example domains,
# and placeholders. Traefik label fragments (routers.app, middlewares.app)
# and internal suffixes are here because the extraction regex cannot tell a
# label path from a hostname. CrowdSec's bundled scenario collections
# reference SSRF/oast canary domains by name — those are also public,
# not leaks.
ALLOWED_DOMAINS='example\.(com|de|org|net)'
ALLOWED_DOMAINS+='|github\.com|docker\.com|google\.com|slack\.com|telegram\.org'
ALLOWED_DOMAINS+='|tailscale\.com|ts\.net|astral\.sh|readthedocs\.io'
ALLOWED_DOMAINS+='|amazonaws\.com|backblazeb2\.com|digitaloceanspaces\.com'
ALLOWED_DOMAINS+='|wasabisys\.com|your-objectstorage\.com'
ALLOWED_DOMAINS+='|stirlingpdf\.com'                # public image in a registry-detection test
ALLOWED_DOMAINS+='|evil\.io|evil\.com|x\.com'       # SSRF canary / placeholder
ALLOWED_DOMAINS+='|blogco\.com|storefront\.de|storefront\.com'  # test placeholders
ALLOWED_DOMAINS+='|tailnet\.internal|ports\.internal|internal\.local'
ALLOWED_DOMAINS+='|routers\.app|services\.app|middlewares\.app'
ALLOWED_DOMAINS+='|contributor-covenant\.org'   # CODE_OF_CONDUCT.md source
ALLOWED_DOMAINS+='|conventionalcommits\.org'    # CONTRIBUTING.md commit spec
ALLOWED_DOMAINS+='|docs\.docker\.com|pypi\.org|files\.pythonhosted\.org'
ALLOWED_DOMAINS+='|letsencrypt\.org|acme-v02\.api\.letsencrypt\.org'
ALLOWED_DOMAINS+='|hetzner\.com|hetzner\.cloud|cloud\.hetzner\.com'
ALLOWED_DOMAINS+='|cloudflare\.com|1\.1\.1\.1'
ALLOWED_DOMAINS+='|oast\.me|oast\.fun|oast\.live|oast\.site|oast\.online|oast\.pro'
ALLOWED_DOMAINS+='|getodin\.com|onlyscans\.com|burpcollaborator\.net'
ALLOWED_DOMAINS+='|crowdsec\.net|hub\.crowdsec\.net'
ALLOWED_DOMAINS+='|canarytokens\.com|requestbin\.net|oastify\.com|cypex\.ai'  # crowdsec SSRF/UA scenarios
ALLOWED_DOMAINS+='|bitbucket\.org|gitlab\.com|ghcr\.io|docker\.io|packagecloud\.io'  # vendor/registry
ALLOWED_DOMAINS+='|json-schema\.org|restic\.net|non-github\.com'
ALLOWED_DOMAINS+='|gatus\.io|networkgenomics\.com'  # Gatus docs / Mitogen docs (public project sites)
ALLOWED_DOMAINS+='|a\.com|app\.com|b\.com|y\.com|z\.com|test\.com'          # test fixtures
ALLOWED_DOMAINS+='|blogco\.de|wrong-domain\.com|yourdomain\.com'           # test fixtures / wizard prompt example

# TLDs are split into two tiers. `com|de|net|io|org|dev|ai|me|co|cloud` are
# unambiguously domain-shaped, so a bare 2-label apex (`example.com`) is
# matched. `sh|app|internal|local` collide constantly with this repo's own
# filenames (`rebuild.sh`) and Traefik label fragments (`routers.app`), so
# those still require 3+ labels, same as the original regex — a bare
# `rebuild.sh` is not flagged, but `evil.rebuild.sh` would be.
APEX_TLDS='com|de|net|io|org|dev|ai|me|co|cloud'
CHAIN_TLDS='sh|app|internal|local'
bad_hosts=$( { ggrep -IihoE "\\b[a-z0-9][a-z0-9-]*(\\.[a-z0-9-]+)*\\.(${APEX_TLDS})\\b" \
                -- "${NO_LOCK[@]}" 2>/dev/null; \
              ggrep -IihoE "\\b[a-z0-9][a-z0-9-]*(\\.[a-z0-9-]+)+\\.(${CHAIN_TLDS})\\b" \
                -- "${NO_LOCK[@]}" 2>/dev/null; } \
              | tr 'A-Z' 'a-z' \
              | sed -E "s/.*\\.([a-z0-9-]+\\.(${APEX_TLDS}|${CHAIN_TLDS}))\$/\\1/" \
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
# RFC 2606 reserves .test, .example, .invalid and .localhost (alongside the
# example.com/org/net/de domains already excluded above) for documentation —
# any address at those TLDs is a placeholder, not a real leak.
bad_mail=$(ggrep -IhoE '\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b' \
             -- "${NO_LOCK[@]}" 2>/dev/null \
             | grep -viE '@([a-z0-9-]+\.)*(example|test)\.(com|org|net|de)$|@github\.com$|@([a-z0-9-]+\.)*(test|example|invalid|localhost)$' \
             | grep -vE "$SYSTEMD_UNIT_RE" \
             | sort -u || true)
if [ -n "$bad_mail" ]; then
    fail "Real-looking email address found — use example.com:"
    echo "$bad_mail" >&2
fi

# ── 6. Tracked junk ──────────────────────────────────────────────────────────
# Not an identity/credential leak, but the same class of "should never have
# been committed" mistake — a stray .pyc, .env, or editor swap file. These
# only need to be checked against the tracked file LIST, not scanned by
# content, so this uses `git ls-files` rather than `git grep`.
JUNK_RE='\.pyc$|(^|/)__pycache__/|\.retry$|(^|/)\.env$|\.swp$|\.swo$|~$|(^|/)\.DS_Store$'
bad_junk=$(gls | grep -E "$JUNK_RE" || true)
if [ -n "$bad_junk" ]; then
    fail "Tracked dev-artifact file found — should not be committed:"
    echo "$bad_junk" >&2
fi

if [ "$status" -eq 0 ]; then
    echo "leak-scan: clean"
else
    echo "leak-scan: FAILED — see errors above" >&2
fi
exit "$status"
