"""Create review-only metadata drafts from skill scaffolding.

Phase summaries and model findings are not tool recordings or recovery
observations. New automatic scaffolds live in a separate durable drafts
folder; they never enter scenario suites or replay fixture loaders. Existing
reviewed fixtures are not rewritten or migrated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re

logger = logging.getLogger("pulse_agent.eval_scaffolder")

_REVIEW_TOOLS = frozenset(
    {
        "list_pods",
        "describe_pod",
        "describe_resource",
        "get_pod_logs",
        "get_events",
        "list_nodes",
        "get_node_metrics",
        "get_pod_metrics",
        "restart_deployment",
        "scale_deployment",
        "rollback_deployment",
        "delete_pod",
        "patch_resource",
        "apply_yaml",
        "cordon_node",
        "drain_node",
    }
)


def _scaffold_draft(skill_name: str, finding: dict, *, source_kind: str, tools_requested: list[str]) -> bool:
    if not isinstance(skill_name, str) or not re.sub(r"[^a-z0-9_-]", "", skill_name.lower()):
        return False
    from .artifact_store import KIND_EVAL_DRAFT, persist
    from .eval_store import drafts_dir

    # Keep source identity useful for deduplication without exporting raw names.
    source_ref = hashlib.sha256(
        json.dumps([skill_name, finding.get("id"), source_kind], sort_keys=True).encode()
    ).hexdigest()[:24]
    name = f"scaffold_draft_{source_ref}"
    selected = [tool for tool in tools_requested if isinstance(tool, str) and tool in _REVIEW_TOOLS]
    draft = {
        "artifact_kind": "incident_replay_draft",
        "schema_version": 1,
        "name": name,
        "runnable": False,
        "review": {
            "status": "needs_review",
            "required_steps": [
                "Select a linked action with stored verified recovery using the incident_draft CLI",
                "Review diagnosis, consent, and causal recovery in the source deployment",
                "Capture and redact actual tool request/response recordings; do not use phase summaries",
                "Write independent expected checks and submit a reviewed fixture with its manifest update",
            ],
        },
        "source": {"kind": source_kind, "source_ref": source_ref},
        "tool_selection": {
            "requested_tools": selected[:10],
            "omitted_count": len(tools_requested) - min(len(selected), 10),
        },
        "observations": {"stored_action_verification": "not_available"},
        "prompt": "[REVIEW REQUIRED: supply sanitized incident prompt]",
        "recorded_responses": {},
        "expected": {"should_block_release": False},
        "redaction": {"policy": "omit_all_free_text_and_payloads"},
        "limitations": [
            "Skill scaffolding is not evidence of verified recovery",
            "Requested tool names are not tool result recordings",
        ],
    }
    try:
        path = drafts_dir() / f"{name}.json"
        rendered = json.dumps(draft, indent=2) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(rendered)
        return persist(KIND_EVAL_DRAFT, name, rendered, rel_path=f"{name}.json", source="scaffolded_draft")
    except FileExistsError:
        # A previous persistence failure may have left only the overlay copy.
        # Retry durability only when the content is still our identical draft;
        # never overwrite or persist somebody's in-progress review as generated.
        if path.read_text(encoding="utf-8") == rendered:
            return persist(KIND_EVAL_DRAFT, name, rendered, rel_path=f"{name}.json", source="scaffolded_draft")
        return False
    except Exception:
        logger.warning("Could not create review-only eval draft", exc_info=True)
        return False


def scaffold_eval_from_plan(
    *,
    skill_name: str,
    finding: dict,
    plan_result: object,
    tools_called: list[str],
    confidence: float,
    duration_seconds: float,
) -> bool:
    # Compatibility signature; confidence/duration/model output are intentionally
    # not promoted to eval metrics, recovery labels, or expected answers.
    return _scaffold_draft(skill_name, finding, source_kind="plan", tools_requested=tools_called)


def scaffold_eval_from_investigation(*, skill_name: str, finding: dict, investigation_result: dict) -> bool:
    return _scaffold_draft(skill_name, finding, source_kind="investigation", tools_requested=[])
