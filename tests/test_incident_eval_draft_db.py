"""Selected incident linkage and chronology use actual PostgreSQL records."""

import json

from sre_agent.db import Database
from sre_agent.evals.incident_draft import build_draft, load_selected_records
from sre_agent.repositories import monitor_repo
from tests.conftest import _TEST_DB_URL, truncate_core_tables


def test_selected_action_does_not_take_later_or_other_incident_diagnosis(monkeypatch):
    db = Database(_TEST_DB_URL)
    repo = monitor_repo.MonitorRepository(db)
    truncate_core_tables(db)
    try:
        db.execute(
            "INSERT INTO findings (id, cluster, message, timestamp, resolved) VALUES (?, ?, ?, ?, ?)",
            ("synthetic-finding", "test", "synthetic secret omitted", 100, 1),
        )
        db.execute(
            "INSERT INTO actions (id, finding_id, timestamp, status, verification_status, verification_evidence, verification_timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("synthetic-action", "synthetic-finding", 200, "completed", "verified", "synthetic observed read", 300),
        )
        for name, finding, timestamp, status in [
            ("preceding", "synthetic-finding", 150, "completed"),
            ("later", "synthetic-finding", 250, "completed"),
            ("other", "another-finding", 175, "completed"),
            ("failed", "synthetic-finding", 190, "failed"),
        ]:
            db.execute(
                "INSERT INTO investigations (id, finding_id, timestamp, status, evidence) VALUES (?, ?, ?, ?, ?)",
                (name, finding, timestamp, status, json.dumps(["synthetic observed signal"])),
            )
        db.commit()
        monkeypatch.setattr(monitor_repo, "get_monitor_repo", lambda: repo)
        action, finding, investigation = load_selected_records("synthetic-action")
        assert investigation["id"] == "preceding"
        draft = build_draft(action, finding, investigation)
        assert draft["review"]["status"] == "needs_review"
        assert "synthetic observed signal" not in json.dumps(draft)
    finally:
        truncate_core_tables(db)
