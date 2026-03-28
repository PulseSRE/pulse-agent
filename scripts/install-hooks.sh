#!/bin/bash
# Install git hooks for pulse-agent
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_DIR="$SCRIPT_DIR/.git/hooks"

echo "Installing pre-commit hook..."

cat > "$HOOK_DIR/pre-commit" << 'HOOK'
#!/bin/bash
# Pre-commit hook: pytest
echo "Running pre-commit checks..."
python3 -m pytest tests/ -q || exit 1
echo "Pre-commit checks passed."
HOOK

chmod +x "$HOOK_DIR/pre-commit"
echo "Done. Pre-commit hook installed."
