#!/usr/bin/env bash
# Multi-region Headscale test suite
# Validates role gating, API registration, wizard templates, and CLI support
# without requiring live servers or Ansible execution
set -euo pipefail

BAY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS + 1)); printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS+=("$1"); printf "  \033[31m✗\033[0m %s\n" "$1"; }

section() { printf "\n\033[1m%s\033[0m\n" "$1"; }

# ── Role gating (headscale_control_region → derived headscale_server) ──
section "Role gating (headscale_control_region)"

# Check derived headscale_server default
if grep -q 'headscale_control_region is not defined' "$BAY_DIR/roles/access_gateway/defaults/main.yml"; then
  pass "headscale_server derived from headscale_control_region"
else
  fail "headscale_server not derived from headscale_control_region in defaults"
fi

# Check role includes are gated
if grep -q 'headscale_server | default(true) | bool' "$BAY_DIR/roles/access_gateway/tasks/main.yml"; then
  pass "headscale role include gated with headscale_server"
else
  fail "headscale role include missing headscale_server gate"
fi

# Check tailscale_node is NOT gated
tailscale_line=$(grep -A1 "Include tailscale_node" "$BAY_DIR/roles/access_gateway/tasks/main.yml" | grep "when:" || true)
if grep -q "headscale_server" <<<"$tailscale_line"; then
  fail "tailscale_node include should NOT be gated with headscale_server"
else
  pass "tailscale_node include is ungated (runs on all headscale hosts)"
fi

# Check compose templates are gated
if grep -q 'headscale_server | default(true) | bool' "$BAY_DIR/roles/deploy_stack/templates/_headscale.j2"; then
  pass "_headscale.j2 compose fragment gated"
else
  fail "_headscale.j2 compose fragment missing headscale_server gate"
fi

# ── API registration path ────────────────────────────────────────────
section "API registration path"

register_file="$BAY_DIR/roles/tailscale_register/tasks/main.yml"

if grep -q '_needs_registration' "$register_file"; then
  pass "registration uses _needs_registration fact"
else
  fail "_needs_registration fact missing"
fi

if grep -q 'api/v1/user' "$register_file"; then
  pass "API user creation endpoint present"
else
  fail "API user creation endpoint missing"
fi

if grep -q 'api/v1/preauthkey' "$register_file"; then
  pass "API preauthkey endpoint present"
else
  fail "API preauthkey endpoint missing"
fi

if grep -q 'headscale_api_key is required' "$register_file"; then
  pass "missing API key error message present"
else
  fail "missing API key error message not found"
fi

# Verify local path still uses docker exec
if grep -q 'docker exec headscale' "$register_file"; then
  pass "local registration path preserved (docker exec)"
else
  fail "local registration path (docker exec) missing"
fi

# ── Wizard scaffold templates ─────────────────────────────────────────
section "Wizard scaffold templates"

region_tmpl="$BAY_DIR/src/bay_cli/wizard/templates/region_main.yml.j2"
gw_tmpl="$BAY_DIR/src/bay_cli/wizard/templates/access_gateway.yml.j2"
secrets_tmpl="$BAY_DIR/src/bay_cli/wizard/templates/production_secrets.yml.j2"

# Region template should NOT contain headscale_server (it's derived now)
if grep -q "headscale_server" "$region_tmpl"; then
  fail "region template should not emit headscale_server (derived from headscale_control_region)"
else
  pass "region template clean (no headscale_server)"
fi

# access_gateway template should emit headscale_control_region for multi-region
if grep -q "headscale_control_region" "$gw_tmpl"; then
  pass "access_gateway template emits headscale_control_region"
else
  fail "access_gateway template missing headscale_control_region"
fi

if grep -q "headscale_api_key" "$secrets_tmpl"; then
  pass "secrets template has headscale_api_key placeholder"
else
  fail "secrets template missing headscale_api_key placeholder"
fi

# ── Gateway CLI multi-region ──────────────────────────────────────────
section "Gateway CLI multi-region"

gateway_file="$BAY_DIR/src/bay_cli/commands/gateway.py"

if grep -q '_get_control_host' "$gateway_file"; then
  pass "gateway has _get_control_host helper"
else
  fail "gateway missing _get_control_host helper"
fi

if grep -q 'headscale_control_region' "$gateway_file"; then
  pass "gateway uses headscale_control_region for control host detection"
else
  fail "gateway missing headscale_control_region support"
fi

if grep -q '"--region"' "$gateway_file"; then
  pass "gateway has --region flag"
else
  fail "gateway missing --region flag"
fi

if grep -q '"--limit"' "$gateway_file"; then
  pass "gateway uses --limit for ansible targeting"
else
  fail "gateway missing --limit targeting"
fi

# ── Region CLI ────────────────────────────────────────────────────────
section "Region CLI"

region_file="$BAY_DIR/src/bay_cli/commands/region.py"

# region add should NOT emit headscale_server: false
if grep -q 'headscale_server.*false' "$region_file"; then
  fail "region add should not emit headscale_server: false (derived now)"
else
  pass "region add clean (no headscale_server: false)"
fi

# region add should handle headscale_control_region
if grep -q 'headscale_control_region' "$region_file"; then
  pass "region add ensures headscale_control_region is set"
else
  fail "region add missing headscale_control_region handling"
fi

# ── Documentation ─────────────────────────────────────────────────────
section "Documentation"

if grep -q "Multi-region Headscale" "$BAY_DIR/docs/access-gateways.md"; then
  pass "access-gateways.md has multi-region section"
else
  fail "access-gateways.md missing multi-region section"
fi

if grep -q "headscale_control_region" "$BAY_DIR/docs/access-gateways.md"; then
  pass "access-gateways.md documents headscale_control_region"
else
  fail "access-gateways.md missing headscale_control_region documentation"
fi

if grep -q "Headscale Access Gateway in Multi-Region" "$BAY_DIR/docs/multi-region.md"; then
  pass "multi-region.md has Headscale section"
else
  fail "multi-region.md missing Headscale section"
fi

if grep -q "headscale_control_region" "$BAY_DIR/docs/multi-region.md"; then
  pass "multi-region.md documents headscale_control_region"
else
  fail "multi-region.md missing headscale_control_region documentation"
fi

# ── Summary ──────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "  \033[32m%d passed\033[0m, \033[31m%d failed\033[0m\n" "$PASS" "$FAIL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo ""
  echo "Failures:"
  for err in "${ERRORS[@]}"; do
    printf "  \033[31m✗\033[0m %s\n" "$err"
  done
  exit 1
fi
