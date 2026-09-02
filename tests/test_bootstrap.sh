#!/usr/bin/env bash
# Tests bootstrap.sh + setup end-to-end:
#   1. Creates a temp project using bootstrap.sh with local bay repo
#   2. Runs 'bin/bay setup --no-interactive' to scaffold the project
#   3. Verifies the scaffold is complete
#   4. Runs the consumer test suite (tests/test_infra.sh)
#   5. Cleans up
#
# Usage: bash tests/test_bootstrap.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMPDIR=""

PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS + 1)); printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS+=("$1"); printf "  \033[31m✗\033[0m %s\n" "$1"; }

section() { printf "\n\033[1m%s\033[0m\n" "$1"; }

cleanup() {
  if [[ -n "$TMPDIR" && -d "$TMPDIR" ]]; then
    rm -rf "$TMPDIR"
  fi
}
trap cleanup EXIT

# ── Run bootstrap ────────────────────────────────────────────────────────
section "Bootstrap"

TMPDIR=$(mktemp -d)
PROJECT_DIR="$TMPDIR/test-project"

printf "  Using bay repo: %s\n" "$BAY_DIR"
printf "  Target dir:      %s\n" "$PROJECT_DIR"

mkdir -p "$PROJECT_DIR"
# Mirror the real install flow: clone framework, then run bootstrap.
# --no-tags is load-bearing: bootstrap.sh pins to the newest tag when it finds
# one, which would test the last *release* rather than this working tree. That
# silently hid the rename to bay (the newest tag still ships the pre-1.0 console
# script), so the wrapper bootstrap wrote could not be spawned. With no tags to
# find, bootstrap leaves the clone on HEAD — the code we actually want tested.
git clone --quiet --no-tags "$BAY_DIR" "$PROJECT_DIR/.bay"
# Overlay the working tree on top of the clone so uncommitted framework changes
# (not just bootstrap.sh) are what gets exercised. Copying only bootstrap.sh
# meant every other working-tree change was tested against the *committed* CLI.
tar -C "$BAY_DIR" -cf - \
  --exclude=./.git \
  --exclude=./.venv \
  --exclude=./vendor \
  --exclude=./group_vars \
  --exclude=./.tracker \
  --exclude='*/__pycache__' \
  --exclude=./.pytest_cache \
  --exclude=./.mypy_cache \
  --exclude=./.ruff_cache \
  --exclude=./.ansible \
  . | tar -C "$PROJECT_DIR/.bay" -xf -
# Remove scaffold .bay-version to skip version-pin (avoids checkout conflict in dev)
rm -f "$PROJECT_DIR/.bay/example/.bay-version"
if bash "$PROJECT_DIR/.bay/bootstrap.sh" 2>&1; then
  pass "bootstrap.sh completed successfully"
else
  fail "bootstrap.sh exited with error"
  exit 1
fi

# Verify bin/bay wrapper was created by bootstrap
if [[ -x "$PROJECT_DIR/bin/bay" ]]; then
  pass "bin/bay wrapper created"
else
  fail "bin/bay wrapper missing after bootstrap"
  exit 1
fi

# ── Run init to scaffold ─────────────────────────────────────────────────
section "Setup (scaffold)"

cd "$PROJECT_DIR"
if bin/bay setup --no-interactive 2>&1; then
  pass "bin/bay setup --no-interactive completed successfully"
else
  fail "bin/bay setup --no-interactive failed"
  exit 1
fi

# ── Verify scaffold ─────────────────────────────────────────────────────
section "Scaffold structure"

