---
# CrowdSec IDS/IPS
---

# CrowdSec IDS/IPS

CrowdSec is Bay's intrusion detection and prevention system. It reads Traefik access logs and SSH auth logs, detects attacks using scenario-based rules, and enforces bans via the nftables firewall bouncer.

## Architecture

```
Traefik access.log ──┐
                      ├──► CrowdSec Agent ──► LAPI ──► Firewall Bouncer ──► nftables
/var/log/auth.log ───┘         │                              │
                               │                         ip crowdsec table
                          Hub scenarios                   priority -10
                          Custom scenarios                input + forward
                          CAPI (community)
```

**Chain priority order:**
- `-10` — CrowdSec bouncer (`ip crowdsec` / `ip6 crowdsec6`) — drops banned IPs
- `0` — Base filter (`inet filter`) — port allowlist + default drop

The bouncer chains run BEFORE the base filter. A banned IP is dropped before the SSH/HTTP allow rules are evaluated.

## Trusted Admin IPs (`crowdsec_trusted_ips`)

The bouncer receives ~22,000 community blocklist (CAPI) decisions. If your admin IP is in a CAPI range, you'll be locked out of SSH.

**Always configure `crowdsec_trusted_ips` before provisioning CrowdSec:**

```yaml
# group_vars/all/security.yml
crowdsec_trusted_ips:
  - 203.0.113.20/32    # Company VPN exit IP
```

This adds the IP to the bouncer's `exclude_ips` config, which prevents the bouncer from adding it to nftables sets regardless of CAPI decisions.

**Important:** The nftables bypass chain approach does NOT work — in nftables, `accept` in one base chain doesn't prevent `drop` in the next chain on the same hook. Only the bouncer's `exclude_ips` is effective.

## Recovery from lockout

If you're locked out by CrowdSec:

1. **Reboot the server** (hosting provider console)
2. **Immediately SSH in** and run:
   ```bash
   sudo systemctl stop crowdsec-firewall-bouncer && sudo systemctl disable crowdsec-firewall-bouncer
   ```
3. **Re-provision** with `crowdsec_trusted_ips` set:
   ```bash
   bin/bay provision production --tags nftables,crowdsec
   ```
4. **Re-enable the bouncer** (you disabled it in step 2, and provisioning
   does not undo that):
   ```bash
   sudo systemctl enable --now crowdsec-firewall-bouncer
   ```
5. **Verify** — decisions still being made, and your IP allowlisted:
   ```bash
   sudo cscli decisions list
   sudo cscli allowlists inspect argo-inventory   # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
   sudo systemctl status crowdsec-firewall-bouncer
   ```
   Confirm your own IP does not show up in `cscli decisions list`, and that
   the bouncer service is `active (running)`, not just enabled.

## Collections

Default collections (configured in `group_vars/all/security.yml`):

| Collection | Purpose |
|------------|---------|
| `crowdsecurity/linux` | Linux system scenarios |
| `crowdsecurity/traefik` | Traefik log parser + HTTP scenarios |
| `crowdsecurity/sshd` | SSH brute-force detection |
| `crowdsecurity/base-http-scenarios` | HTTP probing, scanning, path traversal |
| `crowdsecurity/http-cve` | CVE-specific exploit detection |
| `crowdsecurity/whitelist-good-actors` | Googlebot, Bingbot, etc. allowlist |

## Custom scenarios

Bay includes 11 custom scenarios. Most are enabled by default; crawl detection is **disabled by default** due to false positive risk.

### Enabled by default

| Scenario | What it detects | Ban duration |
|----------|----------------|--------------|
| `actuator-probe` | Spring Boot `/actuator` endpoint scanning | 24h |
| `debug-fuzzing` | Debug endpoints and error parameter probing | 12h |
| `custom-bad-user-agent` | Scanner tools (ffuf, sqlmap, nikto, nuclei, etc.) | 7 days |
| `ssrf-callback` | SSRF with callback domains | 7 days |
| `encoded-attack-payload` | Double-encoded / HTML-entity payloads | 7 days |
| `xss-extended` | Event handler attributes and DOM property access | 4h |
| `cache-buster-probe` | Bot network cache-busting parameters | 7 days |
| `open-redirect-probe` | Redirect parameter fuzzing | 7 days |
| `param-stuffing` | 20+ query parameters per request | 7 days |

### Disabled by default (opt-in)

