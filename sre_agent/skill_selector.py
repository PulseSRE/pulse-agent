"""Multi-signal skill selector — ORCA architecture.

Replaces keyword-only routing with 5-channel fusion. Each channel scores
every skill independently, then scores are fused with weighted sum and re-ranked.

Channels:
1. Keyword scoring (ported from classify_query)
2. Alert taxonomy (alert name prefixes + scanner categories)
3. Component tags (K8s resource type matching)
4. Historical co-occurrence (from skill_usage table)
5. Temporal context (recent changes, deployments, updates)
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("pulse_agent.skill_selector")


@dataclass
class SelectionResult:
    """Result of multi-signal skill selection."""

    skill_name: str
    fused_scores: dict[str, float]  # skill_name -> final score
    channel_scores: dict[str, dict[str, float]]  # channel_name -> {skill_name: score}
    threshold_used: float
    conflicts: list[dict] = field(default_factory=list)
    selection_ms: int = 0
    source: str = "orca"  # "orca" | "fallback"


# Default channel weights (sum to 1.0)
DEFAULT_WEIGHTS: dict[str, float] = {
    "keyword": 0.35,
    "component": 0.25,
    "historical": 0.20,
    "taxonomy": 0.15,  # added in Task 4
    "temporal": 0.05,  # added in Task 4
    "semantic": 0.0,  # activate via PULSE_AGENT_EMBEDDING_CHANNEL=1
}

# K8s resource types for component tag extraction
_K8S_RESOURCES = re.compile(
    r"\b(pod|pods|deployment|deployments|service|services|node|nodes|"
    r"hpa|pvc|pvcs|configmap|configmaps|secret|secrets|"
    r"ingress|ingresses|route|routes|statefulset|statefulsets|"
    r"daemonset|daemonsets|job|jobs|cronjob|cronjobs|"
    r"namespace|namespaces|operator|operators|"
    r"replicaset|replicasets|endpoint|endpoints|"
    r"certificate|certificates|cert|certs|"
    r"networkpolicy|scc|clusterrole|rolebinding)\b",
    re.IGNORECASE,
)

# Map resource types to skill categories
_RESOURCE_CATEGORY_MAP: dict[str, list[str]] = {
    "pod": ["diagnostics", "workloads"],
    "pods": ["diagnostics", "workloads"],
    "deployment": ["workloads"],
    "deployments": ["workloads"],
    "service": ["networking"],
    "services": ["networking"],
    "node": ["diagnostics"],
    "nodes": ["diagnostics"],
    "hpa": ["monitoring", "workloads"],
    "pvc": ["storage"],
    "pvcs": ["storage"],
    "configmap": ["operations"],
    "configmaps": ["operations"],
    "secret": ["security", "operations"],
    "secrets": ["security", "operations"],
    "ingress": ["networking"],
    "ingresses": ["networking"],
    "route": ["networking"],
    "routes": ["networking"],
    "statefulset": ["workloads"],
    "statefulsets": ["workloads"],
    "daemonset": ["workloads"],
    "daemonsets": ["workloads"],
    "job": ["workloads"],
    "jobs": ["workloads"],
    "cronjob": ["workloads"],
    "cronjobs": ["workloads"],
    "namespace": ["diagnostics"],
    "namespaces": ["diagnostics"],
    "operator": ["diagnostics", "operations"],
    "operators": ["diagnostics", "operations"],
    "replicaset": ["workloads"],
    "replicasets": ["workloads"],
    "endpoint": ["networking"],
    "endpoints": ["networking"],
    "certificate": ["security"],
    "certificates": ["security"],
    "cert": ["security"],
    "certs": ["security"],
    "networkpolicy": ["security", "networking"],
    "scc": ["security"],
    "clusterrole": ["security"],
    "rolebinding": ["security"],
}

# Historical cache
_historical_cache: dict[str, dict[str, int]] | None = None
_historical_cache_ts: float = 0
_HISTORICAL_CACHE_TTL = 300  # 5 minutes

# Alert name prefix → skill mapping
_ALERT_TAXONOMY: dict[str, str] = {
    "kube": "sre",
    "pod": "sre",
    "node": "sre",
    "etcd": "sre",
    "podsecurity": "security",
    "rbac": "security",
    "network": "security",
    "certificate": "security",
    "cert": "security",
    "image": "sre",
    "api": "sre",
    "hpa": "sre",
    "pvc": "sre",
    "daemonset": "sre",
}

# Scanner category → skill
_CATEGORY_SKILL: dict[str, str] = {
    "crashloop": "sre",
    "pending": "sre",
    "workloads": "sre",
    "nodes": "sre",
    "alerts": "sre",
    "oom": "sre",
    "image_pull": "sre",
    "operators": "sre",
    "daemonsets": "sre",
    "hpa": "sre",
    "cert_expiry": "security",
    "security": "security",
}

# Temporal keywords for detecting recent changes
_TEMPORAL_KEYWORDS = [
    "just deployed",
    "after deploy",
    "after upgrade",
    "since restart",
    "recent change",
    "just changed",
    "after update",
    "since update",
    "minutes ago",
    "just now",
    "recently",
]

# Conflict detection lists
HARD_CONFLICTS: list[tuple[str, str]] = [
    ("restart_deployment", "rollback_deployment"),
    ("scale_deployment", "drain_node"),
    ("delete_pod", "restart_deployment"),
]

SOFT_CONFLICTS: list[tuple[str, str]] = [
    ("scale_deployment", "rollback_deployment"),
    ("cordon_node", "uncordon_node"),
]


class SkillSelector:
    """Multi-signal skill retrieval engine."""

    def __init__(self, skills: dict, keyword_index: list | None = None):
        """
        Args:
            skills: dict of skill_name -> Skill objects (from skill_loader._skills)
            keyword_index: pre-built keyword index [(keyword, skill_name, len), ...]
        """
        self._skills = skills
        self._keyword_index = keyword_index or []
        self._weights = dict(DEFAULT_WEIGHTS)

    def select(self, query: str, *, context: dict | None = None) -> SelectionResult:
        """Run all active channels, fuse scores, return best skill."""
        start = time.monotonic()

        channel_scores: dict[str, dict[str, float]] = {}

        # Channel 1: Keyword scoring
        channel_scores["keyword"] = self._score_keywords(query)

        # Channel 3: Component tags
        channel_scores["component"] = self._score_component_tags(query)

        # Channel 4: Historical co-occurrence
        channel_scores["historical"] = self._score_historical(query)

        # Channel 2: Alert taxonomy
        channel_scores["taxonomy"] = self._score_alert_taxonomy(query)

        # Channel 5: Temporal context
        channel_scores["temporal"] = self._score_temporal(query)

        # Channel 6: Semantic embedding (optional)
        channel_scores["semantic"] = self._score_semantic_embedding(query)

        # Fuse scores
        fused = self._fuse_scores(channel_scores)

        # Apply threshold
        threshold = self._compute_threshold(context)

        # Find best skill
        if fused:
            best_name = max(
                fused,
                key=lambda n: (
                    fused[n],
                    self._skills[n].priority if n in self._skills else 0,
                ),
            )
            best_score = fused[best_name]
        else:
            best_name = "sre"
            best_score = 0.0

        elapsed_ms = int((time.monotonic() - start) * 1000)

        if best_score >= threshold and best_name in self._skills:
            return SelectionResult(
                skill_name=best_name,
                fused_scores=fused,
                channel_scores=channel_scores,
                threshold_used=threshold,
                selection_ms=elapsed_ms,
                source="orca",
            )

        # Below threshold — fallback
        # Still return the best score even if below threshold
        return SelectionResult(
            skill_name=best_name if best_name in self._skills else "sre",
            fused_scores=fused,
            channel_scores=channel_scores,
            threshold_used=threshold,
            selection_ms=elapsed_ms,
            source="fallback",
        )

    def _score_keywords(self, query: str) -> dict[str, float]:
        """Channel 1: Keyword scoring — ported from classify_query logic."""
        q = query.lower()
        raw_scores: dict[str, int] = {}

        # Direct skill name match
        for skill_name in self._skills:
            variants = [
                skill_name,
                skill_name.replace("_", " "),
                skill_name.replace("_", "-"),
            ]
            for variant in variants:
                if variant in q:
                    raw_scores[skill_name] = raw_scores.get(skill_name, 0) + len(variant) * 2
                    break

        # Keyword index match
        for kw, skill_name, kw_len in self._keyword_index:
            if kw_len < 4:
                if re.search(r"\b" + re.escape(kw) + r"\b", q):
                    raw_scores[skill_name] = raw_scores.get(skill_name, 0) + kw_len
            elif kw in q:
                raw_scores[skill_name] = raw_scores.get(skill_name, 0) + kw_len

        # Normalize to 0.0-1.0
        if not raw_scores:
            return {}
        max_score = max(raw_scores.values())
        if max_score == 0:
            return {}
        return {name: score / max_score for name, score in raw_scores.items()}

    def _score_alert_taxonomy(self, query: str) -> dict[str, float]:
        """Channel 2: Match alert names and scanner categories to skills."""
        q = query.lower()
        scores: dict[str, float] = {}

        # Check alert taxonomy prefixes
        for prefix, skill in _ALERT_TAXONOMY.items():
            if prefix in q:
                scores[skill] = max(scores.get(skill, 0), 0.8)

        # Check scanner categories
        for category, skill in _CATEGORY_SKILL.items():
            if category in q:
                scores[skill] = max(scores.get(skill, 0), 0.7)

        return scores

    def _score_component_tags(self, query: str) -> dict[str, float]:
        """Channel 3: Extract K8s resource types from query, match against skill categories."""
        matches = _K8S_RESOURCES.findall(query.lower())
        if not matches:
            return {}

        # Collect categories from matched resources
        matched_categories: set[str] = set()
        for resource in matches:
            cats = _RESOURCE_CATEGORY_MAP.get(resource.lower(), [])
            matched_categories.update(cats)

        if not matched_categories:
            return {}

        # Score each skill by category overlap
        scores: dict[str, float] = {}
        for skill_name, skill in self._skills.items():
            if not skill.categories:
                continue
            skill_cats = set(skill.categories)
            overlap = len(matched_categories & skill_cats)
            if overlap > 0:
                scores[skill_name] = overlap / max(len(matched_categories), len(skill_cats))

        return scores

    def _score_historical(self, query: str) -> dict[str, float]:
        """Channel 4: Historical co-occurrence — which skills handled similar queries."""
        global _historical_cache, _historical_cache_ts

        now = time.time()
        if _historical_cache is not None and now - _historical_cache_ts < _HISTORICAL_CACHE_TTL:
            # Use cached token→skill mapping
            return self._match_historical_tokens(query)

        try:
            from .db import get_database

            db = get_database()

            # Build token→skill frequency map from recent successful skill_usage
            rows = db.fetchall(
                "SELECT skill_name, query_summary "
                "FROM skill_usage "
                "WHERE feedback IS NULL OR feedback != 'negative' "
                "AND timestamp > NOW() - INTERVAL '7 days' "
                "ORDER BY timestamp DESC "
                "LIMIT 200"
            )
            if not rows:
                return {}

            from .tool_predictor import extract_tokens

            # Build token→skill frequency map
            token_skill_freq: dict[str, dict[str, int]] = {}
            for row in rows:
                tokens = extract_tokens(row.get("query_summary", ""))
                skill = row["skill_name"]
                for token in tokens[:10]:  # limit tokens per query
                    if token not in token_skill_freq:
                        token_skill_freq[token] = {}
                    token_skill_freq[token][skill] = token_skill_freq[token].get(skill, 0) + 1

            _historical_cache = token_skill_freq
            _historical_cache_ts = now
            return self._match_historical_tokens(query)

        except Exception:
            logger.debug("Historical scoring failed", exc_info=True)
            return {}

    def _match_historical_tokens(self, query: str) -> dict[str, float]:
        """Match query tokens against cached historical token→skill map."""
        if not _historical_cache:
            return {}

        from .tool_predictor import extract_tokens

        tokens = extract_tokens(query)
        if not tokens:
            return {}

        skill_scores: dict[str, float] = {}
        for token in tokens:
            skill_freq = _historical_cache.get(token, {})
            for skill, freq in skill_freq.items():
                skill_scores[skill] = skill_scores.get(skill, 0) + freq

        if not skill_scores:
            return {}

        # Normalize to 0.0-1.0
        max_score = max(skill_scores.values())
        return {k: v / max_score for k, v in skill_scores.items()}

    def _score_temporal(self, query: str) -> dict[str, float]:
        """Channel 5: Detect temporal context (recent changes) and boost relevant skills."""
        q = query.lower()
        has_temporal = any(kw in q for kw in _TEMPORAL_KEYWORDS)
        if not has_temporal:
            return {}

        # Boost skills with operations-related categories
        scores: dict[str, float] = {}
        for skill_name, skill in self._skills.items():
            cats = set(skill.categories) if skill.categories else set()
            if cats & {"operations", "workloads", "diagnostics"}:
                scores[skill_name] = 0.6
        return scores

    def _score_semantic_embedding(self, query: str) -> dict[str, float]:
        """Channel 6 (optional): Semantic similarity via embeddings.

        Requires PULSE_AGENT_EMBEDDING_CHANNEL=1 to activate.
        Uses cached skill description embeddings for cosine similarity.
        """
        if not os.environ.get("PULSE_AGENT_EMBEDDING_CHANNEL"):
            return {}

        # Stub: embedding infrastructure not yet deployed.
        # When enabled, this will:
        # 1. Embed query using sentence-transformers or Claude API
        # 2. Compare against pre-cached skill description embeddings
        # 3. Return cosine similarity scores per skill
        logger.debug("Semantic embedding channel: stub (not yet implemented)")
        return {}

    def _fuse_scores(self, channel_scores: dict[str, dict[str, float]]) -> dict[str, float]:
        """Weighted sum fusion across all channels."""
        fused: dict[str, float] = {}
        all_skills = set()
        for scores in channel_scores.values():
            all_skills.update(scores.keys())

        for skill_name in all_skills:
            total = 0.0
            for channel_name, scores in channel_scores.items():
                weight = self._weights.get(channel_name, 0.0)
                score = scores.get(skill_name, 0.0)
                total += weight * score
            fused[skill_name] = round(total, 4)

        # Re-rank by skill priority (tiebreaker)
        # Already handled in select() via the max() key function

        return fused

    def detect_conflicts(self, tools_offered: list[str]) -> list[dict]:
        """Check for conflicting tools in the offered set."""
        conflicts: list[dict] = []
        tool_set = set(tools_offered)
        for a, b in HARD_CONFLICTS:
            if a in tool_set and b in tool_set:
                conflicts.append({"type": "hard", "pair": [a, b], "action": "remove_lower_scored"})
        for a, b in SOFT_CONFLICTS:
            if a in tool_set and b in tool_set:
                conflicts.append({"type": "soft", "pair": [a, b], "action": "warn_agent"})
        return conflicts

    def _compute_threshold(self, context: dict | None) -> float:
        """Dynamic threshold based on incident context."""
        base = 0.45
        if not context:
            return base

        priority = context.get("incident_priority")
        if priority == "P1":
            base = 0.35
        elif priority == "P3":
            base = 0.60

        if context.get("max_fused_score", 1.0) < 0.3:
            base = max(base - 0.10, 0.25)

        if context.get("recent_similar"):
            base = min(base + 0.10, 0.70)

        return base


def record_selection_outcome(
    *,
    session_id: str,
    query_summary: str,
    result: SelectionResult,
    tools_called: list[str] | None = None,
    tools_offered: list[str] | None = None,
    skill_overridden: str | None = None,
) -> None:
    """Log selection outcome to skill_selection_log. Fire-and-forget."""
    try:
        import json

        from .db import get_database

        db = get_database()

        # Detect missed retrievals
        missing = []
        if tools_called and tools_offered:
            offered_set = set(tools_offered)
            missing = [t for t in tools_called if t not in offered_set]

        db.execute(
            "INSERT INTO skill_selection_log "
            "(session_id, query_summary, channel_scores, fused_scores, selected_skill, "
            "threshold_used, conflicts_detected, skill_overridden, tools_requested_missing, "
            "selection_ms, channel_weights) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                query_summary[:200],
                json.dumps(result.channel_scores),
                json.dumps(result.fused_scores),
                result.skill_name,
                result.threshold_used,
                json.dumps(result.conflicts) if result.conflicts else None,
                skill_overridden,
                missing or None,
                result.selection_ms,
                json.dumps(DEFAULT_WEIGHTS),
            ),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to record selection outcome", exc_info=True)
