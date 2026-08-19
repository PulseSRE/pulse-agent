"""Tests for diagnose_stuck_deletion and remove_finalizer.

Most of these assert on *refusals*. remove_finalizer's value is not that it can
patch an object — kubectl does that in one line — it is that it declines the
cases where forcing the finalizer off silently abandons real cleanup.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from sre_agent.k8s_tools.finalizers import diagnose_stuck_deletion, remove_finalizer

MODULE = "sre_agent.k8s_tools.finalizers"


def _obj(
    kind="Namespace",
    name="demo",
    deletion_timestamp="2026-08-14T10:00:00Z",
    finalizers=(),
    spec_finalizers=(),
    conditions=(),
    owners=(),
):
    meta: dict = {"name": name}
    if deletion_timestamp:
        meta["deletionTimestamp"] = deletion_timestamp
    if finalizers:
        meta["finalizers"] = list(finalizers)
    if owners:
        meta["ownerReferences"] = list(owners)
    return {
        "kind": kind,
        "metadata": meta,
        "spec": {"finalizers": list(spec_finalizers)} if spec_finalizers else {},
        "status": {"conditions": list(conditions)},
    }


def _content_remaining(message="Some resources are remaining: dnsrecords.kuadrant.io has 3"):
    return {"type": "NamespaceContentRemaining", "status": "True", "message": message}


class _Api:
    """Stands in for the API server: reads come from ``serve``, writes are recorded.

    Reads go through ``k8s_client.get_raw_json`` rather than a stubbed
    ``call_api``. Stubbing ``call_api`` to hand back a bare dict is what let the
    describe_resource tests pass against code that could not work — the real
    client returns an HTTP response object, never a dict.
    """

    def __init__(self, client):
        self.client = client
        self.obj: dict | None = None

    def serve(self, obj: dict) -> None:
        self.obj = obj

    @property
    def writes(self):
        return [c for c in self.client.call_api.call_args_list if c.args[1] in ("PATCH", "PUT")]


@pytest.fixture
def api(monkeypatch):
    client = MagicMock()
    stub = _Api(client)
    monkeypatch.setattr("sre_agent.k8s_client.get_core_client", lambda: MagicMock(api_client=client))
    monkeypatch.setattr("sre_agent.k8s_client.get_raw_json", lambda path, operation="": stub.obj)
    return stub


# ── diagnose_stuck_deletion ───────────────────────────────────────────────


def test_diagnose_says_so_when_nothing_is_being_deleted(api):
    api.serve(_obj(deletion_timestamp=None))
    assert "not being deleted" in diagnose_stuck_deletion("Namespace", "demo")


def test_diagnose_lists_the_blocking_finalizers(api):
    api.serve(_obj(finalizers=["kuadrant.io/dnsrecord", "foo.io/bar"]))
    out = diagnose_stuck_deletion("Namespace", "demo")
    assert "kuadrant.io/dnsrecord" in out
    assert "foo.io/bar" in out


def test_diagnose_flags_control_plane_finalizers_as_not_forceable(api):
    api.serve(_obj(kind="PersistentVolumeClaim", finalizers=["kubernetes.io/pvc-protection"]))
    out = diagnose_stuck_deletion("PersistentVolumeClaim", "data-0", namespace="demo")
    assert "do not force" in out


def test_diagnose_surfaces_remaining_namespace_content(api):
    api.serve(_obj(spec_finalizers=["kubernetes"], conditions=[_content_remaining()]))
    out = diagnose_stuck_deletion("Namespace", "demo")
    assert "dnsrecords.kuadrant.io" in out
    assert "real fix" in out


def test_diagnose_points_at_the_node_when_no_finalizers_remain(api):
    api.serve(_obj(kind="Pod"))
    out = diagnose_stuck_deletion("Pod", "web-0", namespace="demo")
    assert "kubelet" in out


def test_diagnose_lists_owner_references(api):
    api.serve(_obj(finalizers=["x/y"], owners=[{"kind": "ReplicaSet", "name": "web-abc"}]))
    assert "ReplicaSet/web-abc" in diagnose_stuck_deletion("Pod", "web-0", namespace="demo")


def test_diagnose_never_writes(api):
    api.serve(_obj(finalizers=["x/y"]))
    diagnose_stuck_deletion("Namespace", "demo")
    assert api.writes == []


# ── remove_finalizer: refusals ────────────────────────────────────────────


def test_refuses_a_resource_that_is_not_being_deleted(api):
    api.serve(_obj(deletion_timestamp=None, finalizers=["x/y"]))
    out = remove_finalizer("Namespace", "demo", "x/y")
    assert "Refusing" in out
    assert api.writes == []


def test_refuses_control_plane_finalizers(api):
    """pvc-protection means a pod still mounts the claim. Forcing it leaks the volume."""
    api.serve(_obj(kind="PersistentVolumeClaim", finalizers=["kubernetes.io/pvc-protection"]))
    out = remove_finalizer("PersistentVolumeClaim", "data-0", "kubernetes.io/pvc-protection", namespace="demo")
    assert "Refusing" in out
    assert api.writes == []


def test_refuses_clearing_a_namespace_that_still_has_content(api):
    """The orphan-everything case: the objects survive with no namespace to reach them."""
    api.serve(_obj(spec_finalizers=["kubernetes"], conditions=[_content_remaining()]))
    out = remove_finalizer("Namespace", "demo", "kubernetes")
    assert "Refusing" in out
    assert "orphaned" in out
    assert api.writes == []


def test_refuses_an_empty_finalizer_argument(api):
    assert "required" in remove_finalizer("Namespace", "demo", "  ")
    assert api.writes == []


def test_reports_when_the_named_finalizer_is_absent(api):
    api.serve(_obj(finalizers=["a/b"]))
    out = remove_finalizer("Namespace", "demo", "c/d")
    assert "not present" in out
    assert "a/b" in out
    assert api.writes == []


# ── remove_finalizer: the paths that do write ─────────────────────────────


def test_removes_one_metadata_finalizer_and_leaves_the_rest(api):
    api.serve(_obj(finalizers=["a/b", "c/d"]))
    out = remove_finalizer("Namespace", "demo", "a/b")
    writes = api.writes
    assert len(writes) == 1
    body = json.loads(writes[0].kwargs["body"])
    assert body == {"metadata": {"finalizers": ["c/d"]}}
    assert "c/d" in out


def test_metadata_removal_uses_a_merge_patch(api):
    api.serve(_obj(finalizers=["a/b"]))
    remove_finalizer("Namespace", "demo", "a/b")
    call = api.writes[0]
    assert call.args[1] == "PATCH"
    assert call.kwargs["header_params"]["Content-Type"] == "application/merge-patch+json"


def test_empty_namespace_can_be_finalized(api):
    api.serve(_obj(spec_finalizers=["kubernetes"]))
    out = remove_finalizer("Namespace", "demo", "kubernetes")
    call = api.writes[0]
    assert call.args[0].endswith("/finalize")
    assert call.args[1] == "PUT"
    assert json.loads(call.kwargs["body"])["spec"]["finalizers"] == []
    assert "deletion can now complete" in out


def test_cluster_scoped_and_namespaced_paths_differ(api):
    api.serve(_obj(finalizers=["a/b"]))
    remove_finalizer("CustomResourceDefinition", "widgets.example.io", "a/b", group="apiextensions.k8s.io")
    cluster_path = api.writes[0].args[0]
    api.client.reset_mock()
    api.serve(_obj(finalizers=["a/b"]))
    remove_finalizer("Pod", "web-0", "a/b", namespace="demo")
    namespaced_path = api.writes[0].args[0]
    assert "/namespaces/" not in cluster_path
    assert cluster_path.startswith("/apis/apiextensions.k8s.io/v1/")
    assert "/namespaces/demo/pods/web-0" in namespaced_path


def test_api_failure_is_returned_not_raised(api):
    api.serve(_obj(finalizers=["a/b"]))

    def _fail(*a, **k):
        raise RuntimeError("connection reset")

    api.client.call_api.side_effect = _fail
    assert "connection reset" in remove_finalizer("Namespace", "demo", "a/b")


# ── Gate wiring ───────────────────────────────────────────────────────────


def test_remove_finalizer_is_gated_and_diagnose_is_not():
    """A destructive tool registered as a read would run unattended."""
    import sre_agent.k8s_tools  # noqa: F401  — registers the tools
    from sre_agent.tool_registry import get_write_tools

    writes = get_write_tools()
    assert "remove_finalizer" in writes
    assert "diagnose_stuck_deletion" not in writes
