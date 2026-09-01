"""Durable storage for investigation plan templates.

Plan templates ship as YAML in the repo, which is fine for the bundled ones —
they are rebuilt into every image. Templates *edited or created at runtime* are
not: the PUT handler rewrites YAML inside the installed package directory, so
an operator's edit survived exactly until the next rollout, silently.

Same store and the same version history as skills, so a plan edit is reversible
and a plan someone wrote by hand is not lost by a deploy.
"""

from __future__ import annotations

from pathlib import Path

from .artifact_store import KIND_PLAN, forget, get_version, hydrate, list_artifacts, list_versions, persist

__all__ = [
    "bundled_plans_dir",
    "forget_plan",
    "hydrate_plans_dir",
    "list_stored_plans",
    "persist_plan",
    "plan_version",
    "plan_versions",
    "plans_dir",
]


def plans_dir() -> Path:
    """The writable directory runtime-created plan YAML lives in.

    NOT the package directory. The container image's site-packages is
    read-only under OpenShift's arbitrary UID, so every write that targeted
    the package dir — create, edit, delete, boot-time hydration — failed
    there with EACCES; the sre-bench durable probe found create returning
    500 on its first live run. Bundled templates stay in the package dir as
    read-only seeds; this dir (DB-hydrated at boot, same as user skills)
    holds everything created or edited at runtime.
    """
    from .config import get_settings

    directory = Path(get_settings().server.user_plans_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def bundled_plans_dir() -> Path:
    """The read-only templates shipped inside the package."""
    return Path(__file__).parent / "plan_templates"


def persist_plan(name: str, content: str, *, source: str = "user", created_by: str = "") -> bool:
    """Write a plan template through to durable storage, archiving the prior body.

    ``name`` is the template's incident_type, which is what the REST surface
    addresses templates by and what the filename is derived from.
    """
    return persist(
        KIND_PLAN,
        name,
        content,
        rel_path=f"{name}.yaml",
        source=source,
        created_by=created_by,
    )


def forget_plan(name: str) -> bool:
    return forget(KIND_PLAN, name)


def list_stored_plans() -> list[dict]:
    return list_artifacts(KIND_PLAN)


def plan_versions(name: str) -> list[dict]:
    return list_versions(KIND_PLAN, name)


def plan_version(name: str, version: int) -> dict | None:
    return get_version(KIND_PLAN, name, version)


def hydrate_plans_dir(directory: Path | None = None) -> int:
    return hydrate(KIND_PLAN, Path(directory) if directory else plans_dir())
