"""Tests for resource snapshots — the undo Pulse did not have."""

from __future__ import annotations

import pytest

from sre_agent.snapshot import (
    SUPPORTED_KINDS,
    _clean_metadata,
    describe,
    from_json,
    restore,
    to_json,
)


class TestMetadataCleaning:
    """Server-owned fields must not be replayed."""

    def test_resource_version_is_stripped(self):
        # replaying it makes the restore fail with a conflict
        assert "resourceVersion" not in _clean_metadata({"name": "x", "resourceVersion": "12345"})

    def test_server_owned_fields_are_stripped(self):
        meta = {
            "name": "x",
            "uid": "abc",
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "generation": 4,
            "managedFields": [{"manager": "kubectl"}],
            "ownerReferences": [{"kind": "ReplicaSet"}],
        }
        cleaned = _clean_metadata(meta)
        assert cleaned == {"name": "x"}

    def test_last_applied_annotation_is_dropped(self):
        # it is a second full copy of the object
        meta = {"name": "x", "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...}"}}
        assert "annotations" not in _clean_metadata(meta)

    def test_real_annotations_survive(self):
        meta = {"name": "x", "annotations": {"team": "commerce"}}
        assert _clean_metadata(meta)["annotations"] == {"team": "commerce"}

    def test_empty_metadata_is_safe(self):
        assert _clean_metadata({}) == {}
        assert _clean_metadata(None) == {}


class TestDescribe:
    def test_no_snapshot_says_so_plainly(self):
        out = describe(None)
        assert "cannot be undone" in out

    def test_a_snapshot_names_what_it_restores(self):
        out = describe({"kind": "Deployment", "namespace": "prod", "name": "api"})
        assert "Deployment prod/api" in out


class TestSerialisation:
    def test_round_trip(self):
        snap = {"kind": "Deployment", "name": "api", "namespace": "prod", "spec": {"replicas": 3}}
        assert from_json(to_json(snap)) == snap

    def test_absent_snapshot_serialises_to_empty(self):
        assert to_json(None) == ""

    def test_garbage_deserialises_to_none(self):
        for blob in ("", None, "not json", "[1,2,3]"):
            assert from_json(blob) is None


class TestRestoreRefusesTheUnrestorable:
    """A rollback that fails quietly is worse than one that never existed."""

    def test_empty_snapshot_raises(self):
        with pytest.raises(ValueError, match="No snapshot"):
            restore({})

    def test_unsupported_kind_raises(self):
        with pytest.raises(ValueError, match="not restorable"):
            restore({"kind": "Pod", "name": "x", "namespace": "y"})

    def test_missing_name_raises(self):
        with pytest.raises(ValueError, match="not restorable"):
            restore({"kind": "Deployment", "namespace": "y"})


class TestRollbackInfo:
    """A snapshot makes rollback available for any tool, not just restarts."""

    def test_a_snapshot_beats_the_restart_only_path(self):
        from sre_agent.monitor.findings import _make_rollback_info

        action = {"status": "completed", "tool": "patch_resources", "beforeSnapshot": '{"kind":"Deployment"}'}
        available, blob = _make_rollback_info(action, finding=None)
        assert available == 1, "patch_resources had no rollback before snapshots"
        assert "restore_snapshot" in blob

    def test_no_snapshot_falls_back_to_the_old_behaviour(self):
        from sre_agent.monitor.findings import _make_rollback_info

        action = {"status": "completed", "tool": "patch_resources"}
        assert _make_rollback_info(action, finding=None) == (0, "")

    def test_supported_kinds_are_the_ones_that_hold_desired_state(self):
        for kind in ("Deployment", "StatefulSet", "DaemonSet", "ConfigMap"):
            assert kind in SUPPORTED_KINDS
