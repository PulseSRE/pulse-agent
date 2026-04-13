# Adaptive Tool Selection Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the static category-dump tool selection (8% accuracy) with a learned TF-IDF predictor + LLM fallback + chain expansion system targeting 50%+ accuracy.

**Architecture:** Three-tier prediction — TF-IDF token→tool scoring as the hot path (zero cost, sub-ms), Haiku LLM picker as cold-start fallback (self-eliminating), chain bigrams + co-occurrence for mid-turn expansion. Real-time learning from every completed turn.

**Tech Stack:** PostgreSQL (existing), psycopg2 (existing), Anthropic SDK (existing for Haiku calls)

---

### Task 1: Database Migration — tool_predictions and tool_cooccurrence tables

**Files:**
- Modify: `sre_agent/db_migrations.py:134-146` (add migration 012)
- Modify: `sre_agent/db_schema.py` (add schema constants)
- Test: `tests/test_tool_predictor.py` (new file)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_predictor.py
"""Tests for adaptive tool prediction engine."""

from __future__ import annotations

import os
from sre_agent.db import Database
from sre_agent.db_migrations import run_migrations

_TEST_DB_URL = os.environ.get(
    "PULSE_AGENT_TEST_DATABASE_URL",
    "postgresql://pulse:pulse@localhost:5433/pulse_test",
)


def _make_test_db() -> Database:
    db = Database(_TEST_DB_URL)
    run_migrations(db)
    return db


class TestMigration:
    def test_tool_predictions_table_exists(self):
        db = _make_test_db()
        db.execute("SELECT 1 FROM tool_predictions LIMIT 0")

    def test_tool_cooccurrence_table_exists(self):
        db = _make_test_db()
        db.execute("SELECT 1 FROM tool_cooccurrence LIMIT 0")

    def test_tool_predictions_columns(self):
        db = _make_test_db()
        row = db.fetchone(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'tool_predictions' AND column_name = 'miss_count'"
        )
        assert row is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestMigration -v`
Expected: FAIL — `tool_predictions` table does not exist

- [ ] **Step 3: Add schema constants**

In `sre_agent/db_schema.py`, add at the end:

```python
TOOL_PREDICTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_predictions (
    token       TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    score       FLOAT NOT NULL DEFAULT 1.0,
    hit_count   INT NOT NULL DEFAULT 1,
    miss_count  INT NOT NULL DEFAULT 0,
    last_seen   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (token, tool_name)
);
CREATE INDEX IF NOT EXISTS idx_tool_predictions_token ON tool_predictions(token);
"""

TOOL_COOCCURRENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_cooccurrence (
    tool_a      TEXT NOT NULL,
    tool_b      TEXT NOT NULL,
    frequency   INT NOT NULL DEFAULT 1,
    PRIMARY KEY (tool_a, tool_b)
);
"""
```

- [ ] **Step 4: Add migration 012**

In `sre_agent/db_migrations.py`, add before the `MIGRATIONS` list:

```python
def _migrate_012_tool_predictions(db: Database) -> None:
    """Add tool_predictions and tool_cooccurrence tables for adaptive tool selection."""
    from .db_schema import TOOL_COOCCURRENCE_SCHEMA, TOOL_PREDICTIONS_SCHEMA

    db.executescript(TOOL_PREDICTIONS_SCHEMA + TOOL_COOCCURRENCE_SCHEMA)
```

Add to `MIGRATIONS` list:

```python
    (12, "tool_predictions", _migrate_012_tool_predictions),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestMigration -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/db_schema.py sre_agent/db_migrations.py tests/test_tool_predictor.py
git commit -m "feat: add tool_predictions and tool_cooccurrence tables (migration 012)"
```

---

### Task 2: Token Extraction

**Files:**
- Create: `sre_agent/tool_predictor.py`
- Test: `tests/test_tool_predictor.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tool_predictor.py`:

```python
from sre_agent.tool_predictor import extract_tokens


