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
    secondary_skill: str | None = None


@dataclass
class TemporalSignal:
    """Cluster state signals for temporal channel scoring."""

    recent_deploys: list[dict]
    time_of_day: str  # "business_hours" | "off_hours" | "weekend"
    active_incidents: int


_temporal_signal_cache: dict = {"signal": None, "time": 0.0}


def _build_temporal_signal(cache_ttl: int = 60) -> TemporalSignal:
    now = time.monotonic()
    cached = _temporal_signal_cache.get("signal")
    cached_time = _temporal_signal_cache.get("time", 0.0)
    if cached is not None and (now - cached_time) < cache_ttl:
        return cached

    recent_deploys: list[dict] = []
    active_incidents = 0

    try:
        from .db import get_database

        db = get_database()
        row = db.fetchone(
            "SELECT COUNT(*) as cnt FROM findings "
            "WHERE category = 'audit_deployment' "
            "AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '30 minutes')::BIGINT * 1000"
        )
        deploy_count = row["cnt"] if row else 0
        if deploy_count > 0:
            recent_deploys = [{"count": deploy_count}]

        inc_row = db.fetchone("SELECT COUNT(*) as cnt FROM findings WHERE resolved = 0 AND category NOT LIKE 'audit_%'")
        active_incidents = inc_row["cnt"] if inc_row else 0
    except Exception:
        logger.debug("Temporal signal: data source unavailable", exc_info=True)

    from datetime import UTC, datetime

    now_utc = datetime.now(UTC)
    hour = now_utc.hour
    weekday = now_utc.weekday()
    if weekday >= 5:
        tod = "weekend"
    elif hour < 6 or hour > 22:
        tod = "off_hours"
    else:
        tod = "business_hours"

    signal = TemporalSignal(
        recent_deploys=recent_deploys,
        time_of_day=tod,
        active_incidents=active_incidents,
    )
    _temporal_signal_cache["signal"] = signal
    _temporal_signal_cache["time"] = now
    return signal


