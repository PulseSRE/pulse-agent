"""Tests for migration 025 — re-keying inbox items orphaned by the namespace fix.

The bug this repairs was silent: v2.9.0 changed the correlation key format, and
every pre-existing open item quietly stopped matching any finding. Nothing
errored. The items simply froze at whatever they said that day and stayed in
the inbox forever, next to live duplicates of themselves.
"""

from __future__ import annotations

import json
import time

import pytest

from sre_agent.db import get_database
from sre_agent.db_migrations import _migrate_025_rekey_inbox_correlation_keys

OPEN = ("new", "triaged", "claimed", "in_progress", "agent_reviewing")


@pytest.fixture(autouse=True)
def _clean_inbox():
    db = get_database()
    db.execute("DELETE FROM inbox_items")
    db.commit()
    yield
    db.execute("DELETE FROM inbox_items")
    db.commit()


def _insert(item_id, correlation_key, status="triaged", namespace="kuadrant-system", title="Pod restarting"):
    resources = [{"kind": "Pod", "name": "limitador-op-864kb", "namespace": namespace}] if namespace else []
    now = int(time.time())
    get_database().execute(
        "INSERT INTO inbox_items (id, item_type, status, title, summary, correlation_key, "
        "resources, namespace, created_by, created_at, updated_at) "
        "VALUES (%s, 'task', %s, %s, '', %s, %s, '', 'system:monitor', %s, %s)",
        (item_id, status, title, correlation_key, json.dumps(resources), now, now),
    )
    get_database().commit()


def _row(item_id):
    return get_database().fetchone("SELECT * FROM inbox_items WHERE id = %s", (item_id,))


def _run():
    db = get_database()
    _migrate_025_rekey_inbox_correlation_keys(db)
    db.commit()


def test_orphan_gains_the_namespace_and_becomes_live_again():
    _insert("inb-orphan", "crashloop::Pod/limitador-operator-controller-manager")
    _run()
    row = _row("inb-orphan")
    assert row["correlation_key"] == "crashloop:kuadrant-system:Pod/limitador-operator-controller-manager"
    assert row["status"] == "triaged"


def test_the_namespace_column_is_backfilled_too():
    """It was never populated, so nothing could filter these by namespace."""
    _insert("inb-orphan", "crashloop::Pod/x")
    _run()
    assert _row("inb-orphan")["namespace"] == "kuadrant-system"


def test_an_orphan_duplicating_a_live_item_is_resolved_not_re_keyed():
    """Re-keying it would collide with the item that already covers the condition."""
    _insert("inb-orphan", "crashloop::Pod/limitador")
    _insert("inb-live", "crashloop:kuadrant-system:Pod/limitador")
    _run()
    assert _row("inb-orphan")["status"] == "resolved"
    assert _row("inb-orphan")["resolved_at"]
    assert _row("inb-live")["status"] == "triaged"
    assert _row("inb-live")["correlation_key"] == "crashloop:kuadrant-system:Pod/limitador"


def test_a_resolved_duplicate_does_not_block_re_keying():
    """Only open items compete for a key; history does not."""
    _insert("inb-orphan", "crashloop::Pod/limitador")
    _insert("inb-old", "crashloop:kuadrant-system:Pod/limitador", status="resolved")
    _run()
    assert _row("inb-orphan")["status"] == "triaged"
    assert _row("inb-orphan")["correlation_key"] == "crashloop:kuadrant-system:Pod/limitador"


def test_resolved_orphans_are_left_alone():
    """History is not cleanup, and the 24h reopen lookup reads resolved rows by key."""
    _insert("inb-history", "crashloop::Pod/limitador", status="resolved")
    _run()
    assert _row("inb-history")["correlation_key"] == "crashloop::Pod/limitador"


def test_items_without_a_recoverable_namespace_are_left_alone():
    """'category::Kind/name' is exactly what a cluster-scoped finding produces."""
    _insert("inb-cluster", "rbac_drift::Cluster/x", namespace="")
    _run()
    assert _row("inb-cluster")["correlation_key"] == "rbac_drift::Cluster/x"


def test_already_namespaced_items_are_untouched():
    _insert("inb-fine", "crashloop:kuadrant-system:Pod/limitador")
    _run()
    assert _row("inb-fine")["correlation_key"] == "crashloop:kuadrant-system:Pod/limitador"


def test_a_title_containing_colons_survives_the_rewrite():
    """The fallback key is f'{category}:{namespace}:{title}' — split_part would truncate it."""
    _insert("inb-colons", "alerts::Alert: disk 90% full: node-1")
    _run()
    assert _row("inb-colons")["correlation_key"] == "alerts:kuadrant-system:Alert: disk 90% full: node-1"


def test_running_the_migration_twice_changes_nothing():
    _insert("inb-orphan", "crashloop::Pod/limitador")
    _run()
    first = _row("inb-orphan")["correlation_key"]
    _run()
    assert _row("inb-orphan")["correlation_key"] == first


@pytest.mark.parametrize("status", OPEN)
def test_every_open_status_is_re_keyed(status):
    """An item claimed by a human is exactly the one you least want frozen."""
    _insert("inb-open", "crashloop::Pod/limitador", status=status)
    _run()
    assert _row("inb-open")["correlation_key"] == "crashloop:kuadrant-system:Pod/limitador"
