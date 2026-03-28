"""Pattern recognition from incident history."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .store import IncidentStore


def detect_patterns(store: IncidentStore) -> list[dict]:
    """Analyze incident history and detect recurring patterns.

    Returns list of newly detected patterns.
    """
    incidents = store.conn.execute(
        "SELECT * FROM incidents ORDER BY timestamp DESC LIMIT 200"
    ).fetchall()
    incidents = [dict(r) for r in incidents]

    if len(incidents) < 3:
        return []

    new_patterns = []

    # Keyword clustering — find frequently co-occurring keywords
    keyword_groups: Counter = Counter()
    for inc in incidents:
        kws = sorted(set(inc["query_keywords"].split()))
        for i in range(len(kws)):
            for j in range(i + 1, min(i + 3, len(kws))):
                keyword_groups[(kws[i], kws[j])] += 1

    for (kw1, kw2), count in keyword_groups.most_common(10):
        if count >= 3:
            matching = [
                inc["id"] for inc in incidents
                if kw1 in inc["query_keywords"] and kw2 in inc["query_keywords"]
            ]
            if len(matching) >= 3:
                existing = store.conn.execute(
                    "SELECT id FROM patterns WHERE keywords LIKE ? AND keywords LIKE ?",
                    (f"%{kw1}%", f"%{kw2}%")
                ).fetchall()
                if not existing:
                    pid = store.record_pattern(
                        pattern_type="recurring",
                        description=f"Recurring issue involving '{kw1}' and '{kw2}' ({count} occurrences)",
                        keywords=f"{kw1} {kw2}",
                        incident_ids=matching,
                    )
                    new_patterns.append({"id": pid, "type": "recurring", "keywords": f"{kw1} {kw2}"})

    # Time-based patterns — same error type at similar times
    seen_time_patterns: set[str] = set()
    for inc in incidents:
        if not inc["error_type"]:
            continue
        ts = datetime.fromisoformat(inc["timestamp"])
        hour = ts.hour
        dow = ts.strftime("%A")
        key = f"{inc['error_type']}-{dow}-{hour}"
        if key in seen_time_patterns:
            continue

        same_time = [
            i for i in incidents
            if i["error_type"] == inc["error_type"]
            and i["id"] != inc["id"]
            and abs(datetime.fromisoformat(i["timestamp"]).hour - hour) <= 1
            and datetime.fromisoformat(i["timestamp"]).strftime("%A") == dow
        ]
        if len(same_time) >= 2:
            seen_time_patterns.add(key)
            ids = [inc["id"]] + [i["id"] for i in same_time]
            existing = store.conn.execute(
                "SELECT id FROM patterns WHERE pattern_type = 'time_based' AND keywords LIKE ?",
                (f"%{inc['error_type'].lower()}%",)
            ).fetchall()
            if not existing:
                pid = store.record_pattern(
                    pattern_type="time_based",
                    description=f"{inc['error_type']} tends to occur on {dow}s around {hour}:00",
                    keywords=inc["error_type"].lower(),
                    incident_ids=ids,
                    metadata={"day_of_week": dow, "hour": hour},
                )
                new_patterns.append({"id": pid, "type": "time_based"})

    # Correlation detection: if category A is often followed by category B
    # within 30 minutes, record the correlation (Improvement #5)
    category_events: list[tuple[str, datetime, int]] = []
    for inc in incidents:
        if inc["error_type"]:
            try:
                ts = datetime.fromisoformat(inc["timestamp"])
                category_events.append((inc["error_type"], ts, inc["id"]))
            except (ValueError, TypeError):
                continue

    # Sort chronologically
    category_events.sort(key=lambda x: x[1])

    # Count how often category A is followed by category B within 30 min
    correlation_counts: Counter = Counter()
    correlation_ids: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    window = timedelta(minutes=30)

    for i, (cat_a, ts_a, id_a) in enumerate(category_events):
        for j in range(i + 1, min(i + 20, len(category_events))):  # bounded lookahead
            cat_b, ts_b, id_b = category_events[j]
            if ts_b - ts_a > window:
                break
            if cat_a != cat_b:
                pair = (cat_a, cat_b)
                correlation_counts[pair] += 1
                correlation_ids[pair].extend([id_a, id_b])

    for (cat_a, cat_b), count in correlation_counts.most_common(5):
        if count >= 3:
            ids = sorted(set(correlation_ids[(cat_a, cat_b)]))[:20]
            existing = store.conn.execute(
                "SELECT id FROM patterns WHERE pattern_type = 'correlation' AND keywords LIKE ? AND keywords LIKE ?",
                (f"%{cat_a.lower()}%", f"%{cat_b.lower()}%")
            ).fetchall()
            if not existing:
                pid = store.record_pattern(
                    pattern_type="correlation",
                    description=f"{cat_a} is often followed by {cat_b} within 30 minutes ({count} occurrences)",
                    keywords=f"{cat_a.lower()} {cat_b.lower()}",
                    incident_ids=ids,
                    metadata={"category_a": cat_a, "category_b": cat_b, "window_minutes": 30},
                )
                new_patterns.append({"id": pid, "type": "correlation", "keywords": f"{cat_a} {cat_b}"})

    return new_patterns