EXPECTED_FILES=(
  Makefile
  ansible.cfg
  deploy.yml
  provision.yml
  restore.yml
  .gitignore
  README.md
  tests/test_infra.sh
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
  if [[ -f "$PROJECT_DIR/$f" ]]; then
    pass "$f"
  else
    fail "$f missing"
  fi
done

# Verify .bay/ was cloned
if [[ -d "$PROJECT_DIR/.bay" && -f "$PROJECT_DIR/.bay/bay.mk" ]]; then
  pass ".bay/ framework cloned"
else
  fail ".bay/ framework not found"
fi


# ── Run consumer test suite ──────────────────────────────────────────────
section "Consumer test suite (tests/test_infra.sh)"

if [[ -x "$PROJECT_DIR/tests/test_infra.sh" ]]; then
  printf "  Running consumer tests...\n\n"
  if bash "$PROJECT_DIR/tests/test_infra.sh"; then
    pass "consumer test suite passed"
  else
    fail "consumer test suite failed"
  fi
else
  fail "tests/test_infra.sh not found or not executable"
fi

# ── Wrapper survives a pin to an older tag ───────────────────────────────
# Regression: bootstrap.sh runs from the clone's HEAD, then checks out the
# pinned tag — which rewinds the working tree underneath it. A tag older than
# scripts/bin-bay-wrapper.sh left nothing to copy and the run died with
# "cp: cannot stat". Only the wrapper half is exercised here; a full bootstrap
# would sync Ansible for a third time.
section "Wrapper survives an old pinned tag"

OLD_TAG_DIR=$(mktemp -d)

# A framework repo whose only tag predates the wrapper source.
mkdir -p "$OLD_TAG_DIR/framework/scripts"
cd "$OLD_TAG_DIR/framework"
git init -q .
printf 'placeholder\n' > pyproject.toml
git add -A
git -c user.email=t@example.test -c user.name=t commit -qm "old release"
git tag v0.0.1
# The wrapper source arrives only after the tag, exactly as it did in reality.
cp "$BAY_DIR/scripts/bin-bay-wrapper.sh" scripts/bin-bay-wrapper.sh
cp "$BAY_DIR/bootstrap.sh" bootstrap.sh
chmod +x bootstrap.sh scripts/bin-bay-wrapper.sh
git add -A
git -c user.email=t@example.test -c user.name=t commit -qm "add wrapper source"

# A consumer that clones it and bootstraps. Stop before the dependency install:
# this test is about the wrapper, and `uv sync` here would double the runtime.
mkdir -p "$OLD_TAG_DIR/consumer"
cd "$OLD_TAG_DIR/consumer"
git clone --quiet "$OLD_TAG_DIR/framework" .bay
sed -n '1,/^# ── Install dependencies/p' .bay/bootstrap.sh \
  | sed '$d' > .bay/bootstrap-wrapper-only.sh

if bash .bay/bootstrap-wrapper-only.sh >"$OLD_TAG_DIR/out.log" 2>&1; then
  pass "bootstrap runs against a clone pinned to a pre-wrapper tag"
else
  fail "bootstrap failed against a pre-wrapper tag: $(tail -3 "$OLD_TAG_DIR/out.log")"
fi

if [[ -x "$OLD_TAG_DIR/consumer/bin/bay" ]]; then
  pass "bin/bay created despite the pinned tag lacking the wrapper source"
else
  fail "bin/bay missing after bootstrap against a pre-wrapper tag"
fi

if diff -q "$BAY_DIR/scripts/bin-bay-wrapper.sh" "$OLD_TAG_DIR/consumer/bin/bay" >/dev/null 2>&1; then
  pass "bin/bay matches the shared wrapper source"
else
  fail "bin/bay differs from scripts/bin-bay-wrapper.sh"
fi

# The pin must still have happened — the fix must not skip the checkout.
if [[ "$(cat "$OLD_TAG_DIR/consumer/.bay-version" 2>/dev/null)" == "v0.0.1" ]]; then
  pass "framework still pinned to the tag"
else
  fail "framework was not pinned to v0.0.1"
fi

cd "$BAY_DIR"
rm -rf "$OLD_TAG_DIR"

# ── Summary ──────────────────────────────────────────────────────────────
section "Bootstrap test results"
printf "  %d passed, %d failed\n" "$PASS" "$FAIL"

if [[ ${#ERRORS[@]} -gt 0 ]]; then
  printf "\n\033[31mFailures:\033[0m\n"
  for e in "${ERRORS[@]}"; do
    printf "  - %s\n" "$e"
  done
  exit 1
fi
