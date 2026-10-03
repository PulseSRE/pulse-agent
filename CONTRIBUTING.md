# Contributing

Start with [README](README.md), [architecture](docs/ARCHITECTURE.md), and [testing](TESTING.md). UI contributions belong in [pulse-ui](https://github.com/PulseSRE/pulse-ui); deployment changes belong in [pulse-operator](https://github.com/PulseSRE/pulse-operator).

## Development setup

Python 3.11+ is required by `pyproject.toml`; CI uses Python 3.11.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

Provision a disposable PostgreSQL database as described in [TESTING](TESTING.md) before running tests. The autouse fixture drops its public schema; never use a production or development database containing valuable data.

```bash
make verify
python -m ruff format --check sre_agent/ tests/
python scripts/check_discipline.py
```

`make verify` runs Ruff lint, Mypy, and pytest. Formatting and discipline checks are separate. Live replay is a separate provider-backed quality gate; an offline check is insufficient evidence of model quality.

## Conventions

- Configure through `get_settings()` and Pydantic settings.
- Register native tools with the tool decorator/registry; verify write classification and safety enforcement for each execution path.
- Use the Kubernetes client helpers and `safe()` error handling; preserve caller-token context where an interactive route supplies it.
- Use `get_database()`; schema and forward migrations are maintained in `db_schema.py` and `db_migrations.py`.
- Update [API_CONTRACT](API_CONTRACT.md) for route/message changes and [SECURITY](SECURITY.md) for authorization or policy changes.
- Add regressions for changed behavior, including error paths and denied writes. Do not skip a broken prerequisite and report success.

## Optional pre-commit hook

`bash scripts/install-hooks.sh` installs a local hook in a normal clone. It runs Ruff lint, Ruff format checking, and six focused test files. It does **not** run Mypy or the entire test suite. Run the complete checks above before requesting review. The script assumes `.git` is a directory and needs adaptation for a Git worktree.

## CI and review

[evals.yml](.github/workflows/evals.yml) runs on main PRs/pushes, tags, a daily schedule, and manual dispatch. It includes lint, formatting, types, discipline, tests, route documentation coverage, and offline replay. Provider-backed replay and SRE-Bench simulation run under workflow conditions and require credentials. The image build, release publication, version comparison, and secret scan have separate workflows.

Describe the problem, final behavior, checks performed, and remaining deployment/provider validation. See [RELEASE](RELEASE.md) before tagging or publishing.
