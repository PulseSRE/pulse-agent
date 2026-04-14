"""Multi-signal skill selector — ORCA architecture.

Replaces keyword-only routing with 5-channel fusion (3 active in this task,
2 added later). Each channel scores every skill independently, then scores
are fused with weighted sum and re-ranked.

Channels:
1. Keyword scoring (ported from classify_query)
3. Component tags (K8s resource type matching)
4. Historical co-occurrence (from skill_usage table)
"""

from __future__ import annotations

import logging
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
_historical_cache: dict[str, dict[str, float]] | None = None
_historical_cache_ts: float = 0
_HISTORICAL_CACHE_TTL = 300  # 5 minutes


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

        # Channels 2 and 5 return empty for now (added in Task 4)
        channel_scores["taxonomy"] = {}
        channel_scores["temporal"] = {}

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
        """Channel 4: Historical co-occurrence from skill_usage table."""
        global _historical_cache, _historical_cache_ts

        now = time.time()
        if _historical_cache is not None and now - _historical_cache_ts < _HISTORICAL_CACHE_TTL:
            return dict(_historical_cache)

        try:
            from .db import get_database
            from .tool_predictor import extract_tokens

            db = get_database()
            tokens = extract_tokens(query)
            if not tokens:
                return {}

            # Query skill_usage for skills that handled similar queries
            rows = db.fetchall(
                "SELECT skill_name, COUNT(*) as cnt "
                "FROM skill_usage "
                "WHERE feedback IS NULL OR feedback != 'negative' "
                "GROUP BY skill_name "
                "ORDER BY cnt DESC "
                "LIMIT 20"
            )
            if not rows:
                return {}

            total = sum(r["cnt"] for r in rows)
            scores = {r["skill_name"]: r["cnt"] / total for r in rows}

            _historical_cache = scores
            _historical_cache_ts = now
            return dict(scores)

        except Exception:
            logger.debug("Historical scoring failed", exc_info=True)
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

    def _compute_threshold(self, context: dict | None) -> float:
        """Dynamic threshold based on incident context."""
        base = 0.45
        if not context:
            return base

        priority = context.get("incident_priority")
        if priority == "P1":
            return 0.35
        elif priority == "P3":
            return 0.60

        return base
