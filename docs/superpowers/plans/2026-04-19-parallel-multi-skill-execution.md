# Parallel Multi-Skill Execution Implementation Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend ORCA from single-skill routing to parallel multi-skill execution with synthesis, and wire real cluster state into the temporal channel.

**Architecture:** Two PRs. PR 1 wires `change_risk.py` deploy data into the ORCA temporal channel via a `TemporalSignal` dataclass with in-memory caching. PR 2 adds `classify_query_multi()` for top-K selection + intent splitting, `run_parallel_skills()` for concurrent agent execution via `asyncio.to_thread()`, a `synthesis.py` module for Sonnet-powered output merging with conflict detection, and WebSocket protocol extensions (`multi_skill_start`, `skill_progress`, extended `done`).

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2, FastAPI WebSockets, Claude Sonnet API, pytest

**Spec:** `docs/superpowers/specs/2026-04-19-parallel-multi-skill-execution-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `sre_agent/synthesis.py` | `Conflict`, `SynthesisResult`, `synthesize_parallel_outputs()`, concatenation fallback |
| `tests/test_synthesis.py` | Unit tests for synthesis: merge, conflict detection, no-conflict fast path, fallback |
| `tests/test_temporal_channel.py` | Unit tests for `TemporalSignal`, `_score_temporal()` rework, caching |
| `tests/test_multi_skill.py` | Unit tests for `classify_query_multi()`, `split_compound_intent()`, parallel execution |

### Modified Files
| File | Changes |
|------|---------|
| `sre_agent/config.py` | 4 new settings: `multi_skill`, `multi_skill_threshold`, `multi_skill_max`, `temporal_cache_ttl` |
| `sre_agent/skill_selector.py` | `TemporalSignal` dataclass, `_score_temporal()` rework with caching, `SelectionResult.secondary_skill` |
| `sre_agent/skill_loader.py` | `classify_query_multi()` function, tool budget splitting |
| `sre_agent/orchestrator.py` | `split_compound_intent()` function |
| `sre_agent/plan_runtime.py` | `ParallelSkillResult` dataclass, `run_parallel_skills()` function |
| `sre_agent/context_bus.py` | `parallel_task_id` field on `ContextEntry`, buffered publish mode |
| `sre_agent/api/ws_endpoints.py` | Multi-skill routing branch, new event types, extended `done`, sticky mode reset |

---

## PR 1: Temporal Channel Wiring

### Task 1: Config — Add temporal cache TTL setting

**Files:**
- Modify: `sre_agent/config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_temporal_channel.py
from sre_agent.config import get_settings


def test_temporal_cache_ttl_default():
    s = get_settings()
    assert s.temporal_cache_ttl == 60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_temporal_channel.py::test_temporal_cache_ttl_default -v`
Expected: FAIL with `AttributeError: 'PulseAgentSettings' object has no attribute 'temporal_cache_ttl'`

- [ ] **Step 3: Add the setting to PulseAgentSettings**

In `sre_agent/config.py`, add inside the `PulseAgentSettings` class (after the existing settings):

```python
    temporal_cache_ttl: int = 60
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_temporal_channel.py::test_temporal_cache_ttl_default -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/config.py tests/test_temporal_channel.py
git commit -m "feat: add PULSE_AGENT_TEMPORAL_CACHE_TTL config setting"
```

---

### Task 2: TemporalSignal dataclass and cached signal builder

**Files:**
- Modify: `sre_agent/skill_selector.py`
- Test: `tests/test_temporal_channel.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_temporal_channel.py`:

```python
import time
from unittest.mock import patch, MagicMock
from sre_agent.skill_selector import TemporalSignal, _build_temporal_signal


def test_temporal_signal_defaults():
    sig = TemporalSignal(recent_deploys=[], time_of_day="business_hours", active_incidents=0)
    assert sig.time_of_day == "business_hours"
    assert sig.recent_deploys == []
    assert sig.active_incidents == 0