| Scenario | What it detects | Why disabled |
|----------|----------------|--------------|
| `aggressive-crawl` | Fast scraping (40+ unique paths in bucket) | Modern JS apps load 40+ asset chunks per page view, triggering false positive bans on legitimate users |
| `sustained-crawl` | Slow persistent scraping (120+ total requests) | Same false positive risk |

To enable crawl detection, set in your consumer's `group_vars/all/security.yml`:

```yaml
crowdsec_scenario_aggressive_crawl: true
crowdsec_scenario_aggressive_crawl_capacity: 80    # Increase for asset-heavy sites

# Exclude paths that generate many requests per page view
crowdsec_crawl_exclude_paths:
  - /assets/
  - /api/
  - /__manifest
```

## Log acquisition

CrowdSec reads two log sources (configured via `crowdsec_acquisition`):

```yaml
crowdsec_acquisition:
  - filenames:
      - "{{ stack_dir }}/logs/traefik/access.log"
    labels:
      type: traefik
  - filenames:
      - /var/log/auth.log
    labels:
      type: syslog
```

All HTTP traffic routes through Traefik, so the single access log covers all services. Adding per-service log acquisition is unnecessary.

### Docker datasource (app logs)

To acquire a container's own stdout/stderr — e.g. an app's authoritative verdict log — use a `source: docker` item instead of a file. This survives redeploys (the json-file path under `/var/lib/docker/containers/<id>/` changes on every container recreate, so a file path drifts):

```yaml
crowdsec_acquisition:
  - source: docker
    container_name:           # or container_name_regexp: ['^app-']
      - storefront
      - blog
    # docker_host: tcp://127.0.0.1:2375   # optional; defaults to the unix socket
    labels:
      type: bot_verify
```

The native CrowdSec agent runs as root and reads the Docker socket directly. If you lock the socket down, point `docker_host` at a read-only socket-proxy.

## Custom parsers

`crowdsec_custom_parsers` ships a local parser purely via group_vars — the parser analogue of `crowdsec_custom_scenarios`. Each item renders to `/etc/crowdsec/parsers/<stage>/custom-<name>.yaml` and the role validates it is valid YAML before reload (one bad parser file breaks loading for **all** parsers).

