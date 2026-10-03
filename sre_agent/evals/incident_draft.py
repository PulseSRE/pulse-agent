"""Export one explicitly selected verified incident as a review-only eval draft.

No free text, logs, tool inputs, snapshots, names, or credentials are exported.
A summary is not a recorded tool response. The draft cannot run or gate releases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


class DraftError(ValueError):
    """A source record cannot support a verified incident draft."""


def _reference(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DraftError("Source identifiers must be nonempty strings")
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _timestamp(row: dict, key: str) -> int:
    value = row.get(key)
    if type(value) is not int or value <= 0:
        raise DraftError("Source chronology is missing or malformed")
    return value


def build_draft(action: dict, finding: dict, investigation: dict) -> dict:
    """Validate linked stored observations; export only allowlisted structure."""
    if not all(isinstance(row, dict) for row in (action, finding, investigation)):
        raise DraftError("Action, finding, and investigation records are required")
    if action.get("status") != "completed" or action.get("verification_status") != "verified":
        raise DraftError("Only completed, independently verified actions may seed drafts")
    if not isinstance(action.get("verification_evidence"), str) or not action["verification_evidence"].strip():
        raise DraftError("Stored verification evidence is required")
    finding_id = finding.get("id")
    if not finding_id or action.get("finding_id") != finding_id or investigation.get("finding_id") != finding_id:
        raise DraftError("Source records must belong to the same finding")
    resolved = finding.get("resolved")
    if type(resolved) not in (int, bool) or resolved != 1 or investigation.get("status") != "completed":
        raise DraftError("A resolved finding and completed investigation are required")
    created = _timestamp(finding, "timestamp")
    investigated = _timestamp(investigation, "timestamp")
    executed = _timestamp(action, "timestamp")
    verified = _timestamp(action, "verification_timestamp")
    if not created <= investigated <= executed < verified:
        raise DraftError("Verification must follow execution, and investigation must precede it")
    evidence = investigation.get("evidence")
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError as exc:
            raise DraftError("Stored investigation evidence is malformed") from exc
    if not isinstance(evidence, list) or not evidence:
        raise DraftError("Stored investigation evidence is required")
    observations = [item.get("observation") if isinstance(item, dict) else item for item in evidence]
    if any(not isinstance(item, str) or not item.strip() for item in observations):
        raise DraftError("Stored investigation evidence is malformed")
    # Known product incident categories only. Arbitrary labels can contain secrets.
    category = finding.get("category")
    category = (
        category
        if isinstance(category, str) and category in {"crashloop", "workloads", "scheduling", "oom", "image_pull"}
        else "other"
    )
    tool = action.get("tool")
    tool = (
        tool
        if isinstance(tool, str)
        and tool
        in {
            "restart_deployment",
            "scale_deployment",
            "rollback_deployment",
            "delete_pod",
            "patch_resource",
            "apply_yaml",
            "cordon_node",
            "drain_node",
        }
        else "other"
    )
    source = {
        "action_ref": _reference(action.get("id")),
        "finding_ref": _reference(finding_id),
        "investigation_ref": _reference(investigation.get("id")),
    }
    return {
        "artifact_kind": "incident_replay_draft",
        "schema_version": 1,
        "name": f"incident_draft_{source['action_ref']}",
        "runnable": False,
        "review": {
            "status": "needs_review",
            "required_steps": [
                "Review linked incident records in the source deployment; confirm causal diagnosis and durable recovery",
                "Capture actual tool request/response pairs and sanitize every field; summaries are not recordings",
                "Supply a sanitized incident prompt and resource aliases; review authorization and denial cases",
                "Write expected checks independently of the observed answer and test the reviewed fixture",
                "Submit reviewed fixture and acceptance manifest update through normal code review",
            ],
        },
        "source": source,
        "observations": {
            "incident_category": category,
            "action_execution": "completed",
            "mutation_tool": tool,
            "stored_verification": "verified",
            "verification_evidence": "[OMITTED: review in source deployment]",
            "investigation_evidence_items": len(evidence),
            "chronology_validated": True,
        },
        "redaction": {
            "policy": "omit_all_free_text_and_payloads",
            "omitted": [
                "identifiers",
                "resource_names",
                "input",
                "logs",
                "snapshots",
                "summary",
                "root_cause",
                "verification_text",
            ],
        },
        "prompt": "[REVIEW REQUIRED: supply sanitized incident prompt]",
        "recorded_responses": {},
        "expected": {"should_block_release": False},
        "limitations": [
            "This draft is metadata, not a replayable trajectory or a recovery metric",
            "No tool snapshot fidelity or expected model answer was inferred from summaries",
            "Stored verified status does not prove the proposed remediation caused recovery",
        ],
    }


def load_selected_records(action_id: str) -> tuple[dict, dict, dict]:
    """Read only the selected action and its linked, preceding investigation."""
    from ..repositories.monitor_repo import get_monitor_repo

    repo = get_monitor_repo()
    action = repo.get_action_by_id(action_id)
    if not action or action.get("id") != action_id:
        raise DraftError("Selected action was not found")
    finding_id = action.get("finding_id")
    if not finding_id:
        raise DraftError("Selected action has no linked finding")
    finding = repo.fetch_finding_by_id(finding_id)
    investigation = repo.db.fetchone(
        "SELECT id, finding_id, timestamp, status, evidence FROM investigations "
        "WHERE finding_id = ? AND status = 'completed' AND timestamp <= ? "
        "ORDER BY timestamp DESC LIMIT 1",
        (finding_id, _timestamp(action, "timestamp")),
    )
    if not finding or not investigation:
        raise DraftError("Linked finding or preceding investigation was not found")
    return action, finding, investigation


def write_draft(draft: dict, output: Path) -> None:
    """Private, exclusive output; never put unreviewed drafts in eval loaders."""
    from ..config import get_settings
    from ..eval_store import bundled_fixtures_dir, bundled_scenarios_dir

    target = output.resolve()
    runtime = Path(get_settings().server.user_evals_dir).resolve()
    for forbidden in (runtime, bundled_fixtures_dir().resolve(), bundled_scenarios_dir().resolve()):
        if target == forbidden or forbidden in target.parents:
            raise DraftError("Draft output must be outside automatically loaded eval directories")
    # O_EXCL also prevents overwriting an existing review or following a symlink.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(draft, stream, indent=2)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action-id", required=True, help="Explicitly selected action in the configured source database"
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="New review draft file outside eval loader directories"
    )
    args = parser.parse_args(argv)
    try:
        draft = build_draft(*load_selected_records(args.action_id))
        write_draft(draft, args.output)
    except DraftError as exc:
        print(f"Draft refused: {exc}")
        return 1
    except Exception as exc:
        # Never print source data or DB exception messages (which may contain credentials).
        print(f"Draft could not be created ({type(exc).__name__})")
        return 1
    print("Created redacted draft: needs_review; no replay fixture or release assertion was promoted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
