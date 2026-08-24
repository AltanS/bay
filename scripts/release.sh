#!/usr/bin/env bash
set -euo pipefail

# Bay framework release script
# Usage: make release VERSION=0.38.1

VERSION="${1:-}"

# --- Validation ---

if [[ -z "$VERSION" ]]; then
  echo "error: VERSION is required"
  echo "usage: make release VERSION=0.38.1"
  exit 1
fi

# Strip leading 'v' if provided, normalize to bare version
VERSION="${VERSION#v}"

# Validate semver format
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "error: VERSION must be semver (e.g. 0.38.1), got: $VERSION"
  exit 1
fi

TAG="v${VERSION}"

# Must be on main branch
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" != "main" ]]; then
  echo "error: must be on main branch (currently on '$BRANCH')"
  exit 1
fi

# Working tree must be clean (except version.yml which we'll modify)
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: working tree is dirty — commit or stash changes first"
  exit 1
fi

# Tag must not already exist
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "error: tag $TAG already exists"
  exit 1
fi

# The changelog is how consumers find out what a framework bump changed. A
# release without an entry is invisible to them, so refuse to tag one.
if ! grep -qE "^##[[:space:]]*\[${VERSION//./\\.}\]" CHANGELOG.md; then
  echo "error: CHANGELOG.md has no entry for ${VERSION}"
  echo ""
  echo "Add a section before releasing, then commit it:"
  echo ""
  echo "    ## [${VERSION}] — $(date -u +%Y-%m-%d)"
  echo ""
  echo "    ### Added / Changed / Fixed"
  echo "    - ..."
  echo ""
  echo "Include an 'Upgrade notes' subsection for anything a consumer must do"
  echo "by hand (a provision run, a renamed variable, a manual migration)."
  exit 1
fi

# --- Pre-release gates ---
#
# Nine consecutive releases (v1.1.0 through v1.4.1) were tagged and pushed
# while the `leak-scan` CI job was red on main. Each one shipped a real
# production hostname and IP. The guard was working the whole time; nothing
# stopped a release from ignoring it, because this script never looked.
#
# So it looks now. These run locally and block the tag. Set
# BAY_RELEASE_SKIP_CHECKS=1 to override, which should mean "I have already
# run them", not "they are inconvenient".

if [[ "${BAY_RELEASE_SKIP_CHECKS:-0}" == "1" ]]; then
  echo "warning: pre-release checks skipped (BAY_RELEASE_SKIP_CHECKS=1)"
else
  echo "Running pre-release checks..."

  echo "  → identity leak scan"
  if ! bash scripts/leak-scan.sh; then
    echo ""
    echo "error: leak-scan failed — refusing to tag a release that leaks identity"
    echo "This is the check that nine releases shipped past. Fix it, don't skip it."
    exit 1
  fi

  echo "  → lint"
  if ! make lint >/dev/null; then
    echo "error: 'make lint' failed — run it to see the findings"
    exit 1
  fi

  echo "  → tests"
  if ! make test >/dev/null; then
    echo "error: 'make test' failed — run it to see the failures"
    exit 1
  fi

  # If HEAD is already on the remote, ask GitHub what it thought of it. This
  # catches anything CI checks that the local targets do not. Absent gh, or an
  # unpushed HEAD, this is a warning: the local gates above are the real floor.
  if command -v gh >/dev/null 2>&1; then
    HEAD_SHA=$(git rev-parse HEAD)
    CI_STATUS=$(gh run list --commit "$HEAD_SHA" --limit 1 --json conclusion \
                  --jq '.[0].conclusion // empty' 2>/dev/null || true)
    case "$CI_STATUS" in
      failure|cancelled|timed_out)
        echo "error: CI concluded '$CI_STATUS' for $HEAD_SHA"
        echo "Inspect with: gh run list --commit $HEAD_SHA"
        exit 1
        ;;
      success)
        echo "  → CI green for $HEAD_SHA"
        ;;
      *)
        echo "  → CI status unknown for $HEAD_SHA (not pushed yet, or still running)"
        ;;
    esac
  fi

  echo "Pre-release checks passed."
  echo ""
fi

# --- Release ---

echo "Releasing $TAG..."

# Bump version.yml
sed -i "s/^bay_version: .*/bay_version: \"${VERSION}\"/" version.yml

# Verify the bump worked
if ! grep -q "bay_version: \"${VERSION}\"" version.yml; then
  echo "error: failed to update version.yml"
  git checkout version.yml
  exit 1
fi

# Bump pyproject.toml in lockstep. tests/test_pyproject_metadata.py fails the
# build if these drift, and uv.lock records the package version too — a stale
# lock breaks CI's `uv sync --locked`.
sed -i "0,/^version = .*/s//version = \"${VERSION}\"/" pyproject.toml

if ! grep -q "^version = \"${VERSION}\"" pyproject.toml; then
  echo "error: failed to update pyproject.toml"
  git checkout version.yml pyproject.toml
  exit 1
fi

if ! uv lock; then
  echo "error: uv lock failed"
  git checkout version.yml pyproject.toml uv.lock
  exit 1
fi

# Commit + tag + push
git add version.yml pyproject.toml uv.lock
git commit -m "chore: release ${TAG}"
git tag "$TAG"
git push origin main --tags

echo ""
echo "Released $TAG"
