"""Map a firing alert to the Inbox item and investigation that already exist for it.

The firing-alerts scanner turns each alert into a monitor finding, and the
monitor bridges that finding to an Inbox item, keyed on a correlation key
built from the alert's resource. The Alerts page had no way back: an admin
starting from an alert never saw the investigation that had already run.

This recomputes the same key from the alert's labels — the scanner's own
``_alert_resources`` and the bridge's own ``_finding_corr_key`` — so the
answer is the item the monitor created, not a guess. It also reports:

- ``related``: open items about the same resource under another category
  (the crashloop or OOM item for the pod a ``KubePodCrashLooping`` alert is
  about), which is usually where the diagnosis lives;
- ``episode``: the open episode the alert's finding is a symptom of, with its
  cause and the cause's own item.

Database only: nothing here reads the cluster.
"""

from __future__ import annotations

import re
from typing import Any

MAX_LABELS = 40
MAX_VALUE_LEN = 1024
MAX_BATCH = 200
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class AlertLabelsError(ValueError):
    pass


def validate_labels(labels: Any) -> dict[str, str]:
    if not isinstance(labels, dict):
        raise AlertLabelsError("labels must be an object of label name to value")
    if len(labels) > MAX_LABELS:
        raise AlertLabelsError(f"at most {MAX_LABELS} labels")
    clean: dict[str, str] = {}
    for name, value in labels.items():
        if not isinstance(name, str) or not _LABEL_NAME_RE.match(name):
            raise AlertLabelsError(f"{name!r} is not a valid label name")
        if not isinstance(value, str) or len(value) > MAX_VALUE_LEN:
            raise AlertLabelsError(f"the value of {name} must be a string of at most {MAX_VALUE_LEN} characters")
        clean[name] = value
    if not clean.get("alertname"):
        raise AlertLabelsError("alertname is required")
    return clean


def correlation_key_for_alert(labels: dict[str, str]) -> tuple[str, dict]:
    """The Inbox correlation key the monitor gives this alert's finding, and its primary resource."""
    from .inbox import _finding_corr_key
    from .monitor.scanners import _alert_resources

    alertname = labels["alertname"]
    namespace = labels.get("namespace", "")
    resources = _alert_resources(alertname, labels, namespace)
    finding = {"category": "alerts", "title": alertname, "resources": resources}
    return _finding_corr_key(finding), (resources[0] if resources else {})


def summarize_item(row: Any) -> dict[str, Any]:
    from .inbox import _deserialize_row

    item = _deserialize_row(row)
    meta = item.get("metadata") or {}
    investigation = None
    if meta.get("investigation_id") or meta.get("suspected_cause"):
        investigation = {
            "id": meta.get("investigation_id"),
            "summary": meta.get("investigation_summary") or "",
            "suspectedCause": meta.get("suspected_cause") or "",
            "recommendedFix": meta.get("recommended_fix") or "",
            "confidence": meta.get("investigation_confidence"),
        }
    return {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "status": item.get("status", ""),
        "severity": item.get("severity"),
        "namespace": item.get("namespace"),
        "claimedBy": item.get("claimed_by"),
        "dueDate": item.get("due_date"),
        "findingId": item.get("finding_id"),
        "correlationKey": item.get("correlation_key"),
        "investigation": investigation,
        "fixReady": bool(meta.get("action_plan")),
    }


class _EpisodeIndex:
    """Open episodes, read once per request rather than once per alert."""

    def __init__(self) -> None:
        from .monitor.episodes import list_open, symptom_keys_by_episode

        self.symptoms = symptom_keys_by_episode()
        self.episodes = {e["id"]: e for e in list_open()} if self.symptoms else {}

    def for_key(self, key: str) -> dict | None:
        episode_id = self.symptoms.get(key)
        return self.episodes.get(episode_id) if episode_id else None


def triage_for_alert(labels: dict[str, str], episodes: _EpisodeIndex | None = None) -> dict[str, Any]:
    from .repositories.inbox_repo import get_inbox_repo

    repo = get_inbox_repo()
    key, resource = correlation_key_for_alert(labels)
    row = repo.find_active_by_correlation_task(key)
    related: list[dict[str, Any]] = []
    if resource and resource.get("kind") not in ("Alert", None) and ":" in key:
        resource_key = key.split(":", 1)[1]
        related = [summarize_item(r) for r in repo.find_active_tasks_for_resource(resource_key, key)]
    episode_out = None
    episode = (episodes or _EpisodeIndex()).for_key(key)
    if episode:
        cause_key = episode.get("correlation_key") or ""
        cause_row = repo.find_active_by_correlation_task(cause_key) if cause_key else None
        episode_out = {
            "id": episode.get("id"),
            "causeTitle": episode.get("cause_title") or "",
            "causeItem": summarize_item(cause_row) if cause_row else None,
        }
    return {
        "alertname": labels["alertname"],
        "correlationKey": key,
        "resource": resource or None,
        "item": summarize_item(row) if row else None,
        "related": related,
        "episode": episode_out,
    }


def triage_for_alerts(label_sets: list[dict[str, str]]) -> list[dict[str, Any]]:
    episodes = _EpisodeIndex()
    return [triage_for_alert(labels, episodes) for labels in label_sets]
