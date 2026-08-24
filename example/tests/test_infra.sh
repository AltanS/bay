#!/usr/bin/env bash
# Consumer infrastructure test suite
# Validates structure, YAML integrity, role resolution, and playbook imports
#
# Run via: make bay:test
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BAY_DIR="$(cd "$INFRA_DIR/.bay" && pwd)"
PASS=0
FAIL=0
ERRORS=()
CLEANUP_FILES=()
CLEANUP_DIRS=()

NOTES=0

pass() { PASS=$((PASS + 1)); printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS+=("$1"); printf "  \033[31m✗\033[0m %s\n" "$1"; }
# Advisory only — never affects exit status. Used where a difference from the
# bay example is a legitimate config choice rather than a defect.
note() { NOTES=$((NOTES + 1)); printf "  \033[33m•\033[0m %s\n" "$1"; }

section() { printf "\n\033[1m%s\033[0m\n" "$1"; }

cleanup() {
  for f in "${CLEANUP_FILES[@]}"; do
    rm -f "$f"
  done
  for d in "${CLEANUP_DIRS[@]}"; do
    rm -rf "$d"
  done
}
trap cleanup EXIT

# Create temporary vault pass if needed
if [[ ! -f "$INFRA_DIR/.vault_pass" ]]; then
  echo 'test-vault-password' > "$INFRA_DIR/.vault_pass"
  CLEANUP_FILES+=("$INFRA_DIR/.vault_pass")
fi

# Create mock stubs for external Galaxy roles if vendor dir is empty
VENDOR_DIR="$BAY_DIR/vendor/roles"
mkdir -p "$VENDOR_DIR"
for mock_role in geerlingguy.git geerlingguy.security geerlingguy.pip geerlingguy.docker \
                 darkwizard242.lazydocker; do
  mock_path="$VENDOR_DIR/$mock_role"
  if [[ ! -d "$mock_path" ]]; then
    mkdir -p "$mock_path/tasks"
    echo "---" > "$mock_path/tasks/main.yml"
    CLEANUP_DIRS+=("$mock_path")
  fi
done

# ── Bay framework check ──────────────────────────────────────────────────
section "Prerequisites"

if [[ -d "$BAY_DIR" ]]; then
  pass "bay framework found at .bay/"
else
  fail "bay framework not found at .bay/"
  printf "\n\033[31mCannot continue without bay framework. Run 'make bay:setup' first.\033[0m\n"
  exit 1
fi

# ── Expected files ───────────────────────────────────────────────────────
section "Expected files"

EXPECTED_FILES=(
  ansible.cfg
  provision.yml
  deploy.yml
  restore.yml
  Makefile
  .gitignore
  README.md
  hosts/production
  group_vars/all/main.yml
  group_vars/all/services.yml
  group_vars/all/security.yml
  group_vars/all/users.yml
  group_vars/all/vpn_access.yml
  group_vars/production/main.yml
  group_vars/production/domains.yml
  group_vars/production/secrets.yml
)

for f in "${EXPECTED_FILES[@]}"; do
  if [[ -f "$INFRA_DIR/$f" ]]; then
    pass "$f"
  else
    fail "$f missing"
  fi
done

# ── ansible.cfg validation ──────────────────────────────────────────────
section "ansible.cfg"

if grep -q 'roles_path.*\.bay/' "$INFRA_DIR/ansible.cfg" 2>/dev/null; then
  pass "roles_path references .bay/"
else
  fail "roles_path does not reference .bay/"
fi

if grep -q 'inventory.*=.*hosts' "$INFRA_DIR/ansible.cfg" 2>/dev/null; then
  pass "inventory points to hosts"
else
  fail "inventory not set to hosts"
fi

if grep -q 'vault_password_file' "$INFRA_DIR/ansible.cfg" 2>/dev/null; then
  pass "vault_password_file configured"
else
  fail "vault_password_file not configured"
fi

# Verify roles_path directories exist
roles_path=$(grep 'roles_path' "$INFRA_DIR/ansible.cfg" | cut -d= -f2 | tr -d ' ')
IFS=':' read -ra ROLE_DIRS <<< "$roles_path"
for rdir in "${ROLE_DIRS[@]}"; do
  resolved="$INFRA_DIR/$rdir"
  if [[ -d "$resolved" ]]; then
    pass "roles_path dir exists: $rdir"
  else
    fail "roles_path dir missing: $rdir (resolved: $resolved)"
  fi
done

# ── Wrapper playbook structure ──────────────────────────────────────────
section "Wrapper playbooks"

for playbook in provision.yml deploy.yml restore.yml; do
  if grep -q "import_playbook:.*\.bay/$playbook" "$INFRA_DIR/$playbook" 2>/dev/null; then
    pass "$playbook imports .bay/$playbook"
  else
    fail "$playbook does not import .bay/$playbook"
  fi

  # Verify the target playbook exists in bay
  if [[ -f "$BAY_DIR/$playbook" ]]; then
    pass ".bay/$playbook exists (import target)"
  else
    fail ".bay/$playbook missing (import target)"
  fi
done

# ── group_vars YAML validity ────────────────────────────────────────────
section "group_vars YAML validity"

while IFS= read -r -d '' f; do
  rel="${f#"$INFRA_DIR/"}"
  if uv run --project "$BAY_DIR" python -c "import yaml; yaml.safe_load(open('$f'))" 2>/dev/null; then
    pass "$rel"
  else
    fail "$rel is not valid YAML"
  fi
