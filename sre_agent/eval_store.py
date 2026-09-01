"""Durable storage for scaffolded eval scenarios and replay fixtures.

Scaffolded evals ship nothing — they exist only because a verified resolution
generated them at runtime. They used to be written into the installed package
directory (``sre_agent/evals/scenarios_data`` and ``.../fixtures``), which is
read-only under OpenShift's arbitrary UID, so on a cluster every scaffold and
every boot-time hydration failed with EACCES and was swallowed. Same bug, same
fix as plan templates (see ``plan_store.plans_dir``): bundled eval data stays
in the package as a read-only seed, and everything written at runtime lives in
a writable directory that is DB-hydrated at boot and scanned by the loaders
alongside the packaged data.
"""

from __future__ import annotations

from pathlib import Path

from .artifact_store import KIND_EVAL_FIXTURE, KIND_EVAL_SCENARIO, hydrate

__all__ = [
    "bundled_fixtures_dir",
    "bundled_scenarios_dir",
    "fixtures_dir",
    "hydrate_evals_dirs",
    "scenarios_dir",
]


def _user_evals_root() -> Path:
    from .config import get_settings

    return Path(get_settings().server.user_evals_dir)


def scenarios_dir() -> Path:
    """The writable directory runtime-scaffolded scenario suites live in.

    NOT the package directory — site-packages is read-only on the cluster.
    """
    directory = _user_evals_root() / "scenarios_data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def fixtures_dir() -> Path:
    """The writable directory runtime-scaffolded replay fixtures live in."""
    directory = _user_evals_root() / "fixtures"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def bundled_scenarios_dir() -> Path:
    """The read-only scenario suites shipped inside the package."""
    return Path(__file__).parent / "evals" / "scenarios_data"


def bundled_fixtures_dir() -> Path:
    """The read-only replay fixtures shipped inside the package."""
    return Path(__file__).parent / "evals" / "fixtures"


def hydrate_evals_dirs() -> int:
    """Replay DB-persisted eval artifacts into the writable dirs at boot.

    Returns how many files were written. Must never target the package
    directory: that is exactly the EACCES-on-every-boot bug this module
    exists to fix.
    """
    written = hydrate(KIND_EVAL_SCENARIO, scenarios_dir())
    written += hydrate(KIND_EVAL_FIXTURE, fixtures_dir())
    return written