The primary use (bay#23): ban off an app's authoritative verdict rather than re-deriving it from raw UA strings. The app already logs a zero-false-positive marker for every spoofer (`[bot-verify] SPOOFED_GOOGLEBOT … ip=<ip>`, and never for real Google); a parser turns that line into a parsed event with `evt.Meta.source_ip`, and a `type: trigger` custom scenario bans on the single event:

```yaml
crowdsec_custom_parsers:
  - name: bot-verify-spoof
    stage: s01-parse              # s00-raw | s01-parse | s02-enrich (default s01-parse)
    body: |                       # raw parser YAML; the role injects `name:` for you
      filter: "evt.Line.Raw contains 'SPOOFED_GOOGLEBOT'"
      onsuccess: next_stage
      nodes:
        - grok:
            pattern: 'ip=%{IP:spoof_ip}'
            apply_on: message
          statics:
            - meta: source_ip
              expression: evt.Parsed.spoof_ip
            - meta: log_type
              value: bot_verify_spoof

crowdsec_custom_scenarios:
  - name: spoofed-bot
    type: trigger
    description: "Ban IPs the app flagged as spoofed Google crawlers"
    filter: "evt.Meta.log_type == 'bot_verify_spoof'"
    groupby: evt.Meta.source_ip
    labels:
      service: http
      type: spoofing
      remediation: true
```

Validate before deploy with `cscli explain --log '<sample line>' --type <acquisition label type>` — `evt.Meta.source_ip` should be populated and the trigger scenario should overflow into a decision. This replaces the brittle UA-substring `spoofed-googlebot` heuristic (no token list to maintain, no rDNS-whitelist double-negative).

## Hub data refresh (GeoLite2)

CrowdSec's `crowdsecurity/geoip-enrich` parser enriches every event with the source IP's ASN and city, so scenarios can key on `evt.Enriched.ASNumber`. The enrichment data lives in two MaxMind databases the parser ships as hub `data:` files:

- `/var/lib/crowdsec/data/GeoLite2-ASN.mmdb`
- `/var/lib/crowdsec/data/GeoLite2-City.mmdb`

**Why the vendor timer doesn't cover them.** The CrowdSec package installs a daily `crowdsec-hubupdate.timer` that runs `cscli hub upgrade` **without** `--force`. That only re-downloads an item's `data:` files when the item itself version-bumps — and `geoip-enrich` rarely bumps. So the mmdbs pin to whatever was current on provision day and rot (~4 months stale observed). ASN-keyed scenarios then silently match against stale ASN→CIDR mappings and fail open.

**Why cscli can't do it.** cscli 1.7 has **no** command that re-fetches a present-but-stale data file (verified empirically on 1.7.7): `cscli parsers upgrade <p> --force` prints `Nothing to install or remove.` and never re-downloads — even with the data file deleted — because the 1.7 plan engine keys purely on item version/taint, not data-file content. Even `remove --purge --force` + `install` only pulls *missing* files (purge leaves data files in place; install skips present ones). Fetching the declared URLs directly is the only reliable refresh path on 1.7.

**What the role installs.** A weekly systemd timer + oneshot that refreshes only the configured parsers' data files, then reloads crowdsec **only if a file's content actually changed** and `crowdsec -t -error` passes:

- `/usr/local/bin/crowdsec-data-refresh.sh` — for each configured parser, reads the parser's declared `data:` `source_url`s (from the installed hub-item yaml that `cscli parsers inspect -o json` points at via `local_path`) and fetches each one with a conditional GET (`curl -z`, If-Modified-Since against the on-disk copy). A changed file is written to a same-directory temp and `mv`'d into place atomically; crowdsec is reloaded once at the end if anything changed.
- `crowdsec-data-refresh.service` (oneshot) + `crowdsec-data-refresh.timer` (weekly).

The refresh is **surgical on purpose**: the script only touches the data files declared by the parsers in `crowdsec_data_refresh_parsers` — nothing hub-wide — so tainted hub items that carry local overrides are never disturbed.

**Upstream can lag, too.** The refresh converges every host to whatever the hub-data mirror *currently* publishes — it does not guarantee absolute freshness. The mirror itself sometimes stalls (observed 2026-07-03: both mmdbs still carried `Last-Modified: 2026-02-05`). When upstream is stale, every host is equally stale, and the weekly run is just a cheap conditional GET that transfers nothing until a new drop lands.

### Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `crowdsec_data_refresh_enabled` | `true` | Master switch for the refresh timer. |
| `crowdsec_data_refresh_calendar` | `"Sat *-*-* 05:00:00"` | systemd `OnCalendar`. GeoLite2 publishes Tue/Fri, so Saturday picks up Friday's drop; `RandomizedDelaySec=3600` adds up to 1h jitter. |
| `crowdsec_data_refresh_parsers` | `[crowdsecurity/geoip-enrich]` | Parsers whose declared `data:` files get refreshed. |

`geoip-enrich` is present transitively via the `crowdsecurity/linux` collection; if a listed parser isn't installed the script logs and skips it.

### Running it manually

```bash
# SSH to server, then:
sudo systemctl start crowdsec-data-refresh.service   # blocks until the oneshot finishes
journalctl -u crowdsec-data-refresh                  # see what refreshed / reloaded
systemctl list-timers crowdsec-data-refresh.timer    # next scheduled run
```

## IP management

### Whitelist (CrowdSec parser level)

```yaml
# Prevents CrowdSec from creating LOCAL decisions for these IPs.
# Does NOT prevent CAPI decisions — use crowdsec_trusted_ips for that.
ip_whitelist: "{{ vpn_allowed_ips + ['127.0.0.0/8'] }}"
```

### Blocklist (manual bans)

```yaml
ip_blocklist:
  - 1.2.3.4              # Single IP
  - 192.168.0.0/16        # CIDR range
```

### Viewing and managing decisions

```bash
# SSH to server, then:
sudo cscli decisions list                    # All active bans
sudo cscli decisions list -i 1.2.3.4        # Check specific IP
sudo cscli decisions delete --ip 1.2.3.4    # Unban an IP
sudo cscli decisions delete --all           # Remove all local bans
sudo cscli alerts list --limit 20           # Recent alerts
sudo cscli metrics                          # Processing stats
```

## Bouncer lifecycle

The bouncer is bound to the CrowdSec agent via systemd `PartOf=`:
- When CrowdSec restarts (OOM, update, reload), the bouncer automatically restarts
- The bouncer starts after CrowdSec (`After=crowdsec.service`)
- On fresh install, the bouncer is stopped immediately after apt to prevent a double-start race condition

## Resource usage

Typical production footprint:

| Component | CPU | Memory |
|-----------|-----|--------|
| CrowdSec agent | 0.3% | ~280 MB |
| Firewall bouncer | 0.0% | ~18 MB |
| Data directory | — | ~130 MB |
