#!/bin/bash
# Install git hooks for pulse-agent
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_DIR="$SCRIPT_DIR/.git/hooks"

echo "Installing pre-commit hook..."

cat > "$HOOK_DIR/pre-commit" << 'HOOK'
#!/bin/bash
echo "Running pre-commit checks..."
python3 -m ruff check sre_agent/ tests/ || exit 1
python3 -m ruff format --check sre_agent/ tests/ || exit 1
# Fast subset: lint + format + core tests (~15s). Full suite runs in CI.
python3 -m pytest tests/test_skill_loader.py tests/test_orchestrator.py tests/test_agent.py tests/test_plan_templates.py tests/test_eval_tool_selection.py tests/test_backend_integration.py -q || exit 1
echo "Pre-commit checks passed (fast). Full suite: python3 -m pytest tests/ -q"
HOOK

chmod +x "$HOOK_DIR/pre-commit"
echo "Done. Pre-commit hook installed."