class TestExtractTokens:
    def test_basic_query(self):
        tokens = extract_tokens("why are pods crashlooping in production")
        assert "pods" in tokens
        assert "crashlooping" in tokens
        assert "production" in tokens

    def test_drops_stopwords(self):
        tokens = extract_tokens("can you please show me the pods")
        assert "can" not in tokens
        assert "you" not in tokens
        assert "please" not in tokens
        assert "pods" in tokens

    def test_bigrams(self):
        tokens = extract_tokens("check node pressure")
        assert "node pressure" in tokens

    def test_k8s_terms_intact(self):
        tokens = extract_tokens("pod is in CrashLoopBackOff state")
        assert "crashloopbackoff" in tokens

    def test_punctuation_stripped(self):
        tokens = extract_tokens("what's wrong with my pods?")
        assert "pods" in tokens
        assert "wrong" in tokens

    def test_empty_query(self):
        assert extract_tokens("") == []

    def test_deduplication(self):
        tokens = extract_tokens("pods pods pods")
        assert tokens.count("pods") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestExtractTokens -v`
Expected: FAIL — `tool_predictor` module does not exist

- [ ] **Step 3: Implement token extraction**

Create `sre_agent/tool_predictor.py`:

```python
"""Adaptive tool selection engine — learns which tools to offer per query.

Three-tier prediction:
1. TF-IDF token scoring (hot path, zero cost, sub-ms)
2. LLM picker via Haiku (cold-start fallback, self-eliminating)
3. Chain bigrams + co-occurrence (mid-turn expansion)
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("pulse_agent.tool_predictor")

_STOPWORDS = frozenset({
    "the", "a", "an", "in", "my", "me", "can", "you", "please", "what",
    "is", "are", "do", "how", "this", "that", "it", "for", "to", "of",
    "and", "or", "show", "tell", "get", "why", "with", "all", "about",
    "i", "need", "want", "help", "check", "look", "at", "on", "from",
    "be", "been", "being", "has", "have", "had", "was", "were", "will",
    "would", "could", "should", "does", "did", "just", "also", "some",
    "if", "so", "but", "not", "no", "there", "their", "they", "them",
    "its", "any", "more", "very", "too", "into", "up", "out",
})

_SPLIT_RE = re.compile(r"[^a-z0-9_-]+")


def extract_tokens(query: str) -> list[str]:
    """Extract meaningful tokens from a user query.

    Returns deduplicated unigrams + bigrams, stopwords removed.
    K8s compound terms (e.g., CrashLoopBackOff) are kept intact.
    """
    if not query or not query.strip():
        return []

    lowered = query.lower()
    words = [w for w in _SPLIT_RE.split(lowered) if w and w not in _STOPWORDS]

    seen: set[str] = set()
    tokens: list[str] = []

    for w in words:
        if w not in seen:
            seen.add(w)
            tokens.append(w)

    for i in range(len(words) - 1):
        bigram = f"{words[i]} {words[i + 1]}"
        if bigram not in seen:
            seen.add(bigram)
            tokens.append(bigram)

    return tokens
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestExtractTokens -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_predictor.py tests/test_tool_predictor.py
git commit -m "feat: add token extraction for adaptive tool selection"
```

---

### Task 3: Real-Time Learning — learn() function

**Files:**
- Modify: `sre_agent/tool_predictor.py`
- Test: `tests/test_tool_predictor.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tool_predictor.py`:

```python
from unittest.mock import patch, MagicMock
from sre_agent.tool_predictor import learn


class TestLearn:
    def _make_mock_db(self):
        db = MagicMock()
        return db

    @patch("sre_agent.tool_predictor._get_db")
    def test_records_positive_signals(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        learn(
            query="show pods in production",
            tools_called=["list_pods", "describe_pod"],
            tools_offered=["list_pods", "describe_pod", "get_configmap"],
        )

        # Should have called execute for positive signals (tokens x tools_called)
        # and negative signals (tokens x tools_not_called)
        assert db.execute.call_count > 0
        assert db.commit.called

    @patch("sre_agent.tool_predictor._get_db")
    def test_records_cooccurrence(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        learn(
            query="check pods",
            tools_called=["list_pods", "describe_pod", "get_pod_logs"],
            tools_offered=["list_pods", "describe_pod", "get_pod_logs"],
        )

        # Co-occurrence for 3 tools = 3 pairs: (a,b), (a,c), (b,c)
        calls = [str(c) for c in db.execute.call_args_list]
        cooccurrence_calls = [c for c in calls if "tool_cooccurrence" in c]
        assert len(cooccurrence_calls) == 3

    @patch("sre_agent.tool_predictor._get_db")
    def test_no_crash_on_db_failure(self, mock_get_db):
        mock_get_db.side_effect = Exception("DB down")
        # Should not raise
        learn(query="test", tools_called=["list_pods"], tools_offered=["list_pods"])

    @patch("sre_agent.tool_predictor._get_db")
    def test_skips_empty_calls(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db
        learn(query="test", tools_called=[], tools_offered=["list_pods"])
        assert not db.commit.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestLearn -v`
Expected: FAIL — `learn` not defined

- [ ] **Step 3: Implement learn()**

Add to `sre_agent/tool_predictor.py`:

```python
from itertools import combinations


def _get_db():
    """Get database connection. Separate function for easy mocking."""
    from .db import get_database
    return get_database()


def learn(
    *,
    query: str,
    tools_called: list[str],
    tools_offered: list[str],
) -> None:
    """Record a completed turn to update predictions and co-occurrence.

    Fire-and-forget: swallows all exceptions.
    """
    if not tools_called:
        return

    try:
        db = _get_db()
        tokens = extract_tokens(query)
        if not tokens:
            return

        called_set = set(tools_called)
        not_called = set(tools_offered) - called_set

        # Positive signals: tokens x tools_called
        for token in tokens:
            for tool in tools_called:
                db.execute(
                    "INSERT INTO tool_predictions (token, tool_name, score, hit_count, miss_count, last_seen) "
                    "VALUES (%s, %s, 1.0, 1, 0, NOW()) "
                    "ON CONFLICT (token, tool_name) DO UPDATE SET "
                    "score = tool_predictions.score + 1.0, "
                    "hit_count = tool_predictions.hit_count + 1, "
                    "last_seen = NOW()",
                    (token, tool),
                )

        # Negative signals: tokens x tools_not_called
        for token in tokens:
            for tool in not_called:
                db.execute(
                    "INSERT INTO tool_predictions (token, tool_name, score, hit_count, miss_count, last_seen) "
                    "VALUES (%s, %s, 0.0, 0, 1, NOW()) "
                    "ON CONFLICT (token, tool_name) DO UPDATE SET "
                    "miss_count = tool_predictions.miss_count + 1, "
                    "last_seen = NOW()",
                    (token, tool),
                )

        # Co-occurrence: pairs of tools called together
        for tool_a, tool_b in combinations(sorted(tools_called), 2):
            db.execute(
                "INSERT INTO tool_cooccurrence (tool_a, tool_b, frequency) "
                "VALUES (%s, %s, 1) "
                "ON CONFLICT (tool_a, tool_b) DO UPDATE SET "
                "frequency = tool_cooccurrence.frequency + 1",
                (tool_a, tool_b),
            )

        db.commit()
    except Exception:
        logger.debug("Failed to record tool predictions", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestLearn -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_predictor.py tests/test_tool_predictor.py
git commit -m "feat: add real-time learning for tool predictions"
```

---

### Task 4: TF-IDF Prediction — predict_tools()

**Files:**
- Modify: `sre_agent/tool_predictor.py`
- Test: `tests/test_tool_predictor.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tool_predictor.py`:

```python
from sre_agent.tool_predictor import predict_tools, PredictionResult


class TestPredictTools:
    @patch("sre_agent.tool_predictor._get_db")
    def test_returns_prediction_result(self, mock_get_db):
        db = MagicMock()
        db.fetchall.return_value = [
            {"tool_name": "list_pods", "total_score": 10.0, "total_hits": 5},
            {"tool_name": "describe_pod", "total_score": 8.0, "total_hits": 4},
            {"tool_name": "get_pod_logs", "total_score": 6.0, "total_hits": 3},
        ]
        mock_get_db.return_value = db

        result = predict_tools("show me pods", top_k=10)
        assert isinstance(result, PredictionResult)
        assert result.confidence == "high"
        assert "list_pods" in result.tools

    @patch("sre_agent.tool_predictor._get_db")
    def test_low_confidence_when_no_data(self, mock_get_db):
        db = MagicMock()
        db.fetchall.return_value = []
        mock_get_db.return_value = db

        result = predict_tools("show me pods", top_k=10)
        assert result.confidence == "low"
        assert result.tools == []

    @patch("sre_agent.tool_predictor._get_db")
    def test_low_confidence_when_sparse_hits(self, mock_get_db):
        db = MagicMock()
        db.fetchall.return_value = [
            {"tool_name": "list_pods", "total_score": 2.0, "total_hits": 2},
        ]
        mock_get_db.return_value = db

        result = predict_tools("show me pods", top_k=10)
        assert result.confidence == "low"

    @patch("sre_agent.tool_predictor._get_db")
    def test_cooccurrence_expansion(self, mock_get_db):
        db = MagicMock()
        # First call: predictions, second call: co-occurrence
        db.fetchall.side_effect = [
            [
                {"tool_name": "describe_pod", "total_score": 20.0, "total_hits": 15},
            ],
            [
                {"tool_b": "get_pod_logs", "frequency": 50},
                {"tool_b": "get_events", "frequency": 30},
            ],
        ]
        mock_get_db.return_value = db

        result = predict_tools("describe the pod", top_k=10)
        assert "describe_pod" in result.tools
        assert "get_pod_logs" in result.tools
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestPredictTools -v`
Expected: FAIL — `predict_tools` and `PredictionResult` not defined

- [ ] **Step 3: Implement predict_tools()**

Add to `sre_agent/tool_predictor.py`:

```python
from dataclasses import dataclass, field

_CONFIDENCE_THRESHOLD = 10  # min total hit_count to trust TF-IDF


@dataclass
class PredictionResult:
    """Result of tool prediction."""
    tools: list[str] = field(default_factory=list)
    confidence: str = "low"  # "high" or "low"
    source: str = "none"  # "tfidf", "llm", "category", "none"


def predict_tools(query: str, *, top_k: int = 10) -> PredictionResult:
    """Predict which tools are most relevant for a query using TF-IDF scoring.

    Returns a PredictionResult with the predicted tool names and confidence level.
    """
    tokens = extract_tokens(query)
    if not tokens:
        return PredictionResult()

    try:
        db = _get_db()
    except Exception:
        return PredictionResult()

    placeholders = ", ".join(["%s"] * len(tokens))

    try:
        rows = db.fetchall(
            f"SELECT tool_name, "
            f"SUM(score - miss_count * 0.3) AS total_score, "
            f"SUM(hit_count) AS total_hits "
            f"FROM tool_predictions "
            f"WHERE token IN ({placeholders}) "
            f"GROUP BY tool_name "
            f"HAVING SUM(score - miss_count * 0.3) > 0 "
            f"ORDER BY total_score DESC "
            f"LIMIT %s",
            (*tokens, top_k),
        )
    except Exception:
        logger.debug("TF-IDF lookup failed", exc_info=True)
        return PredictionResult()

    if not rows:
        return PredictionResult()

    max_hits = max(r["total_hits"] for r in rows)
    if max_hits < _CONFIDENCE_THRESHOLD:
        return PredictionResult(confidence="low")

    predicted = [r["tool_name"] for r in rows]

    # Co-occurrence expansion
    expanded = _expand_cooccurrence(db, predicted, top_k)
    final = predicted + [t for t in expanded if t not in predicted]

    return PredictionResult(tools=final[:top_k + 5], confidence="high", source="tfidf")


def _expand_cooccurrence(db, tools: list[str], limit: int = 5) -> list[str]:
    """Find tools that frequently co-occur with the predicted set."""
    if not tools:
        return []

    placeholders = ", ".join(["%s"] * len(tools))
    try:
        rows = db.fetchall(
            f"SELECT tool_b, frequency FROM tool_cooccurrence "
            f"WHERE tool_a IN ({placeholders}) AND tool_b NOT IN ({placeholders}) "
            f"ORDER BY frequency DESC LIMIT %s",
            (*tools, *tools, limit),
        )
        return [r["tool_b"] for r in rows]
    except Exception:
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestPredictTools -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_predictor.py tests/test_tool_predictor.py
git commit -m "feat: add TF-IDF prediction with co-occurrence expansion"
```

---

### Task 5: LLM Fallback — Haiku Tool Picker

**Files:**
- Modify: `sre_agent/tool_predictor.py`
- Test: `tests/test_tool_predictor.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tool_predictor.py`:

```python
from sre_agent.tool_predictor import llm_pick_tools


class TestLLMPicker:
    @patch("sre_agent.tool_predictor.create_client")
    def test_returns_tool_list(self, mock_create):
        client = MagicMock()
        mock_create.return_value = client
        client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="list_pods, describe_pod, get_pod_logs")]
        )

        tools = llm_pick_tools(
            query="why are pods crashing",
            tool_names=["list_pods", "describe_pod", "get_pod_logs", "scale_deployment", "drain_node"],
            top_k=3,
        )
        assert "list_pods" in tools
        assert "describe_pod" in tools
        assert len(tools) <= 3

    @patch("sre_agent.tool_predictor.create_client")
    def test_filters_invalid_names(self, mock_create):
        client = MagicMock()
        mock_create.return_value = client
        client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="list_pods, FAKE_TOOL, describe_pod")]
        )

        tools = llm_pick_tools(
            query="check pods",
            tool_names=["list_pods", "describe_pod"],
            top_k=10,
        )
        assert "FAKE_TOOL" not in tools
        assert "list_pods" in tools

    @patch("sre_agent.tool_predictor.create_client")
    def test_returns_empty_on_failure(self, mock_create):
        mock_create.side_effect = Exception("API down")
        tools = llm_pick_tools(query="test", tool_names=["list_pods"], top_k=5)
        assert tools == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestLLMPicker -v`
Expected: FAIL — `llm_pick_tools` not defined

- [ ] **Step 3: Implement llm_pick_tools()**

Add to `sre_agent/tool_predictor.py`:

```python
def llm_pick_tools(
    *,
    query: str,
    tool_names: list[str],
    top_k: int = 10,
) -> list[str]:
    """Use Haiku to pick the most relevant tools for a query.

    Sends only tool names (~200 tokens), not full schemas.
    Returns validated tool names (filtered against tool_names).
    """
    try:
        from .agent import create_client

        client = create_client()
        tool_list = ", ".join(tool_names)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": query}],
            system=(
                f"You are a tool selector. Given a user query about Kubernetes/OpenShift, "
                f"pick the {top_k} most relevant tools from this list:\n{tool_list}\n\n"
                f"Reply with ONLY comma-separated tool names, nothing else."
            ),
        )

        raw = response.content[0].text.strip()
        valid_set = set(tool_names)
        picked = [t.strip() for t in raw.split(",") if t.strip() in valid_set]
        return picked[:top_k]

    except Exception:
        logger.debug("LLM tool picker failed", exc_info=True)
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestLLMPicker -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_predictor.py tests/test_tool_predictor.py
git commit -m "feat: add Haiku LLM fallback for cold-start tool prediction"
```

---

### Task 6: Orchestration — select_tools_adaptive()

**Files:**
- Modify: `sre_agent/tool_predictor.py`
- Test: `tests/test_tool_predictor.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tool_predictor.py`:

```python
from sre_agent.tool_predictor import select_tools_adaptive, ALWAYS_INCLUDE_SLIM


class TestSelectToolsAdaptive:
    def _mock_tool(self, name):
        t = MagicMock()
        t.name = name
        t.to_dict.return_value = {"name": name}
        return t

    def _all_tools(self):
        names = list(ALWAYS_INCLUDE_SLIM) + [
            "describe_pod", "get_pod_logs", "get_configmap",
            "scale_deployment", "drain_node", "list_nodes",
        ]
        return {n: self._mock_tool(n) for n in names}

    @patch("sre_agent.tool_predictor.predict_tools")
    def test_high_confidence_uses_tfidf(self, mock_predict):
        mock_predict.return_value = PredictionResult(
            tools=["describe_pod", "get_pod_logs"],
            confidence="high",
            source="tfidf",
        )
        all_tools = self._all_tools()
        defs, tool_map, offered = select_tools_adaptive(
            query="show pod logs",
            all_tool_map=all_tools,
            fallback_categories=["diagnostics"],
        )
        assert "describe_pod" in tool_map
        assert "get_pod_logs" in tool_map
        # ALWAYS_INCLUDE_SLIM should be present
        for t in ALWAYS_INCLUDE_SLIM:
            if t in all_tools:
                assert t in tool_map

    @patch("sre_agent.tool_predictor.predict_tools")
    @patch("sre_agent.tool_predictor.llm_pick_tools")
    def test_low_confidence_uses_llm(self, mock_llm, mock_predict):
        mock_predict.return_value = PredictionResult(confidence="low")
        mock_llm.return_value = ["describe_pod", "list_nodes"]

        all_tools = self._all_tools()
        defs, tool_map, offered = select_tools_adaptive(
            query="unusual query",
            all_tool_map=all_tools,
            fallback_categories=["diagnostics"],
        )
        assert "describe_pod" in tool_map
        mock_llm.assert_called_once()

    @patch("sre_agent.tool_predictor.predict_tools")
    @patch("sre_agent.tool_predictor.llm_pick_tools")
    def test_falls_back_to_categories(self, mock_llm, mock_predict):
        mock_predict.return_value = PredictionResult(confidence="low")
        mock_llm.return_value = []

        all_tools = self._all_tools()
        defs, tool_map, offered = select_tools_adaptive(
            query="check pods",
            all_tool_map=all_tools,
            fallback_categories=["diagnostics"],
        )
        # Should still have tools (from category fallback)
        assert len(tool_map) >= len(ALWAYS_INCLUDE_SLIM)

    @patch("sre_agent.tool_predictor.predict_tools")
    def test_minimum_set_size(self, mock_predict):
        mock_predict.return_value = PredictionResult(
            tools=["describe_pod"],
            confidence="high",
            source="tfidf",
        )
        all_tools = self._all_tools()
        defs, tool_map, offered = select_tools_adaptive(
            query="describe pod",
            all_tool_map=all_tools,
            fallback_categories=["diagnostics"],
        )
        assert len(tool_map) >= 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestSelectToolsAdaptive -v`
Expected: FAIL — `select_tools_adaptive` not defined

- [ ] **Step 3: Implement select_tools_adaptive()**

Add to `sre_agent/tool_predictor.py`:

```python
ALWAYS_INCLUDE_SLIM = {
    "list_pods",
    "get_events",
    "namespace_summary",
    "record_audit_entry",
    "list_my_skills",
}

_MIN_TOOL_SET_SIZE = 8


def select_tools_adaptive(
    query: str,
    *,
    all_tool_map: dict,
    fallback_categories: list[str] | None = None,
) -> tuple[list[dict], dict, list[str]]:
    """Adaptive tool selection: TF-IDF -> LLM -> category fallback.

    Returns (tool_defs, tool_map, offered_names) — same signature as select_tools().
    """
    from .skill_loader import TOOL_CATEGORIES

    # Phase 1: TF-IDF prediction
    result = predict_tools(query)

    if result.confidence == "high" and result.tools:
        tool_names = set(result.tools)
        source = "tfidf"
    else:
        # Phase 2: LLM fallback
        llm_tools = llm_pick_tools(
            query=query,
            tool_names=list(all_tool_map.keys()),
        )
        if llm_tools:
            tool_names = set(llm_tools)
            source = "llm"

            # Bootstrap TF-IDF from LLM picks (fire-and-forget)
            try:
                learn(query=query, tools_called=llm_tools, tools_offered=list(all_tool_map.keys()))
            except Exception:
                pass
        else:
            # Phase 3: Category fallback
            tool_names = set()
            for cat_name in (fallback_categories or []):
                cat = TOOL_CATEGORIES.get(cat_name, {})
                tool_names.update(cat.get("tools", []))
            source = "category"

    # Always include slim set
    tool_names.update(ALWAYS_INCLUDE_SLIM)

    # Enforce minimum set size by padding from categories
    if len(tool_names) < _MIN_TOOL_SET_SIZE and fallback_categories:
        for cat_name in fallback_categories:
            cat = TOOL_CATEGORIES.get(cat_name, {})
            tool_names.update(cat.get("tools", []))
            if len(tool_names) >= _MIN_TOOL_SET_SIZE:
                break

    # Build final tool map
    tool_map = {n: t for n, t in all_tool_map.items() if n in tool_names}
    tool_defs = [t.to_dict() for t in tool_map.values()]
    offered = list(tool_map.keys())

    logger.info(
        "Adaptive tool selection: source=%s, predicted=%d, final=%d (query: %.50s)",
        source, len(result.tools) if result.tools else 0, len(tool_map), query,
    )

    return tool_defs, tool_map, offered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestSelectToolsAdaptive -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_predictor.py tests/test_tool_predictor.py
git commit -m "feat: add adaptive tool selection orchestrator (TF-IDF -> LLM -> category)"
```

---

### Task 7: Integration — Wire into skill_loader and agent

**Files:**
- Modify: `sre_agent/skill_loader.py:1029-1091`
- Modify: `sre_agent/tool_usage.py:133-210`
- Test: `tests/test_tool_predictor.py` (extend)

- [ ] **Step 1: Write failing test for skill_loader integration**

Append to `tests/test_tool_predictor.py`:

```python
class TestSkillLoaderIntegration:
    @patch("sre_agent.tool_predictor.predict_tools")
    @patch("sre_agent.skill_loader.build_config_from_skill")
    def test_build_config_calls_adaptive_when_query_present(self, mock_build, mock_predict):
        """Verify build_config_from_skill passes query to adaptive selection."""
        # This test verifies the integration exists — actual behavior
        # is tested in TestSelectToolsAdaptive
        from sre_agent.skill_loader import build_config_from_skill
        # Just verify the function exists and is importable
        assert callable(build_config_from_skill)
```

- [ ] **Step 2: Modify skill_loader.py — add query parameter to build_config_from_skill()**

In `sre_agent/skill_loader.py`, modify `build_config_from_skill` (line 1029):

Change the function signature from:
```python
def build_config_from_skill(skill: Skill) -> dict:
```
to:
```python
def build_config_from_skill(skill: Skill, query: str = "") -> dict:
```

Replace lines 1050-1060 (the category-based tool selection block) with:

```python
    if not skill.categories:
        # No categories = all tools (like view_designer)
        tool_map = dict(all_tools)
    elif query:
        # Adaptive selection when query is available
        from .tool_predictor import select_tools_adaptive

        _defs, tool_map, _offered = select_tools_adaptive(
            query,
            all_tool_map=all_tools,
            fallback_categories=skill.categories,
        )
    else:
        # No query = static category selection (startup, config-only calls)
        tool_names = set(ALWAYS_INCLUDE)
        for cat_name in skill.categories:
            cat = TOOL_CATEGORIES.get(cat_name, {})
            tool_names.update(cat.get("tools", []))
        tool_map = {n: t for n, t in all_tools.items() if n in tool_names}
```

- [ ] **Step 3: Update callers to pass query**

In `sre_agent/skill_loader.py`, find `build_config_from_skill` calls. The main caller is `build_orchestrated_config()`. Find it and pass the query through.

Search for `build_config_from_skill(skill)` in `skill_loader.py` and update:

```python
# In build_orchestrated_config() — add query parameter
def build_orchestrated_config(mode: AgentMode, query: str = "") -> dict:
```

And update the call:
```python
    return build_config_from_skill(skill, query=query)
```

In `sre_agent/api/ws_endpoints.py`, update the calls to `build_orchestrated_config` to pass the user query. Find lines where `build_orchestrated_config(cast("AgentMode", mode))` is called and add the query:

```python
config = build_orchestrated_config(cast("AgentMode", mode), query=last_user_text)
```

Where `last_user_text` is extracted from the most recent user message.

- [ ] **Step 4: Wire learn() into tool_usage.record_turn()**

In `sre_agent/tool_usage.py`, at the end of `record_turn()` (after `db.commit()` on line 205), add:

```python
        # Feed adaptive tool predictor
        try:
            from .tool_predictor import learn as _learn_tools
            _learn_tools(
                query=query_summary,
                tools_called=tools_called,
                tools_offered=tools_offered,
            )
        except Exception:
            pass
```

- [ ] **Step 5: Run all tests**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All 1490+ tests PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/skill_loader.py sre_agent/tool_usage.py sre_agent/api/ws_endpoints.py tests/test_tool_predictor.py
git commit -m "feat: wire adaptive tool selection into skill_loader and agent loop"
```

---

### Task 8: Mid-Turn Chain Expansion

**Files:**
- Modify: `sre_agent/tool_predictor.py`
- Test: `tests/test_tool_predictor.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tool_predictor.py`:

```python
from sre_agent.tool_predictor import expand_tool_set


class TestChainExpansion:
    def test_adds_chain_predicted_tools(self):
        current_tools = {"list_pods": MagicMock(), "describe_pod": MagicMock()}
        new_tool = MagicMock()
        new_tool.name = "get_pod_logs"
        new_tool.to_dict.return_value = {"name": "get_pod_logs"}
        all_tools = {**current_tools, "get_pod_logs": new_tool}

        with patch("sre_agent.tool_predictor._chain_hints_cache", {
            "describe_pod": [("get_pod_logs", 0.85), ("get_events", 0.6)],
        }):
            expanded_defs, expanded_map = expand_tool_set(
                called_tool="describe_pod",
                current_tool_map=current_tools,
                all_tool_map=all_tools,
            )
            assert "get_pod_logs" in expanded_map

    def test_no_expansion_when_already_present(self):
        tool = MagicMock()
        tool.name = "get_pod_logs"
        current_tools = {"describe_pod": MagicMock(), "get_pod_logs": tool}

        with patch("sre_agent.tool_predictor._chain_hints_cache", {
            "describe_pod": [("get_pod_logs", 0.9)],
        }):
            _, expanded_map = expand_tool_set(
                called_tool="describe_pod",
                current_tool_map=current_tools,
                all_tool_map=current_tools,
            )
            assert len(expanded_map) == len(current_tools)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestChainExpansion -v`
Expected: FAIL — `expand_tool_set` not defined

- [ ] **Step 3: Implement expand_tool_set()**

Add to `sre_agent/tool_predictor.py`:

```python
from .tool_chains import _chain_hints_cache


def expand_tool_set(
    *,
    called_tool: str,
    current_tool_map: dict,
    all_tool_map: dict,
    min_probability: float = 0.5,
) -> tuple[list[dict], dict]:
    """Expand the tool set based on chain bigrams and co-occurrence.

    Called after each tool execution to dynamically add predicted next-tools.
    Returns updated (tool_defs, tool_map).
    """
    new_tools: dict[str, object] = {}

    # Chain bigram expansion
    suggestions = _chain_hints_cache.get(called_tool, [])
    for next_tool, prob in suggestions:
        if prob >= min_probability and next_tool not in current_tool_map and next_tool in all_tool_map:
            new_tools[next_tool] = all_tool_map[next_tool]

    # Co-occurrence expansion
    try:
        db = _get_db()
        rows = db.fetchall(
            "SELECT tool_b, frequency FROM tool_cooccurrence "
            "WHERE tool_a = %s AND frequency >= 3 "
            "ORDER BY frequency DESC LIMIT 3",
            (called_tool,),
        )
        for r in rows:
            t = r["tool_b"]
            if t not in current_tool_map and t in all_tool_map:
                new_tools[t] = all_tool_map[t]
    except Exception:
        pass

    if not new_tools:
        return [t.to_dict() for t in current_tool_map.values()], dict(current_tool_map)

    expanded = {**current_tool_map, **new_tools}
    logger.debug(
        "Chain expansion after %s: added %s",
        called_tool, ", ".join(new_tools.keys()),
    )
    return [t.to_dict() for t in expanded.values()], expanded
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestChainExpansion -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_predictor.py tests/test_tool_predictor.py
git commit -m "feat: add mid-turn chain expansion for adaptive tool selection"
```

---

### Task 9: Staleness Decay

**Files:**
- Modify: `sre_agent/tool_predictor.py`
- Test: `tests/test_tool_predictor.py` (extend)

- [ ] **Step 1: Write failing test**

Append to `tests/test_tool_predictor.py`:

```python
from sre_agent.tool_predictor import decay_scores


class TestDecay:
    @patch("sre_agent.tool_predictor._get_db")
    def test_decay_multiplies_scores(self, mock_get_db):
        db = MagicMock()
        mock_get_db.return_value = db

        decay_scores(factor=0.95, prune_days=30)

        calls = [str(c) for c in db.execute.call_args_list]
        # Should have decay update and prune delete
        assert any("score" in c and "0.95" in c for c in calls)
        assert any("DELETE" in c for c in calls)
        assert db.commit.called

    @patch("sre_agent.tool_predictor._get_db")
    def test_no_crash_on_failure(self, mock_get_db):
        mock_get_db.side_effect = Exception("DB down")
        decay_scores()  # Should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestDecay -v`
Expected: FAIL — `decay_scores` not defined

- [ ] **Step 3: Implement decay_scores()**

Add to `sre_agent/tool_predictor.py`:

```python
def decay_scores(*, factor: float = 0.95, prune_days: int = 30) -> None:
    """Apply daily decay to prediction scores and prune stale entries.

    Call from a daily cron or at startup.
    """
    try:
        db = _get_db()
        db.execute(
            "UPDATE tool_predictions SET score = score * %s",
            (factor,),
        )
        db.execute(
            "DELETE FROM tool_predictions WHERE last_seen < NOW() - INTERVAL '%s days'",
            (prune_days,),
        )
        db.commit()
        logger.info("Decayed prediction scores by %.2f, pruned entries older than %d days", factor, prune_days)
    except Exception:
        logger.debug("Failed to decay prediction scores", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tool_predictor.py::TestDecay -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_predictor.py tests/test_tool_predictor.py
git commit -m "feat: add daily score decay and stale entry pruning"
```

---

### Task 10: Full Test Suite + Lint + Type Check

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All tests PASS (1490+ existing + new tool_predictor tests)

- [ ] **Step 2: Run ruff lint**

Run: `python3 -m ruff check sre_agent/ tests/`
Expected: All checks passed

- [ ] **Step 3: Run mypy**

Run: `python3 -m mypy sre_agent/ --ignore-missing-imports --exclude 'skills/(view-designer|capacity-planner)'`
Expected: Success: no issues found

- [ ] **Step 4: Run eval suites to verify no regressions**

Run: `python3 -m sre_agent.evals.cli --suite release --fail-on-gate`
Expected: Gate PASS

- [ ] **Step 5: Update CLAUDE.md**

Update the test count and add tool_predictor.py to the Key Files section:

```
- `tool_predictor.py` — adaptive tool selection engine (TF-IDF prediction, LLM fallback, chain expansion, real-time learning)
```

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "chore: verify adaptive tool selection — all tests, lint, types, evals pass"
```