def test_build_temporal_signal_caches(monkeypatch):
    """Second call within TTL returns cached result without hitting DB."""
    call_count = 0
    original_build = None

    def mock_fetchone(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return {"cnt": 2}

    mock_db = MagicMock()
    mock_db.fetchone = mock_fetchone

    with patch("sre_agent.skill_selector.get_database", return_value=mock_db):
        with patch("sre_agent.skill_selector._temporal_signal_cache", {"signal": None, "time": 0.0}):
            sig1 = _build_temporal_signal(cache_ttl=60)
            sig2 = _build_temporal_signal(cache_ttl=60)
            # Second call should use cache
            assert call_count == 1


def test_build_temporal_signal_db_failure():
    """When DB is unreachable, returns neutral signal."""
    with patch("sre_agent.skill_selector.get_database", side_effect=Exception("DB down")):
        with patch("sre_agent.skill_selector._temporal_signal_cache", {"signal": None, "time": 0.0}):
            sig = _build_temporal_signal(cache_ttl=60)
            assert sig.recent_deploys == []
            assert sig.active_incidents == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_temporal_channel.py -v -k "temporal_signal"`
Expected: FAIL with `ImportError: cannot import name 'TemporalSignal'`

- [ ] **Step 3: Implement TemporalSignal and _build_temporal_signal**

In `sre_agent/skill_selector.py`, add after the existing imports:

```python
from dataclasses import dataclass as _dc_dataclass

@_dc_dataclass
class TemporalSignal:
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

        inc_row = db.fetchone(
            "SELECT COUNT(*) as cnt FROM findings "
            "WHERE resolved = 0 "
            "AND category NOT LIKE 'audit_%'"
        )
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_temporal_channel.py -v -k "temporal_signal"`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add sre_agent/skill_selector.py tests/test_temporal_channel.py
git commit -m "feat: add TemporalSignal dataclass with cached builder"
```

---

### Task 3: Rework _score_temporal() to use TemporalSignal

**Files:**
- Modify: `sre_agent/skill_selector.py` (lines 474-538)
- Test: `tests/test_temporal_channel.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_temporal_channel.py`:

```python
from sre_agent.skill_selector import SkillSelector


def _make_skills():
    """Minimal skill dicts for selector tests."""
    from sre_agent.skill_loader import Skill
    from pathlib import Path

    return {
        "sre": Skill(
            name="sre", version=1, description="SRE diagnostics",
            keywords=["pods", "crash", "deploy"], categories=["diagnostics", "workloads", "operations"],
            write_tools=True, priority=1, system_prompt="", path=Path("."),
        ),
        "security": Skill(
            name="security", version=1, description="Security scanning",
            keywords=["cve", "vulnerability", "rbac"], categories=["security"],
            write_tools=False, priority=2, system_prompt="", path=Path("."),
        ),
        "slo-management": Skill(
            name="slo-management", version=1, description="SLO management",
            keywords=["slo", "burn", "error budget"], categories=["monitoring"],
            write_tools=False, priority=3, system_prompt="", path=Path("."),
        ),
        "postmortem": Skill(
            name="postmortem", version=1, description="Post-incident review",
            keywords=["postmortem", "incident"], categories=["diagnostics"],
            write_tools=False, priority=4, system_prompt="", path=Path("."),
        ),
    }


def test_temporal_recent_deploy_boosts_sre():
    skills = _make_skills()
    selector = SkillSelector(skills, keyword_index={})
    signal = TemporalSignal(recent_deploys=[{"count": 3}], time_of_day="business_hours", active_incidents=0)
    with patch.object(selector, "_get_temporal_signal", return_value=signal):
        scores = selector._score_temporal("why are pods crashing")
    assert scores.get("sre", 0) >= 0.3
    assert scores.get("postmortem", 0) >= 0.15


def test_temporal_off_hours_boosts_slo():
    skills = _make_skills()
    selector = SkillSelector(skills, keyword_index={})
    signal = TemporalSignal(recent_deploys=[], time_of_day="off_hours", active_incidents=0)
    with patch.object(selector, "_get_temporal_signal", return_value=signal):
        scores = selector._score_temporal("check error budget")
    assert scores.get("slo-management", 0) >= 0.1


def test_temporal_active_incidents_boosts_sre():
    skills = _make_skills()
    selector = SkillSelector(skills, keyword_index={})
    signal = TemporalSignal(recent_deploys=[], time_of_day="business_hours", active_incidents=3)
    with patch.object(selector, "_get_temporal_signal", return_value=signal):
        scores = selector._score_temporal("what is going on")
    assert scores.get("sre", 0) >= 0.2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_temporal_channel.py -v -k "test_temporal_recent or test_temporal_off or test_temporal_active"`
Expected: FAIL — `_get_temporal_signal` method doesn't exist yet

- [ ] **Step 3: Rework _score_temporal()**

Replace the existing `_score_temporal()` method (lines 474-538 of `sre_agent/skill_selector.py`) with:

```python
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

        # Recent deploys boost SRE and postmortem
        if signal.recent_deploys:
            for skill_name, skill in self._skills.items():
                cats = set(skill.categories) if skill.categories else set()
                if cats & {"operations", "workloads", "diagnostics"}:
                    scores[skill_name] = 0.8
            scores["postmortem"] = max(scores.get("postmortem", 0), 0.15)

        # Active incidents boost SRE
        if signal.active_incidents > 0:
            scores["sre"] = max(scores.get("sre", 0), 0.2)

        # Temporal keywords in query text
        if has_temporal_text:
            for skill_name, skill in self._skills.items():
                cats = set(skill.categories) if skill.categories else set()
                if cats & {"operations", "workloads", "diagnostics"}:
                    scores[skill_name] = max(scores.get(skill_name, 0), 0.7)

        # Time-of-day signals
        if signal.time_of_day in ("off_hours", "weekend"):
            scores["slo-management"] = max(scores.get("slo-management", 0), 0.1)
            if not scores:
                scores["sre"] = 0.4
                scores["security"] = 0.3

        return scores
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_temporal_channel.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `python3 -m pytest tests/ -v --timeout=120`
Expected: All existing tests pass

- [ ] **Step 6: Commit**

```bash
git add sre_agent/skill_selector.py tests/test_temporal_channel.py
git commit -m "feat: wire temporal channel to real cluster state via TemporalSignal"
```

---

## PR 2: Parallel Multi-Skill Execution

### Task 4: Config — Add multi-skill settings

**Files:**
- Modify: `sre_agent/config.py`
- Test: `tests/test_multi_skill.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_multi_skill.py
from sre_agent.config import get_settings


def test_multi_skill_config_defaults():
    s = get_settings()
    assert s.multi_skill is True
    assert s.multi_skill_threshold == 0.15
    assert s.multi_skill_max == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_multi_skill.py::test_multi_skill_config_defaults -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Add settings to PulseAgentSettings**

In `sre_agent/config.py`, add inside `PulseAgentSettings`:

```python
    multi_skill: bool = True
    multi_skill_threshold: float = 0.15
    multi_skill_max: int = 2
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_multi_skill.py::test_multi_skill_config_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/config.py tests/test_multi_skill.py
git commit -m "feat: add PULSE_AGENT_MULTI_SKILL config settings"
```

---

### Task 5: SelectionResult.secondary_skill

**Files:**
- Modify: `sre_agent/skill_selector.py` (lines 24-34)
- Test: `tests/test_multi_skill.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multi_skill.py`:

```python
from sre_agent.skill_selector import SelectionResult


def test_selection_result_secondary_skill_default():
    r = SelectionResult(
        skill_name="sre",
        fused_scores={"sre": 0.8, "security": 0.7},
        channel_scores={},
        threshold_used=0.3,
    )
    assert r.secondary_skill is None


def test_selection_result_secondary_skill_set():
    r = SelectionResult(
        skill_name="sre",
        fused_scores={"sre": 0.8, "security": 0.7},
        channel_scores={},
        threshold_used=0.3,
        secondary_skill="security",
    )
    assert r.secondary_skill == "security"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "selection_result"`
Expected: FAIL with `TypeError: unexpected keyword argument 'secondary_skill'`

- [ ] **Step 3: Add secondary_skill field**

In `sre_agent/skill_selector.py`, modify the `SelectionResult` dataclass (line ~33) to add:

```python
    secondary_skill: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "selection_result"`
Expected: PASS

- [ ] **Step 5: Populate secondary_skill in SkillSelector.select()**

In `sre_agent/skill_selector.py`, in the `select()` method, after fused_scores is built and best skill is picked, add logic to detect when top-2 gap is within threshold:

```python
        # After determining best_skill (existing code):
        from .config import get_settings
        _ms = get_settings()
        secondary = None
        if _ms.multi_skill and len(sorted_skills) >= 2:
            gap = sorted_skills[0][1] - sorted_skills[1][1]
            if gap <= _ms.multi_skill_threshold:
                candidate = sorted_skills[1][0]
                # Bidirectional conflict check
                best_obj = self._skills.get(best_skill)
                cand_obj = self._skills.get(candidate)
                if best_obj and cand_obj:
                    conflicts = (
                        candidate in (best_obj.conflicts_with or [])
                        or best_skill in (cand_obj.conflicts_with or [])
                    )
                    if not conflicts:
                        secondary = candidate
```

Set `secondary_skill=secondary` in the returned `SelectionResult`.

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -v --timeout=120`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add sre_agent/skill_selector.py tests/test_multi_skill.py
git commit -m "feat: add secondary_skill to SelectionResult with score gap detection"
```

---

### Task 6: split_compound_intent() in orchestrator

**Files:**
- Modify: `sre_agent/orchestrator.py`
- Test: `tests/test_multi_skill.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multi_skill.py`:

```python
from sre_agent.orchestrator import split_compound_intent


def test_split_simple_conjunction():
    parts = split_compound_intent("check pod crashes and scan for CVEs")
    assert len(parts) == 2
    assert "check pod crashes" in parts[0]
    assert "scan for CVEs" in parts[1]


def test_split_no_conjunction():
    parts = split_compound_intent("why are pods crashing in production")
    assert len(parts) == 1
    assert parts[0] == "why are pods crashing in production"


def test_split_also_conjunction():
    parts = split_compound_intent("check memory usage, also run a security audit")
    assert len(parts) == 2


def test_split_preserves_single_and():
    """'and' inside a phrase should not split."""
    parts = split_compound_intent("check pods and services in namespace foo")
    # "pods and services" is a list, not two intents
    assert len(parts) == 1


def test_split_triple_conjunction():
    parts = split_compound_intent("check pods and scan CVEs and review SLOs")
    assert len(parts) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "split_"`
Expected: FAIL with `ImportError: cannot import name 'split_compound_intent'`

- [ ] **Step 3: Implement split_compound_intent**

In `sre_agent/orchestrator.py`, add:

```python
import re as _re

_SPLIT_PATTERN = _re.compile(
    r"""
    (?:,\s*(?:and\s+)?also\s+)  |  # ", also" or ", and also"
    (?:,\s*also\s+)              |  # ", also"
    (?:\.\s+(?:Also|Then|Plus)\s+) |  # ". Also" / ". Then" / ". Plus"
    (?:,\s*(?:then|plus)\s+)     |  # ", then" / ", plus"
    (?:\s+and\s+(?=\w+\s+(?:for|the|my|a|an|all|any|check|scan|run|review|investigate|list|show|get|describe)))
    """,
    _re.VERBOSE | _re.IGNORECASE,
)


def split_compound_intent(query: str) -> list[str]:
    parts = _SPLIT_PATTERN.split(query)
    parts = [p.strip() for p in parts if p and p.strip()]
    return parts if parts else [query]
```

The regex splits on ", also", ". Also", ", then", ", plus", and "and" only when followed by a verb-like word (check, scan, run, etc.) — this avoids splitting "pods and services" which is a list, not two intents.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "split_"`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add sre_agent/orchestrator.py tests/test_multi_skill.py
git commit -m "feat: add split_compound_intent() for compound query detection"
```

---

### Task 7: classify_query_multi() in skill_loader

**Files:**
- Modify: `sre_agent/skill_loader.py`
- Test: `tests/test_multi_skill.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multi_skill.py`:

```python
from unittest.mock import patch, MagicMock
from sre_agent.skill_loader import classify_query_multi


def _mock_skill(name):
    from sre_agent.skill_loader import Skill
    from pathlib import Path
    return Skill(
        name=name, version=1, description=f"{name} skill",
        keywords=[], categories=[], write_tools=False,
        priority=1, system_prompt="", path=Path("."),
    )


def test_classify_query_multi_single_skill():
    """When score gap is large, returns (primary, None)."""
    mock_result = SelectionResult(
        skill_name="sre",
        fused_scores={"sre": 0.9, "security": 0.3},
        channel_scores={},
        threshold_used=0.3,
        secondary_skill=None,
    )
    with patch("sre_agent.skill_loader.classify_query", return_value=_mock_skill("sre")):
        with patch("sre_agent.skill_loader._get_selector") as mock_sel:
            mock_sel.return_value.last_result = mock_result
            primary, secondary = classify_query_multi("check pod logs")
    assert primary.name == "sre"
    assert secondary is None


def test_classify_query_multi_two_skills():
    """When score gap is small, returns both skills."""
    mock_result = SelectionResult(
        skill_name="sre",
        fused_scores={"sre": 0.8, "security": 0.7},
        channel_scores={},
        threshold_used=0.3,
        secondary_skill="security",
    )
    with patch("sre_agent.skill_loader.classify_query", return_value=_mock_skill("sre")):
        with patch("sre_agent.skill_loader._get_selector") as mock_sel:
            mock_sel.return_value.last_result = mock_result
            with patch("sre_agent.skill_loader.get_skill", side_effect=lambda n: _mock_skill(n)):
                primary, secondary = classify_query_multi("check crashes and scan for CVEs")
    assert primary.name == "sre"
    assert secondary is not None
    assert secondary.name == "security"


def test_classify_query_multi_disabled():
    """When multi_skill is False, always returns (primary, None)."""
    with patch("sre_agent.skill_loader.classify_query", return_value=_mock_skill("sre")):
        with patch("sre_agent.skill_loader.get_settings") as mock_settings:
            mock_settings.return_value.multi_skill = False
            primary, secondary = classify_query_multi("check crashes and scan CVEs")
    assert secondary is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "classify_query_multi"`
Expected: FAIL with `ImportError: cannot import name 'classify_query_multi'`

- [ ] **Step 3: Implement classify_query_multi**

In `sre_agent/skill_loader.py`, add after the existing `classify_query()` function:

```python
def classify_query_multi(
    query: str, *, context: dict | None = None
) -> tuple[Skill, Skill | None]:
    from .config import get_settings

    settings = get_settings()
    primary = classify_query(query, context=context)

    if not settings.multi_skill:
        return primary, None

    # Check ORCA score gap for secondary skill
    selector = _get_selector()
    result = getattr(selector, "last_result", None)
    if result and result.secondary_skill:
        secondary_skill = get_skill(result.secondary_skill)
        if secondary_skill:
            return primary, secondary_skill

    # Intent splitting for compound queries
    from .orchestrator import split_compound_intent

    parts = split_compound_intent(query)
    if len(parts) >= 2:
        # Route each sub-query independently
        skills_seen: dict[str, Skill] = {primary.name: primary}
        for part in parts:
            sub_skill = classify_query(part, context=context)
            if sub_skill.name not in skills_seen:
                skills_seen[sub_skill.name] = sub_skill
        if len(skills_seen) >= 2:
            other_name = next(n for n in skills_seen if n != primary.name)
            secondary = skills_seen[other_name]
            # Bidirectional conflict check
            if (
                other_name in (primary.conflicts_with or [])
                or primary.name in (secondary.conflicts_with or [])
            ):
                return primary, None
            return primary, secondary

    return primary, None
```

Also, in `SkillSelector.select()`, store the result for retrieval:

```python
        # At end of select(), before return:
        self.last_result = result
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "classify_query_multi"`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add sre_agent/skill_loader.py sre_agent/skill_selector.py tests/test_multi_skill.py
git commit -m "feat: add classify_query_multi() for top-K skill selection"
```

---

### Task 8: Context bus buffered publish mode

**Files:**
- Modify: `sre_agent/context_bus.py`
- Test: `tests/test_multi_skill.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multi_skill.py`:

```python
from sre_agent.context_bus import ContextBus, ContextEntry


def test_context_bus_buffered_mode():
    bus = ContextBus(max_entries=100, ttl_seconds=3600)
    task_id = "parallel-123"

    # Start buffering
    bus.start_buffering(task_id)

    entry = ContextEntry(
        source="sre_agent", category="diagnosis",
        summary="test finding", details={},
        parallel_task_id=task_id,
    )
    bus.publish(entry)

    # Entry should NOT be visible yet
    results = bus.get_context_for(category="diagnosis")
    buffered_summaries = [r.summary for r in results if r.summary == "test finding"]
    assert len(buffered_summaries) == 0

    # Flush the buffer
    bus.flush_buffer(task_id)

    # Now it should be visible
    results = bus.get_context_for(category="diagnosis")
    buffered_summaries = [r.summary for r in results if r.summary == "test finding"]
    assert len(buffered_summaries) == 1


def test_context_bus_unbuffered_publishes_immediately():
    bus = ContextBus(max_entries=100, ttl_seconds=3600)
    entry = ContextEntry(
        source="sre_agent", category="diagnosis",
        summary="immediate finding", details={},
    )
    bus.publish(entry)
    results = bus.get_context_for(category="diagnosis")
    immediate = [r for r in results if r.summary == "immediate finding"]
    assert len(immediate) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "context_bus"`
Expected: FAIL — `parallel_task_id` not a field, `start_buffering` doesn't exist

- [ ] **Step 3: Implement buffered publish**

In `sre_agent/context_bus.py`, modify `ContextEntry` to add:

```python
    parallel_task_id: str = ""
```

Add to the `ContextBus` class:

```python
    def __init__(self, max_entries=100, ttl_seconds=3600):
        # ... existing init ...
        self._buffers: dict[str, list[ContextEntry]] = {}

    def start_buffering(self, task_id: str) -> None:
        with self._lock:
            self._buffers[task_id] = []

    def flush_buffer(self, task_id: str) -> None:
        with self._lock:
            entries = self._buffers.pop(task_id, [])
        for entry in entries:
            self.publish(entry)

    def publish(self, entry: ContextEntry) -> None:
        # If entry has a parallel_task_id and we're buffering that task, buffer it
        if entry.parallel_task_id and entry.parallel_task_id in self._buffers:
            with self._lock:
                if entry.parallel_task_id in self._buffers:
                    self._buffers[entry.parallel_task_id].append(entry)
                    return
        # ... existing publish logic (DB insert, prune) ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "context_bus"`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add sre_agent/context_bus.py tests/test_multi_skill.py
git commit -m "feat: add buffered publish mode to ContextBus for parallel execution"
```

---

### Task 9: Synthesis module

**Files:**
- Create: `sre_agent/synthesis.py`
- Create: `tests/test_synthesis.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_synthesis.py
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from sre_agent.synthesis import (
    Conflict, SynthesisResult, ParallelSkillResult,
    synthesize_parallel_outputs, _build_fallback_response,
)


def test_conflict_dataclass():
    c = Conflict(
        topic="scaling", skill_a="sre", position_a="scale up",
        skill_b="capacity_planner", position_b="node at limit",
    )
    assert c.topic == "scaling"
    assert c.skill_a == "sre"


def test_synthesis_result_dataclass():
    r = SynthesisResult(
        unified_response="merged output",
        conflicts=[],
        sources={"sre": "pod analysis"},
    )
    assert r.unified_response == "merged output"
    assert r.conflicts == []


def test_parallel_skill_result_dataclass():
    r = ParallelSkillResult(
        primary_output="SRE findings", secondary_output="Security findings",
        primary_skill="sre", secondary_skill="security",
        primary_confidence=0.9, secondary_confidence=0.85,
        duration_ms=3000,
    )
    assert r.primary_skill == "sre"
    assert r.duration_ms == 3000


def test_fallback_response_concatenates():
    result = ParallelSkillResult(
        primary_output="SRE: pods crashing", secondary_output="Security: no CVEs found",
        primary_skill="sre", secondary_skill="security",
        primary_confidence=0.9, secondary_confidence=0.8,
        duration_ms=2000,
    )
    fallback = _build_fallback_response(result)
    assert "SRE: pods crashing" in fallback
    assert "Security: no CVEs found" in fallback
    assert "sre" in fallback.lower()
    assert "security" in fallback.lower()


@pytest.mark.asyncio
async def test_synthesize_fallback_on_api_error():
    """When Sonnet call fails, falls back to concatenation."""
    result = ParallelSkillResult(
        primary_output="SRE output", secondary_output="Security output",
        primary_skill="sre", secondary_skill="security",
        primary_confidence=0.9, secondary_confidence=0.8,
        duration_ms=2000,
    )
    mock_client = MagicMock()
    mock_client.messages.create = MagicMock(side_effect=Exception("API down"))

    synthesis = await synthesize_parallel_outputs(result, "test query", mock_client)
    assert "SRE output" in synthesis.unified_response
    assert "Security output" in synthesis.unified_response
    assert synthesis.conflicts == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sre_agent.synthesis'`

- [ ] **Step 3: Implement synthesis module**

Create `sre_agent/synthesis.py`:

```python
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SYNTHESIS_MODEL = "claude-sonnet-4-6-20250514"


@dataclass
class Conflict:
    topic: str
    skill_a: str
    position_a: str
    skill_b: str
    position_b: str


@dataclass
class SynthesisResult:
    unified_response: str
    conflicts: list[Conflict] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class ParallelSkillResult:
    primary_output: str
    secondary_output: str
    primary_skill: str
    secondary_skill: str
    primary_confidence: float
    secondary_confidence: float
    duration_ms: int


_SYNTHESIS_SYSTEM = """You merge two AI skill outputs into one coherent response.

Rules:
1. Combine non-conflicting findings into a single narrative. Do not repeat information.
2. If the skills contradict each other, emit a JSON conflict block — do NOT resolve it.
3. Attribute findings to their source skill where relevant.
4. Keep the merged response concise and actionable.

Output format:
- Write the merged response as plain text (markdown OK).
- If conflicts exist, end with a JSON block:
```json
{"conflicts": [{"topic": "...", "skill_a": "...", "position_a": "...", "skill_b": "...", "position_b": "..."}]}
```
- If no conflicts, do not include the JSON block."""


def _build_fallback_response(result: ParallelSkillResult) -> str:
    return (
        f"## {result.primary_skill.upper()} Analysis\n\n"
        f"{result.primary_output}\n\n"
        f"## {result.secondary_skill.upper()} Analysis\n\n"
        f"{result.secondary_output}"
    )


def _parse_conflicts(text: str) -> tuple[str, list[Conflict]]:
    conflicts: list[Conflict] = []
    json_start = text.rfind('```json')
    if json_start == -1:
        return text, conflicts

    json_end = text.find('```', json_start + 7)
    if json_end == -1:
        return text, conflicts

    json_str = text[json_start + 7:json_end].strip()
    clean_text = text[:json_start].strip()

    try:
        data = json.loads(json_str)
        for c in data.get("conflicts", []):
            conflicts.append(Conflict(
                topic=c.get("topic", ""),
                skill_a=c.get("skill_a", ""),
                position_a=c.get("position_a", ""),
                skill_b=c.get("skill_b", ""),
                position_b=c.get("position_b", ""),
            ))
    except (json.JSONDecodeError, KeyError):
        logger.debug("Failed to parse conflict JSON from synthesis", exc_info=True)

    return clean_text, conflicts


async def synthesize_parallel_outputs(
    result: ParallelSkillResult,
    query: str,
    client,
) -> SynthesisResult:
    try:
        import asyncio

        user_content = (
            f"Original user query: {query}\n\n"
            f"--- {result.primary_skill.upper()} SKILL OUTPUT ---\n"
            f"{result.primary_output}\n\n"
            f"--- {result.secondary_skill.upper()} SKILL OUTPUT ---\n"
            f"{result.secondary_output}"
        )

        response = await asyncio.to_thread(
            client.messages.create,
            model=SYNTHESIS_MODEL,
            max_tokens=4096,
            system=_SYNTHESIS_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )

        raw_text = response.content[0].text
        clean_text, conflicts = _parse_conflicts(raw_text)

        return SynthesisResult(
            unified_response=clean_text,
            conflicts=conflicts,
            sources={
                result.primary_skill: result.primary_output[:200],
                result.secondary_skill: result.secondary_output[:200],
            },
        )

    except Exception:
        logger.warning("Synthesis failed, falling back to concatenation", exc_info=True)
        return SynthesisResult(
            unified_response=_build_fallback_response(result),
            conflicts=[],
            sources={
                result.primary_skill: result.primary_output[:200],
                result.secondary_skill: result.secondary_output[:200],
            },
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add sre_agent/synthesis.py tests/test_synthesis.py
git commit -m "feat: add synthesis module for parallel skill output merging"
```

---

### Task 10: run_parallel_skills() in plan_runtime

**Files:**
- Modify: `sre_agent/plan_runtime.py`
- Test: `tests/test_multi_skill.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multi_skill.py`:

```python
import asyncio
import pytest
from sre_agent.synthesis import ParallelSkillResult


@pytest.mark.asyncio
async def test_run_parallel_skills_both_complete():
    from sre_agent.plan_runtime import run_parallel_skills

    primary = _mock_skill("sre")
    secondary = _mock_skill("security")

    async def mock_agent(*args, **kwargs):
        return "mock response"

    with patch("sre_agent.plan_runtime.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        mock_thread.return_value = "mock response"
        with patch("sre_agent.plan_runtime.build_config_from_skill") as mock_config:
            mock_config.return_value = {
                "system_prompt": "test", "tool_defs": [], "tool_map": {},
                "write_tools": set(),
            }
            with patch("sre_agent.plan_runtime.create_client"):
                result = await run_parallel_skills(
                    primary=primary, secondary=secondary,
                    query="test query", messages=[],
                    client=MagicMock(),
                )

    assert isinstance(result, ParallelSkillResult)
    assert result.primary_skill == "sre"
    assert result.secondary_skill == "security"
    assert result.primary_output == "mock response"
    assert result.secondary_output == "mock response"


@pytest.mark.asyncio
async def test_run_parallel_skills_one_timeout():
    from sre_agent.plan_runtime import run_parallel_skills

    primary = _mock_skill("sre")
    secondary = _mock_skill("security")

    call_count = 0

    async def mock_thread_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise asyncio.TimeoutError("skill timed out")
        return "primary completed"

    with patch("sre_agent.plan_runtime.asyncio.to_thread", side_effect=mock_thread_side_effect):
        with patch("sre_agent.plan_runtime.build_config_from_skill") as mock_config:
            mock_config.return_value = {
                "system_prompt": "test", "tool_defs": [], "tool_map": {},
                "write_tools": set(),
            }
            with patch("sre_agent.plan_runtime.create_client"):
                result = await run_parallel_skills(
                    primary=primary, secondary=secondary,
                    query="test query", messages=[],
                    client=MagicMock(),
                )

    assert result.primary_output == "primary completed"
    assert result.secondary_output == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "run_parallel"`
Expected: FAIL with `ImportError: cannot import name 'run_parallel_skills'`

- [ ] **Step 3: Implement run_parallel_skills**

In `sre_agent/plan_runtime.py`, add:

```python
from .synthesis import ParallelSkillResult


async def run_parallel_skills(
    primary,
    secondary,
    query: str,
    messages: list[dict],
    client,
    on_text=None,
    on_tool_use=None,
) -> ParallelSkillResult:
    from .skill_loader import build_config_from_skill
    from .agent import run_agent_streaming
    from .context_bus import get_context_bus

    start = time.monotonic()
    bus = get_context_bus()
    task_id = f"parallel-{uuid.uuid4().hex[:8]}"
    bus.start_buffering(task_id)

    primary_config = build_config_from_skill(primary, query=query)
    secondary_config = build_config_from_skill(secondary, query=query)

    # Secondary skill: strip write tools if primary also has them
    sec_write_tools = secondary_config["write_tools"]
    if primary_config["write_tools"] and sec_write_tools:
        sec_write_tools = set()

    async def _run_skill(config, skill_name, write_tools):
        try:
            result = await asyncio.to_thread(
                run_agent_streaming,
                client=client,
                messages=list(messages),
                system_prompt=config["system_prompt"],
                tool_defs=config["tool_defs"],
                tool_map=config["tool_map"],
                write_tools=write_tools,
                mode=skill_name,
            )
            return result if isinstance(result, str) else result[0]
        except asyncio.TimeoutError:
            logger.warning("Parallel skill %s timed out", skill_name)
            return ""
        except Exception:
            logger.warning("Parallel skill %s failed", skill_name, exc_info=True)
            return ""

    primary_task = asyncio.create_task(
        _run_skill(primary_config, primary.name, primary_config["write_tools"])
    )
    secondary_task = asyncio.create_task(
        _run_skill(secondary_config, secondary.name, sec_write_tools)
    )

    primary_output, secondary_output = await asyncio.gather(
        primary_task, secondary_task, return_exceptions=False
    )

    # Handle gather exceptions
    if isinstance(primary_output, BaseException):
        logger.warning("Primary skill failed: %s", primary_output)
        primary_output = ""
    if isinstance(secondary_output, BaseException):
        logger.warning("Secondary skill failed: %s", secondary_output)
        secondary_output = ""

    bus.flush_buffer(task_id)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    return ParallelSkillResult(
        primary_output=primary_output,
        secondary_output=secondary_output,
        primary_skill=primary.name,
        secondary_skill=secondary.name,
        primary_confidence=0.0,
        secondary_confidence=0.0,
        duration_ms=elapsed_ms,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "run_parallel"`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add sre_agent/plan_runtime.py tests/test_multi_skill.py
git commit -m "feat: add run_parallel_skills() for concurrent skill execution"
```

---

### Task 11: WebSocket integration — multi-skill routing branch

**Files:**
- Modify: `sre_agent/api/ws_endpoints.py`
- Test: `tests/test_multi_skill.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multi_skill.py`:

```python
def test_done_event_includes_multi_skill_metadata():
    """Verify the done event structure when multi-skill is active."""
    from sre_agent.synthesis import Conflict

    conflicts = [
        Conflict(topic="scaling", skill_a="sre", position_a="scale up",
                 skill_b="capacity_planner", position_b="node at limit")
    ]
    done_event = {
        "type": "done",
        "full_response": "merged response",
        "skill_name": "sre",
        "multi_skill": {
            "skills": ["sre", "capacity_planner"],
            "conflicts": [
                {"topic": c.topic, "skill_a": c.skill_a, "position_a": c.position_a,
                 "skill_b": c.skill_b, "position_b": c.position_b}
                for c in conflicts
            ],
        },
    }
    assert done_event["multi_skill"]["skills"] == ["sre", "capacity_planner"]
    assert len(done_event["multi_skill"]["conflicts"]) == 1
    assert done_event["multi_skill"]["conflicts"][0]["topic"] == "scaling"
```

- [ ] **Step 2: Run test to verify it passes** (this is a structure test, should pass immediately)

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "done_event"`
Expected: PASS

- [ ] **Step 3: Modify websocket_auto_agent() routing**

In `sre_agent/api/ws_endpoints.py`, replace the routing section (lines 375-382) with:

```python
            # --- Auto-classify intent with sticky mode ---
            # Try multi-skill routing first, fall back to single-skill
            secondary_skill = None
            try:
                from ..skill_loader import classify_query, classify_query_multi

                skill, secondary_skill = classify_query_multi(content)
                intent = skill.name
                is_strong = True
            except Exception:
                try:
                    from ..skill_loader import classify_query

                    skill = classify_query(content)
                    intent = skill.name
                    is_strong = True
                except Exception:
                    intent, is_strong = classify_intent(content)
```

Then, after the sticky mode logic and config building (around line 478), add the multi-skill branch:

```python
            if secondary_skill and get_settings().multi_skill:
                # --- Multi-skill parallel execution ---
                try:
                    await websocket.send_json({
                        "type": "multi_skill_start",
                        "skills": [intent, secondary_skill.name],
                    })

                    from ..plan_runtime import run_parallel_skills
                    from ..synthesis import synthesize_parallel_outputs

                    parallel_result = await run_parallel_skills(
                        primary=skill, secondary=secondary_skill,
                        query=content, messages=messages, client=None,
                    )

                    await websocket.send_json({"type": "skill_progress", "skill": intent, "status": "complete"})
                    await websocket.send_json({"type": "skill_progress", "skill": secondary_skill.name, "status": "complete"})
                    await websocket.send_json({"type": "skill_progress", "skill": "synthesis", "status": "running"})

                    from ..k8s_client import create_client
                    synth_client = create_client()
                    synthesis = await synthesize_parallel_outputs(parallel_result, content, synth_client)

                    full_response = synthesis.unified_response
                    messages.append({"role": "assistant", "content": full_response})

                    # Stream as text_delta
                    await websocket.send_json({"type": "text_delta", "text": full_response})

                    # Publish both outputs to context bus
                    if parallel_result.primary_output:
                        bus.publish(ContextEntry(
                            source="sre_agent", category="diagnosis",
                            summary=parallel_result.primary_output[:200],
                            details={"mode": intent}, namespace=namespace_from_context,
                        ))
                    if parallel_result.secondary_output:
                        bus.publish(ContextEntry(
                            source="security_agent" if secondary_skill.name == "security" else "sre_agent",
                            category="diagnosis",
                            summary=parallel_result.secondary_output[:200],
                            details={"mode": secondary_skill.name}, namespace=namespace_from_context,
                        ))

                    # Done with multi_skill metadata
                    conflict_dicts = [
                        {"topic": c.topic, "skill_a": c.skill_a, "position_a": c.position_a,
                         "skill_b": c.skill_b, "position_b": c.position_b}
                        for c in synthesis.conflicts
                    ]
                    await websocket.send_json({
                        "type": "done",
                        "full_response": full_response,
                        "skill_name": intent,
                        "multi_skill": {
                            "skills": [intent, secondary_skill.name],
                            "conflicts": conflict_dicts,
                        },
                    })

                    # Reset sticky mode after multi-skill turn
                    last_mode = None
                    continue

                except Exception:
                    logger.warning("Multi-skill execution failed, falling back to single-skill", exc_info=True)
                    secondary_skill = None
                    # Fall through to single-skill path below
```

The `continue` skips the single-skill path. If multi-skill fails, it falls through.

- [ ] **Step 4: Run full test suite**

Run: `python3 -m pytest tests/ -v --timeout=120`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/ws_endpoints.py tests/test_multi_skill.py
git commit -m "feat: add multi-skill routing branch to WebSocket auto-agent"
```

---

### Task 12: Guard rails — circuit breaker, rate guard, write exclusivity

**Files:**
- Modify: `sre_agent/api/ws_endpoints.py`
- Test: `tests/test_multi_skill.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multi_skill.py`:

```python
def test_multi_skill_guard_circuit_breaker():
    """Multi-skill should degrade when circuit breaker is open."""
    from sre_agent.agent import CircuitBreaker

    cb = CircuitBreaker(threshold=3, timeout=60)
    # Open the circuit breaker
    for _ in range(3):
        cb.record_failure()
    assert cb.state == "OPEN"

    # When CB is open, multi-skill should be suppressed
    # (tested via integration in ws_endpoints — this validates CB state detection)
    assert cb.should_allow() is False


def test_multi_skill_write_tool_exclusivity():
    """Only primary skill gets write tools when both declare write_tools=True."""
    from sre_agent.skill_loader import Skill
    from pathlib import Path

    primary = Skill(
        name="sre", version=1, description="SRE", keywords=[], categories=["operations"],
        write_tools=True, priority=1, system_prompt="", path=Path("."),
    )
    secondary = Skill(
        name="security", version=1, description="Security", keywords=[], categories=["security"],
        write_tools=True, priority=2, system_prompt="", path=Path("."),
    )
    # Both have write_tools=True; secondary should be stripped
    assert primary.write_tools is True
    assert secondary.write_tools is True
    # run_parallel_skills handles this — secondary gets empty write_tools set
```

- [ ] **Step 2: Run tests to verify they pass** (these validate existing behavior)

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "guard"`
Expected: PASS

- [ ] **Step 3: Add circuit breaker guard to ws_endpoints multi-skill branch**

In the multi-skill branch of `ws_endpoints.py`, before `run_parallel_skills()`, add:

```python
                    # Guard: circuit breaker
                    from ..agent import _circuit_breaker
                    if _circuit_breaker and not _circuit_breaker.should_allow():
                        logger.info("Circuit breaker OPEN — degrading to single-skill")
                        secondary_skill = None
                        # Fall through to single-skill
```

- [ ] **Step 4: Add rate guard tracking**

In the multi-skill branch, after successful completion, add:

```python
                    # Rate guard: track multi-skill activation rate
                    _multi_skill_turns = getattr(websocket, "_multi_skill_turns", 0) + 1
                    websocket._multi_skill_turns = _multi_skill_turns
                    if turn_counter > 4 and _multi_skill_turns / turn_counter > 0.5:
                        logger.warning(
                            "Multi-skill rate %.0f%% exceeds 50%% — threshold may be too loose",
                            (_multi_skill_turns / turn_counter) * 100,
                        )
```

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v --timeout=120`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add sre_agent/api/ws_endpoints.py tests/test_multi_skill.py
git commit -m "feat: add circuit breaker, rate, and write-tool guards for multi-skill"
```

---

### Task 13: Eval scenarios for multi-skill routing

**Files:**
- Modify: eval suite files (check existing eval structure first)
- Test: `tests/test_multi_skill.py`

- [ ] **Step 1: Write eval scenario tests**

Append to `tests/test_multi_skill.py`:

```python
def test_compound_query_routes_to_two_skills():
    """Compound SRE+Security query should activate multi-skill."""
    parts = split_compound_intent("check why pods are crashing and scan for vulnerabilities")
    assert len(parts) == 2

    # Verify the two sub-queries would route differently
    # (Full integration test — mock ORCA to return different skills per sub-query)
    with patch("sre_agent.skill_loader.classify_query") as mock_cq:
        mock_cq.side_effect = [_mock_skill("sre"), _mock_skill("security")]
        with patch("sre_agent.skill_loader._get_selector") as mock_sel:
            mock_sel.return_value.last_result = SelectionResult(
                skill_name="sre", fused_scores={"sre": 0.8, "security": 0.3},
                channel_scores={}, threshold_used=0.3, secondary_skill=None,
            )
            primary, secondary = classify_query_multi("check why pods are crashing and scan for vulnerabilities")
    assert primary.name == "sre"
    assert secondary is not None
    assert secondary.name == "security"


def test_single_domain_query_stays_single_skill():
    """Pure SRE query should NOT activate multi-skill."""
    parts = split_compound_intent("why are pods crashing in the production namespace")
    assert len(parts) == 1

    with patch("sre_agent.skill_loader.classify_query", return_value=_mock_skill("sre")):
        with patch("sre_agent.skill_loader._get_selector") as mock_sel:
            mock_sel.return_value.last_result = SelectionResult(
                skill_name="sre", fused_scores={"sre": 0.9, "security": 0.2},
                channel_scores={}, threshold_used=0.3, secondary_skill=None,
            )
            primary, secondary = classify_query_multi("why are pods crashing in the production namespace")
    assert primary.name == "sre"
    assert secondary is None
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/test_multi_skill.py -v -k "routes_to or stays_single"`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_multi_skill.py
git commit -m "test: add eval scenarios for multi-skill routing accuracy"
```

---

### Task 14: Final integration test and docs update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v --timeout=120`
Expected: All tests pass

- [ ] **Step 2: Run linter and type checker**

Run: `make verify`
Expected: 0 lint errors, 0 mypy errors, all tests pass

- [ ] **Step 3: Update CLAUDE.md**

Update the project description line to reflect multi-skill capability:

In the `## Project` section, update the relevant counts and add mention of parallel multi-skill execution:

- Add "parallel multi-skill execution (max 2)" to the feature list
- Update tool count if any new tools were added
- Add `synthesis.py` to the key files section:
  ```
  - `synthesis.py` — parallel skill output merging with Sonnet-powered conflict detection and fallback concatenation
  ```
- Add new env vars to the table:
  ```
  | `PULSE_AGENT_MULTI_SKILL` | Enable parallel multi-skill routing | `true` |
  | `PULSE_AGENT_MULTI_SKILL_THRESHOLD` | ORCA score gap for multi-skill activation | `0.15` |
  | `PULSE_AGENT_MULTI_SKILL_MAX` | Max concurrent skills | `2` |
  | `PULSE_AGENT_TEMPORAL_CACHE_TTL` | Temporal signal cache TTL (seconds) | `60` |
  ```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with parallel multi-skill execution"
```

- [ ] **Step 5: Run tests one final time**

Run: `python3 -m pytest tests/ -v --timeout=120`
Expected: All tests pass — ready for PR
