#!/bin/bash
# publish.sh — AutoSwap release script
# Usage: ./scripts/publish.sh [--npm] [--pypi] [--all]
#
# Prerequisites:
#   npm:  NPM_TOKEN in agent vault (npm token create --access public)
#   pypi: PYPI_TOKEN in agent vault (https://pypi.org/manage/account/token/)
#
# Add tokens to vault:
#   ~/.pi/agent/skills/agent-vault/vault.sh write NPM_TOKEN npm_...
#   ~/.pi/agent/skills/agent-vault/vault.sh write PYPI_TOKEN pypi-...

set -euo pipefail

VAULT="$HOME/.pi/agent/skills/agent-vault/vault.sh"
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NPM_DIR="$PKG_DIR/npm"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[publish]${NC} $*"; }
warn() { echo -e "${YELLOW}[publish]${NC} $*"; }
fail() { echo -e "${RED}[publish ERROR]${NC} $*" >&2; exit 1; }

PUBLISH_NPM=false
PUBLISH_PYPI=false

for arg in "$@"; do
  case $arg in
    --npm)  PUBLISH_NPM=true ;;
    --pypi) PUBLISH_PYPI=true ;;
    --all)  PUBLISH_NPM=true; PUBLISH_PYPI=true ;;
    *) fail "Unknown arg: $arg. Use --npm, --pypi, or --all" ;;
  esac
done

if ! $PUBLISH_NPM && ! $PUBLISH_PYPI; then
  fail "Specify --npm, --pypi, or --all"
fi

# ── npm publish ───────────────────────────────────────────────────────────────
if $PUBLISH_NPM; then
  log "Publishing to npm..."

  NPM_TOKEN=$($VAULT read NPM_TOKEN 2>/dev/null) || fail "NPM_TOKEN not found in vault. Add with: $VAULT write NPM_TOKEN npm_..."
  [ -z "$NPM_TOKEN" ] && fail "NPM_TOKEN is empty in vault"

  # Create temp .npmrc (never commit this)
  NPMRC="$NPM_DIR/.npmrc"
  echo "//registry.npmjs.org/:_authToken=${NPM_TOKEN}" > "$NPMRC"
  trap "rm -f '$NPMRC'" EXIT

  cd "$NPM_DIR"

  # Verify auth
  npm whoami --registry=https://registry.npmjs.org/ || fail "npm auth failed. NPM_TOKEN may be expired. Generate a new one at https://www.npmjs.com/settings/~/tokens"

  # Publish
  npm publish --access public --registry=https://registry.npmjs.org/
  log "✅ Published autoswap@$(node -p "require('./package.json').version") to npm"
fi

# ── PyPI publish ──────────────────────────────────────────────────────────────
if $PUBLISH_PYPI; then
  log "Publishing to PyPI..."

  PYPI_TOKEN=$($VAULT read PYPI_TOKEN 2>/dev/null) || fail "PYPI_TOKEN not found in vault. Add with: $VAULT write PYPI_TOKEN pypi-..."
  [ -z "$PYPI_TOKEN" ] && fail "PYPI_TOKEN is empty in vault"

  cd "$PKG_DIR"

  # Check tools
  python3 -m pip install --quiet build twine

  # Clean + build
  rm -rf dist/ build/ *.egg-info
  python3 -m build

  # Upload to PyPI
  TWINE_USERNAME="__token__" TWINE_PASSWORD="$PYPI_TOKEN" \
    python3 -m twine upload dist/* --non-interactive

  VERSION=$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])" 2>/dev/null || grep "^version" pyproject.toml | head -1 | cut -d'"' -f2)
  log "✅ Published autoswap==${VERSION} to PyPI"
fi

log "🎉 Done!"
