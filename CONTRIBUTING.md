# Contributing

## Code Style

### TypeScript (OpenshiftPulse)
- **Linter**: ESLint with TypeScript + React + React Hooks plugins
- **Formatter**: Prettier (semi, single quotes, trailing commas, 100 char width)
- **Type checking**: `tsc --noEmit` (strict mode available via `tsconfig.strict.json`)
- Run: `npm run verify` (type-check + strict + lint + test + build)

### Python (pulse-agent)
- **Linter**: Ruff (pycodestyle, pyflakes, isort, bugbear, simplify)
- **Formatter**: Ruff format (double quotes, 120 char width)
- **Type checking**: Mypy (permissive mode)
- Run: `make verify` (lint + type-check + test)

## Conventions

### General
- Types defined once in canonical locations, imported everywhere
- No duplicate interfaces across files
- Every REST endpoint documented in API_CONTRACT.md
- Every security change documented in SECURITY.md
- Version is dynamic (read from package metadata)

### Python
- Use `@beta_tool` decorator for K8s tools, register in `tool_registry`
- Use `safe()` wrapper for all K8s API calls
- Use `get_database()` for database access (PostgreSQL only — no SQLite fallback)
- Config via Pydantic Settings (`get_settings()`)

### TypeScript
- Use Zustand for state (no Redux)
- Use `cn()` from `@/lib/utils` for className merging
- Use lucide-react for icons
- Use Card, EmptyState from primitives
- Feature flags via `isFeatureEnabled()`

## Pre-commit Hooks

Install: `bash scripts/install-hooks.sh`

Hooks run automatically before every commit:
- Python: ruff lint + ruff format check + mypy + pytest

## CI

Both repos have GitHub Actions that run on every push to main:
- pulse-agent: pytest + ruff + eval gates
- OpenshiftPulse: type-check + strict + lint + test + build
