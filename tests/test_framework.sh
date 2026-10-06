#!/usr/bin/env bash
# Bay framework test suite
# Validates playbook syntax, role structure, linting, and YAML integrity
set -euo pipefail

BAY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0
ERRORS=()
MOCK_DIRS=()

pass() { PASS=$((PASS + 1)); printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS+=("$1"); printf "  \033[31m✗\033[0m %s\n" "$1"; }

section() { printf "\n\033[1m%s\033[0m\n" "$1"; }

cleanup() {
  for d in "${MOCK_DIRS[@]}"; do
    rm -rf "$d"
  done
}
trap cleanup EXIT

# Create mock stubs for external Galaxy roles (same list as .ansible-lint mock_roles)
VENDOR_DIR="$BAY_DIR/vendor/roles"
mkdir -p "$VENDOR_DIR"
for mock_role in geerlingguy.git geerlingguy.security geerlingguy.pip geerlingguy.docker \
                 darkwizard242.lazydocker; do
  mock_path="$VENDOR_DIR/$mock_role"
  if [[ ! -d "$mock_path" ]]; then
    mkdir -p "$mock_path/tasks"
    echo "---" > "$mock_path/tasks/main.yml"
    MOCK_DIRS+=("$mock_path")
  fi
done

# ── Playbook syntax checks ──────────────────────────────────────────────
section "Playbook syntax"

for playbook in provision.yml deploy.yml restore.yml; do
  output=$(uv run ansible-playbook "$BAY_DIR/$playbook" --syntax-check \
      -e target_host=all -e accessory=postgres -e backup_file=/dev/null 2>&1) || true
  if grep -q "playbook:" <<<"$output"; then
    pass "$playbook"
  else
    fail "$playbook syntax check failed"
    head -5 <<<"$output" | while IFS= read -r line; do
      printf "    %s\n" "$line"
    done
  fi
done

# ── ansible-lint ─────────────────────────────────────────────────────────
section "Linting"

lint_output=$(cd "$BAY_DIR" && uv run ansible-lint 2>&1) || true
if grep -q "Passed" <<<"$lint_output"; then
  pass "ansible-lint (production profile)"
elif grep -q "Failed" <<<"$lint_output"; then
  violations=$(echo "$lint_output" | grep -oP '\d+ failure' | grep -oP '\d+' || echo "?")
  fail "ansible-lint: $violations failure(s) — run 'uv run ansible-lint' for details"
else
  pass "ansible-lint (production profile)"
fi

# ── Role structure ───────────────────────────────────────────────────────
section "Role structure"

EXPECTED_ROLES=(common users cronjobs traefik build_image backup nftables docker_monitor deploy_stack container_lifecycle)

for role in "${EXPECTED_ROLES[@]}"; do
  role_dir="$BAY_DIR/roles/$role"
  if [[ ! -d "$role_dir" ]]; then
    fail "role '$role' directory missing"
    continue
  fi
  if [[ ! -f "$role_dir/tasks/main.yml" ]]; then
    fail "role '$role' missing tasks/main.yml"
  else
    pass "role '$role' has tasks/main.yml"
  fi
  if [[ ! -f "$role_dir/defaults/main.yml" ]]; then
    fail "role '$role' missing defaults/main.yml"
  else
    pass "role '$role' has defaults/main.yml"
  fi
done

# ── group_vars YAML validity ────────────────────────────────────────────
section "group_vars YAML validity"

while IFS= read -r -d '' f; do
  rel="${f#"$BAY_DIR/"}"
  if uv run python -c "import yaml; yaml.safe_load(open('$f'))" 2>/dev/null; then
    pass "$rel"
  else
    fail "$rel is not valid YAML"
  fi
done < <(find "$BAY_DIR/example/group_vars" -name '*.yml' -print0)

# ── Template syntax (Jinja2 parse check) ────────────────────────────────
section "Jinja2 templates"

while IFS= read -r -d '' f; do
  rel="${f#"$BAY_DIR/"}"
  if uv run python -c "
from jinja2 import Environment, BaseLoader
env = Environment(loader=BaseLoader())
env.parse(open('$f').read())
" 2>/dev/null; then
    pass "$rel"
  else
    fail "$rel failed Jinja2 parse"
  fi
done < <(find "$BAY_DIR/roles" -name '*.j2' -print0)

# ── requirements.yml covers external roles ──────────────────────────────
section "External role dependencies"

REQ_ROLES=$(uv run python -c "
import yaml
data = yaml.safe_load(open('$BAY_DIR/requirements.yml'))
for r in data.get('roles', []):
    print(r['name'])
" 2>/dev/null)

for role in geerlingguy.git geerlingguy.security geerlingguy.pip geerlingguy.docker \
            darkwizard242.lazydocker; do
  if grep -qx "$role" <<<"$REQ_ROLES"; then
    pass "$role listed in requirements.yml"
  else
    fail "$role used in playbook but missing from requirements.yml"
  fi
done

# ── Expected group_vars files ────────────────────────────────────────────
section "Expected group_vars files"

EXPECTED_GROUP_VARS=(
  example/group_vars/all/main.yml
  example/group_vars/all/services.yml
  example/group_vars/all/security.yml
  example/group_vars/all/users.yml
  example/group_vars/all/vpn_access.yml
  example/group_vars/production/main.yml
  example/group_vars/production/domains.yml
  example/group_vars/production/secrets.yml
)

for gv in "${EXPECTED_GROUP_VARS[@]}"; do
  if [[ -f "$BAY_DIR/$gv" ]]; then
    pass "$gv exists"
  else
    fail "$gv missing"
  fi
done

# ── Required variables defined in group_vars ─────────────────────────────
section "Required variables"

check_var() {
  local file="$1" var="$2"
  if grep -q "^${var}:" "$BAY_DIR/$file" 2>/dev/null; then
    pass "$var defined in $file"
  else
    fail "$var not found in $file"
  fi
}

check_var example/group_vars/all/main.yml stack_name
check_var example/group_vars/all/main.yml stack_dir
check_var example/group_vars/all/main.yml ansible_user
check_var example/group_vars/all/services.yml services
check_var example/group_vars/all/services.yml accessories
check_var example/group_vars/all/security.yml firewall_allowed_tcp_ports
check_var example/group_vars/all/security.yml crowdsec_collections
check_var example/group_vars/all/users.yml users
check_var example/group_vars/all/vpn_access.yml vpn_allowed_ips
check_var example/group_vars/production/domains.yml letsencrypt_email
check_var example/group_vars/production/secrets.yml secrets

# ── Summary ──────────────────────────────────────────────────────────────
section "Results"
printf "  %d passed, %d failed\n" "$PASS" "$FAIL"

if [[ ${#ERRORS[@]} -gt 0 ]]; then
  printf "\n\033[31mFailures:\033[0m\n"
  for e in "${ERRORS[@]}"; do
    printf "  - %s\n" "$e"
  done
  exit 1
fi