done < <(find "$INFRA_DIR/group_vars" -name '*.yml' -print0)

# ── Required variables present in group_vars ─────────────────────────────
section "Required variables"

check_var() {
  local file="$1" var="$2"
  local filepath="$INFRA_DIR/$file"
  # Skip plaintext check for vault-encrypted files
  if head -1 "$filepath" 2>/dev/null | grep -q '^\$ANSIBLE_VAULT'; then
    pass "$var present in $file (vault-encrypted)"
    return
  fi
  if grep -q "^${var}:" "$filepath" 2>/dev/null; then
    pass "$var defined in $file"
  else
    fail "$var not found in $file"
  fi
}

check_var group_vars/all/main.yml stack_name
check_var group_vars/all/main.yml stack_dir
check_var group_vars/all/main.yml ansible_user
check_var group_vars/all/services.yml services
check_var group_vars/all/services.yml accessories
check_var group_vars/all/security.yml firewall_allowed_tcp_ports
check_var group_vars/all/security.yml crowdsec_collections
check_var group_vars/all/users.yml users
check_var group_vars/all/vpn_access.yml vpn_allowed_ips
check_var group_vars/production/domains.yml letsencrypt_email
check_var group_vars/production/secrets.yml secrets

# ── Playbook syntax + task resolution ────────────────────────────────────
section "Playbook resolution (--list-tasks)"

cd "$INFRA_DIR"
COLLECTIONS_ENV="ANSIBLE_COLLECTIONS_PATH=$BAY_DIR/vendor/collections"
for playbook in deploy.yml provision.yml; do
  output=$(env "$COLLECTIONS_ENV" uv run --project "$BAY_DIR" ansible-playbook "$playbook" \
    --list-tasks -e target_host=production 2>&1) || true

  if grep -q 'tasks:' <<<"$output"; then
    task_count=$(echo "$output" | grep -c 'TAGS:' || true)
    pass "$playbook resolves ($task_count tasks listed)"
  else
    fail "$playbook failed to resolve tasks"
    # Show why — a resolution failure is otherwise a dead end for the reader.
    head -5 <<<"$output" | while IFS= read -r line; do
      printf "      %s\n" "$line"
    done
  fi
done

# restore.yml needs extra vars
output=$(env "$COLLECTIONS_ENV" uv run --project "$BAY_DIR" ansible-playbook restore.yml \
  --list-tasks -e target_host=production -e accessory=postgres \
  -e backup_file=/dev/null 2>&1) || true

if grep -q 'tasks:' <<<"$output"; then
  task_count=$(echo "$output" | grep -c 'TAGS:' || true)
  pass "restore.yml resolves ($task_count tasks listed)"
else
  fail "restore.yml failed to resolve tasks"
fi

# ── group_vars parity with bay example ──────────────────────────────────
section "group_vars parity with bay (advisory)"

# Parity is only meaningful for groups THIS consumer actually has. The example
# ships staging/ plus eu/ + na/; a single-region consumer legitimately has none
# of them, and hard-coding the example's layout made `bay test` permanently red
# for every real consumer. Derive the group set from the consumer's own
# inventory instead — INI headers, with [x:children]/[x:vars] reduced to `x`.
declare -A INV_GROUPS=([all]=1)
while IFS= read -r -d '' inv; do
  while read -r g; do
    [[ -n "$g" ]] && INV_GROUPS["$g"]=1
  done < <(grep -oE '^\[[^]]+\]' "$inv" 2>/dev/null | tr -d '[]' | cut -d: -f1)
done < <(find "$INFRA_DIR/hosts" -maxdepth 1 -type f -print0 2>/dev/null)

# EXPECTED_FILES above is the authority on what a consumer MUST have. Anything
# the example ships beyond it is either an optional feature (access_gateway.yml)
# or a layout choice (multi-region puts network.yml under each region rather
# than production/). Those are drift worth reporting, not failures — this
# section asserting more than EXPECTED_FILES is what made it wrong before.
while IFS= read -r -d '' f; do
  rel="${f#"$BAY_DIR/example/"}"
  group="$(cut -d/ -f2 <<<"$rel")"
  if [[ -z "${INV_GROUPS[$group]:-}" ]]; then
    continue  # group not in this consumer's inventory — not applicable
  fi
  if [[ -f "$INFRA_DIR/$rel" ]]; then
    pass "$rel present (matches bay example)"
  else
    note "$rel not present (optional in bay example — add if you use it)"
  fi
done < <(find "$BAY_DIR/example/group_vars" -name '*.yml' -print0)

# ── .gitignore covers secrets ────────────────────────────────────────────
section "Security"

if grep -q '.vault_pass' "$INFRA_DIR/.gitignore" 2>/dev/null; then
  pass ".vault_pass in .gitignore"
else
  fail ".vault_pass not in .gitignore"
fi

if grep -q '.bay/' "$INFRA_DIR/.gitignore" 2>/dev/null; then
  pass ".bay/ in .gitignore"
else
  fail ".bay/ not in .gitignore"
fi

# ── Summary ──────────────────────────────────────────────────────────────
section "Results"
printf "  %d passed, %d failed, %d advisory\n" "$PASS" "$FAIL" "$NOTES"

if [[ ${#ERRORS[@]} -gt 0 ]]; then
  printf "\n\033[31mFailures:\033[0m\n"
  for e in "${ERRORS[@]}"; do
    printf "  - %s\n" "$e"
  done
  exit 1
fi