# Default channel weights (sum to 1.0)
DEFAULT_WEIGHTS: dict[str, float] = {
    "keyword": 0.25,
    "component": 0.20,
    "historical": 0.20,
    "taxonomy": 0.10,
    "temporal": 0.10,  # state-aware: cluster changes + time-of-day
    "semantic": 0.15,  # TF-IDF cosine similarity against skill descriptions + keywords
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
        self._skill_token_cache: dict[str, set[str]] | None = None

        # Load learned weights if available
        try:
            from .selector_learning import load_learned_weights

            learned = load_learned_weights()
            if learned:
                self._weights.update(learned)
                logger.info("Using learned channel weights")
        except Exception:
            pass

    def set_weights(self, weights: dict[str, float]) -> None:
        """Replace channel weights. Use this instead of mutating _weights directly."""
        self._weights = dict(weights)

    def invalidate_skill_token_cache(self) -> None:
        """Clear the semantic embedding cache so it rebuilds on next query."""
        self._skill_token_cache = None

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

        # Channel 6: Semantic embedding
        channel_scores["semantic"] = self._score_semantic_embedding(query)

        # Inject SLO context if available
        try:
            from .slo_registry import get_slo_registry

            slo_context = get_slo_registry().get_context_for_selector()
            if slo_context and context is not None:
                context["slo_alerts"] = slo_context
        except Exception:
            pass

        # Fuse scores
        fused = self._fuse_scores(channel_scores)

        # Apply threshold
        threshold = self._compute_threshold(context)

        # Resolve conflicts before selecting
        # Get top skills by score
        top_skills = sorted(fused.keys(), key=lambda k: -fused.get(k, 0))[:5] if fused else []
        conflicts = self.detect_conflicts([], selected_skills=top_skills)

        # Hard skill conflicts: drop the lower-scored skill from fused scores
        for conflict in conflicts:
            if conflict["type"] == "skill_conflict":
                a, b = conflict["pair"]
                if a in fused and b in fused:
                    # Drop the lower-scored one
                    if fused.get(a, 0) < fused.get(b, 0):
                        fused[a] = 0.0
                        logger.info("Hard conflict: dropped '%s' (lower score) vs '%s'", a, b)
                    else:
                        fused[b] = 0.0
                        logger.info("Hard conflict: dropped '%s' (lower score) vs '%s'", b, a)

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

        # Detect secondary skill via score gap
        secondary = None
        try:
            from .config import get_settings

            _ms = get_settings()
            if _ms.multi_skill and fused:
                sorted_skills = sorted(fused.items(), key=lambda x: x[1], reverse=True)
                if len(sorted_skills) >= 2:
                    gap = sorted_skills[0][1] - sorted_skills[1][1]
                    if gap <= _ms.multi_skill_threshold:
                        candidate = sorted_skills[1][0]
                        best_obj = self._skills.get(best_name)
                        cand_obj = self._skills.get(candidate)
                        if best_obj and cand_obj:
                            has_conflict = candidate in (best_obj.conflicts_with or []) or best_name in (
                                cand_obj.conflicts_with or []
                            )
                            if not has_conflict:
                                secondary = candidate
        except Exception:
            pass

        if best_score >= threshold and best_name in self._skills:
            result = SelectionResult(
                skill_name=best_name,
                fused_scores=fused,
                channel_scores=channel_scores,
                threshold_used=threshold,
                conflicts=conflicts,
                selection_ms=elapsed_ms,
                source="orca",
                secondary_skill=secondary,
            )
            self.last_result = result
            return result

        # Below threshold — fallback
        result = SelectionResult(
            skill_name=best_name if best_name in self._skills else "sre",
            fused_scores=fused,
            channel_scores=channel_scores,
            threshold_used=threshold,
            conflicts=conflicts,
            selection_ms=elapsed_ms,
            source="fallback",
            secondary_skill=secondary,
        )
        self.last_result = result
        return result

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
        """Channel 2: Match alert names and scanner categories to skills.

        Uses skill-defined alert_triggers first, then falls back to hardcoded maps.
        """
        q = query.lower()
        scores: dict[str, float] = {}

        # Skill-defined alert triggers (highest priority)
        for skill_name, skill in self._skills.items():
            for trigger in skill.alert_triggers:
                if trigger.lower() in q:
                    scores[skill_name] = max(scores.get(skill_name, 0), 0.9)

        # Fallback: hardcoded alert taxonomy prefixes
        for prefix, skill in _ALERT_TAXONOMY.items():
            if prefix in q and skill not in scores:
                scores[skill] = max(scores.get(skill, 0), 0.7)

        # Fallback: scanner categories
        for category, skill in _CATEGORY_SKILL.items():
            if category in q and skill not in scores:
                scores[skill] = max(scores.get(skill, 0), 0.6)

        return scores

    def _score_component_tags(self, query: str) -> dict[str, float]:
        """Channel 3: Extract K8s resource types from query, match against skill data.

        Uses skill-defined cluster_components first, then category overlap.
        """
        matches = _K8S_RESOURCES.findall(query.lower())
        if not matches:
            return {}

        matched_resources = {m.lower() for m in matches}

        # Score by skill-defined cluster_components (direct match, highest signal)
        scores: dict[str, float] = {}
        for skill_name, skill in self._skills.items():
            if skill.cluster_components:
                skill_comps = {c.lower() for c in skill.cluster_components}
                overlap = len(matched_resources & skill_comps)
                if overlap > 0:
                    scores[skill_name] = max(
                        scores.get(skill_name, 0),
                        overlap / max(len(matched_resources), len(skill_comps)),
                    )

        # Fallback: category overlap from resource→category map
        matched_categories: set[str] = set()
        for resource in matches:
            cats = _RESOURCE_CATEGORY_MAP.get(resource.lower(), [])
            matched_categories.update(cats)

        if matched_categories:
            for skill_name, skill in self._skills.items():
                if skill_name in scores:
                    continue  # already scored by cluster_components
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
                "WHERE (feedback IS NULL OR feedback != 'negative') "
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

    def _get_temporal_signal(self) -> TemporalSignal:
        from .config import get_settings

        ttl = get_settings().temporal_cache_ttl
        return _build_temporal_signal(cache_ttl=ttl)

    def _score_temporal(self, query: str) -> dict[str, float]:
        """Channel 5: State-aware temporal context — cluster changes + time signals."""
        scores: dict[str, float] = {}
        signal = self._get_temporal_signal()

        q = query.lower()
        has_temporal_text = any(kw in q for kw in _TEMPORAL_KEYWORDS)

        if signal.recent_deploys:
            for skill_name, skill in self._skills.items():
                cats = set(skill.categories) if skill.categories else set()
                if cats & {"operations", "workloads", "diagnostics"}:
                    scores[skill_name] = 0.8
            scores["postmortem"] = max(scores.get("postmortem", 0), 0.15)

        if signal.active_incidents > 0:
            scores["sre"] = max(scores.get("sre", 0), 0.2)

        if has_temporal_text:
            for skill_name, skill in self._skills.items():
                cats = set(skill.categories) if skill.categories else set()
                if cats & {"operations", "workloads", "diagnostics"}:
                    scores[skill_name] = max(scores.get(skill_name, 0), 0.7)

        if signal.time_of_day in ("off_hours", "weekend"):
            scores["slo-management"] = max(scores.get("slo-management", 0), 0.1)
            if not scores:
                scores["sre"] = 0.4
                scores["security"] = 0.3

        return scores

    def _score_semantic_embedding(self, query: str) -> dict[str, float]:
        """Channel 6: Semantic similarity via TF-IDF cosine.

        Compares query tokens against cached skill description + keyword tokens.
        Lightweight — no external model needed.
        """
        import math

        # Build skill token sets (cached on first call)
        if self._skill_token_cache is None:
            self._skill_token_cache = {}
            for skill_name, skill in self._skills.items():
                tokens = set()
                # Description tokens
                for word in skill.description.lower().split():
                    if len(word) >= 3:
                        tokens.add(word)
                # Keyword tokens
                for kw in skill.keywords:
                    tokens.add(kw.lower())
                # Category tokens
                for cat in skill.categories:
                    tokens.add(cat.lower())
                self._skill_token_cache[skill_name] = tokens

        # Tokenize query
        query_tokens = {w for w in query.lower().split() if len(w) >= 3}
        if not query_tokens:
            return {}

        # Cosine-like similarity: |intersection| / sqrt(|A| * |B|)
        scores: dict[str, float] = {}
        for skill_name, skill_tokens in self._skill_token_cache.items():
            if not skill_tokens:
                continue
            overlap = len(query_tokens & skill_tokens)
            if overlap > 0:
                denom = math.sqrt(len(query_tokens) * len(skill_tokens))
                scores[skill_name] = overlap / denom if denom > 0 else 0.0

        # Normalize to 0-1
        if scores:
            max_score = max(scores.values())
            if max_score > 0:
                scores = {k: v / max_score for k, v in scores.items()}

        return scores

    def _fuse_scores(self, channel_scores: dict[str, dict[str, float]]) -> dict[str, float]:
        """Weighted sum fusion across all channels, with post-fusion multipliers."""
        fused: dict[str, float] = {}
        all_skills: set[str] = set()
        for scores in channel_scores.values():
            all_skills.update(scores.keys())

        for skill_name in all_skills:
            total = 0.0
            for channel_name, scores in channel_scores.items():
                weight = self._weights.get(channel_name, 0.0)
                score = scores.get(skill_name, 0.0)
                total += weight * score
            fused[skill_name] = round(total, 4)

        # Post-fusion multipliers
        for skill_name in list(fused.keys()):
            skill = self._skills.get(skill_name)
            if not skill:
                continue

            # 1. success_boost — historical success rate from skill_usage
            success_rate = self._get_skill_success_rate(skill_name)
            if success_rate is not None:
                fused[skill_name] *= success_rate**2  # squared for stronger signal

            # 2. review_boost — reviewed skills get a 20% boost over unreviewed
            if not skill.reviewed:
                fused[skill_name] *= 0.8  # 20% penalty for unreviewed AI-generated skills

            fused[skill_name] = round(fused[skill_name], 4)

        return fused

    def _get_skill_success_rate(self, skill_name: str) -> float | None:
        """Get historical success rate for a skill from skill_usage table.

        Returns None if insufficient data (<5 invocations).
        """
        try:
            from .db import get_database

            db = get_database()
            row = db.fetchone(
                "SELECT COUNT(*) FILTER (WHERE feedback IS NULL OR feedback != 'negative') AS good, "
                "COUNT(*) AS total "
                "FROM skill_usage "
                "WHERE skill_name = %s "
                "AND timestamp > NOW() - INTERVAL '30 days'",
                (skill_name,),
            )
            if not row or row["total"] < 5:
                return None
            return row["good"] / max(row["total"], 1)
        except Exception:
            return None

    def detect_conflicts(self, tools_offered: list[str], selected_skills: list[str] | None = None) -> list[dict]:
        """Check for conflicting tools and skills."""
        conflicts: list[dict] = []

        # Tool-level conflicts (hardcoded)
        tool_set = set(tools_offered)
        for a, b in HARD_CONFLICTS:
            if a in tool_set and b in tool_set:
                conflicts.append({"type": "hard", "pair": [a, b], "action": "remove_lower_scored"})
        for a, b in SOFT_CONFLICTS:
            if a in tool_set and b in tool_set:
                conflicts.append({"type": "soft", "pair": [a, b], "action": "warn_agent"})

        # Skill-level conflicts (from skill.conflicts_with)
        if selected_skills:
            for skill_name in selected_skills:
                skill = self._skills.get(skill_name)
                if skill and skill.conflicts_with:
                    for conflict in skill.conflicts_with:
                        if conflict in selected_skills:
                            conflicts.append(
                                {
                                    "type": "skill_conflict",
                                    "pair": [skill_name, conflict],
                                    "action": "warn_agent",
                                }
                            )

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
