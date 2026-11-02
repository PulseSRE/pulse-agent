"""check_upgrade_api_removals: go / no_go / unknown before an OpenShift minor upgrade.

Fixtures are the shapes OpenShift returns for APIRequestCount, ClusterVersion
and the admin-gates / admin-acks ConfigMaps. No cluster is contacted.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from kubernetes.client.rest import ApiException

from sre_agent.errors import ToolError
from sre_agent.k8s_tools import WRITE_TOOLS, check_upgrade_api_removals
from sre_agent.self_tools import list_deprecated_apis
from sre_agent.tool_categories import TOOL_CATEGORIES
from sre_agent.tool_registry import TOOL_REGISTRY
from sre_agent.upgrade_api_removals import (
    compute_upgrade_api_removals,
    kube_minor_for_ocp,
    render_upgrade_api_removals,
    split_api_name,
)

GATE_419 = "ack-4.18-kube-1.32-api-removals-in-4.19"
GATE_418 = "ack-4.17-kube-1.31-api-removals-in-4.18"
GATE_420 = "ack-4.19-kube-1.33-api-removals-in-4.20"
GATES = {
    GATE_418: "Kubernetes 1.31 and therefore OpenShift 4.18 remove several APIs.",
    GATE_419: "Kubernetes 1.32 and therefore OpenShift 4.19 remove several APIs which require admin consideration.",
    GATE_420: "Kubernetes 1.33 and therefore OpenShift 4.20 remove several APIs.",
}


def _err(status: int, message: str = "") -> ToolError:
    return ToolError(message=message or f"status {status}", category="server", status_code=status)


def _cv(
    version: str = "4.18.3", updates: tuple[str, ...] = ("4.18.5", "4.19.1"), conditional: tuple[str, ...] = ()
) -> dict:
    return {
        "spec": {"channel": "stable-4.19"},
        "status": {
            "desired": {"version": version},
            "history": [
                {"state": "Partial", "version": "4.18.9"},
                {"state": "Completed", "version": version},
            ],
            "availableUpdates": [{"version": v} for v in updates],
            "conditionalUpdates": [{"release": {"version": v}, "risks": []} for v in conditional],
            "conditions": [{"type": "Upgradeable", "status": "True"}],
        },
    }


def _hour(*users: tuple[str, str, int], node: str = "master-0") -> dict:
    return {
        "byNode": [
            {
                "nodeName": node,
                "byUser": [{"username": u, "userAgent": ua, "requestCount": n, "byVerb": []} for u, ua, n in users],
            }
        ]
    }


def _arc(name: str, removed: str | None, count: int, last24h: list[dict] | None = None) -> dict:
    status: dict = {"requestCount": count, "last24h": last24h or []}
    if removed is not None:
        status["removedInRelease"] = removed
    return {"metadata": {"name": name}, "status": status}


FLOWSCHEMA = "flowschemas.v1beta3.flowcontrol.apiserver.k8s.io"
PRIORITY = "prioritylevelconfigurations.v1beta3.flowcontrol.apiserver.k8s.io"
LATER = "widgets.v1beta1.example.io"
GONE = "podsecuritypolicies.v1beta1.policy"


def _arcs(in_use: bool = True) -> list[dict]:
    flow = (
        _arc(
            FLOWSCHEMA,
            "1.32",
            130,
            [
                _hour(
                    ("system:serviceaccount:openshift-cluster-version:default", "cluster-version-operator/v1", 90),
                    ("alice", "kubectl/v1.31.0", 5),
                ),
                _hour(("alice", "kubectl/v1.31.0", 15), node="master-1"),
                _hour(("ci-bot", "helm/v3.14", 20)),
            ],
        )
        if in_use
        else _arc(FLOWSCHEMA, "1.32", 0)
    )
    return [
        flow,
        _arc(PRIORITY, "1.32", 0),
        _arc(LATER, "1.33", 7, [_hour(("bob", "argocd/v2", 7))]),
        _arc(GONE, "1.25", 3, [_hour(("legacy", "old/v1", 3))]),
        _arc("pods.v1", None, 99999),
    ]


@pytest.fixture(autouse=True)
def _fresh_generator_cache():
    from sre_agent.inbox_generators import _reset_upgrade_api_cache

    _reset_upgrade_api_cache()
    yield
    _reset_upgrade_api_cache()


def _upgradeable_false(cv: dict, reason: str, message: str) -> dict:
    cv["status"]["conditions"] = [{"type": "Upgradeable", "status": "False", "reason": reason, "message": message}]
    return cv


def _compute(cv=None, arcs=None, gates=None, acks=None, target: str = "") -> dict:
    return compute_upgrade_api_removals(
        _cv() if cv is None else cv,
        _arcs() if arcs is None else arcs,
        {"data": GATES} if gates is None else gates,
        {"data": {GATE_418: "true", GATE_419: "true"}} if acks is None else acks,
        target,
    )


class TestVersionMapping:
    def test_ocp_to_kubernetes(self):
        assert kube_minor_for_ocp((4, 16)) == (1, 29)
        assert kube_minor_for_ocp((4, 17)) == (1, 30)
        assert kube_minor_for_ocp((4, 18)) == (1, 31)
        assert kube_minor_for_ocp((4, 19)) == (1, 32)
        assert kube_minor_for_ocp((3, 11)) is None

    def test_split_api_name(self):
        assert split_api_name(FLOWSCHEMA) == {
            "resource": "flowschemas",
            "version": "v1beta3",
            "group": "flowcontrol.apiserver.k8s.io",
        }
        assert split_api_name("pods.v1") == {"resource": "pods", "version": "v1", "group": ""}


class TestCompute:
    def test_in_use_removed_api_is_no_go_with_callers(self):
        data = _compute()
        assert data["status"] == "no_go"
        assert data["cause"] is None
        assert data["current"] == {"ocp": "4.18.3", "kubernetes": "1.31"}
        assert data["target"] == {"ocp": "4.19", "kubernetes": "1.32", "source": "available_update"}
        assert "1 API removed in Kubernetes 1.32 is still being called" in data["reason"]
        [api] = data["inUse"]
        assert api["name"] == FLOWSCHEMA
        assert api["group"] == "flowcontrol.apiserver.k8s.io"
        assert api["requestCount"] == 130
        # Summed across hours and nodes; people before platform callers, never hidden.
        assert api["callers"] == [
            {"username": "alice", "userAgent": "kubectl/v1.31.0", "count": 20, "platform": False},
            {"username": "ci-bot", "userAgent": "helm/v3.14", "count": 20, "platform": False},
            {
                "username": "system:serviceaccount:openshift-cluster-version:default",
                "userAgent": "cluster-version-operator/v1",
                "count": 90,
                "platform": True,
            },
        ]
        assert api["callerCount"] == 3
        assert [a["name"] for a in data["unused"]] == [PRIORITY]
        # A later removal is reported as scheduled, not as this upgrade's blocker;
        # an API already removed in the running release is not reported at all.
        assert [a["name"] for a in data["scheduledForRemoval"]] == [FLOWSCHEMA, PRIORITY, LATER]
        assert [(g["key"], g["acked"]) for g in data["gates"]] == [(GATE_419, True)]

    def test_only_unused_removed_apis_is_go(self):
        data = _compute(arcs=_arcs(in_use=False))
        assert data["status"] == "go"
        assert data["inUse"] == []
        assert sorted(a["name"] for a in data["unused"]) == [FLOWSCHEMA, PRIORITY]
        assert "2 removed APIs are unused" in data["reason"]
        assert "1 admin gate acknowledged" in data["reason"]

    def test_unacknowledged_gate_is_no_go(self):
        data = _compute(arcs=_arcs(in_use=False), acks={"data": {GATE_418: "true"}})
        assert data["status"] == "no_go"
        assert data["reason"] == "Upgrade to 4.19 is blocked: 1 admin gate for 4.19 not acknowledged."
        assert data["gates"] == [
            {"key": GATE_419, "description": GATES[GATE_419], "release": "4.19", "acked": False},
        ]

    def test_acknowledged_gate_is_go(self):
        data = _compute(arcs=_arcs(in_use=False), acks={"data": {GATE_419: "true"}})
        assert data["status"] == "go"
        assert data["gates"][0]["acked"] is True

    @pytest.mark.parametrize("value", ["True", " true", "TRUE", "yes", ""])
    def test_only_exactly_true_is_an_ack(self, value):
        data = _compute(arcs=_arcs(in_use=False), acks={"data": {GATE_419: value}})
        assert data["status"] == "no_go"
        assert data["gates"][0]["acked"] is False

    def test_go_reason_says_what_24h_cannot_see(self):
        data = _compute(arcs=_arcs(in_use=False))
        assert "less often than daily" in data["reason"]

    def test_missing_acks_configmap_means_nothing_acknowledged(self):
        data = _compute(arcs=_arcs(in_use=False), acks=_err(404))
        assert data["status"] == "no_go"
        assert data["gates"][0]["acked"] is False

    def test_missing_gates_configmap_is_could_not_check(self):
        # The CVO treats a missing admin-gates ConfigMap as not upgradeable.
        data = _compute(arcs=_arcs(in_use=False), gates=_err(404))
        assert data["status"] == "unknown"
        assert data["cause"] == "read_failed"
        assert "admin-gates does not exist" in data["reason"]

    @pytest.mark.parametrize("status", [403, 500])
    def test_unreadable_apirequestcounts_is_unknown(self, status):
        data = _compute(arcs=_err(status, "forbidden"))
        assert data["status"] == "unknown"
        assert data["cause"] == "read_failed"
        assert data["reason"].startswith("Could not check: could not read APIRequestCounts")
        assert data["inUse"] == [] and data["unused"] == []

    def test_apirequestcount_not_served_is_unknown_not_available(self):
        data = _compute(arcs=_err(404))
        assert data["status"] == "unknown"
        assert "not available" in data["reason"]
        assert "not served on this cluster" in data["checks"]["apiRequestCounts"]

    @pytest.mark.parametrize(
        ("which", "status"), [("gates", 403), ("gates", 404), ("gates", 500), ("acks", 403), ("acks", 500)]
    )
    def test_unreadable_configmaps_are_unknown(self, which, status):
        data = _compute(arcs=_arcs(in_use=False), **{which: _err(status)})
        assert data["status"] == "unknown"
        assert data["cause"] == "read_failed"
        assert "Could not check" in data["reason"]
        if which == "acks":
            assert data["gates"][0]["acked"] is None

    def test_proven_blocker_stays_no_go_when_another_read_failed(self):
        data = _compute(gates=_err(403, "forbidden"))
        assert data["status"] == "no_go"
        assert "Also could not check" in data["reason"]
        assert "admin-gates" in data["reason"]

    def test_planning_window_falls_back_to_next_minor(self):
        # stable-4.18 offers only z-streams until the admin switches channel;
        # that is when the admin needs this answer.
        data = _compute(cv=_cv(updates=("4.18.5",)), acks={"data": {}})
        assert data["target"] == {"ocp": "4.19", "kubernetes": "1.32", "source": "next_minor"}
        assert data["status"] == "no_go"
        assert [a["name"] for a in data["inUse"]] == [FLOWSCHEMA]
        assert [(g["key"], g["acked"]) for g in data["gates"]] == [(GATE_419, False)]
        assert "1 admin gate for 4.19 not acknowledged" in data["reason"]

    def test_no_current_version_is_unknown(self):
        cv = _cv()
        cv["status"]["history"] = []
        cv["status"]["desired"] = {}
        data = _compute(cv=cv)
        assert data["status"] == "unknown"
        assert data["cause"] == "no_current"

    def test_upgradeable_false_admin_ack_required_is_no_go(self):
        cv = _upgradeable_false(_cv(), "AdminAckRequired", "Kubernetes 1.32 removes APIs; admin-ack required")
        data = _compute(cv=cv, arcs=_arcs(in_use=False))  # every gate we can see is acked
        assert data["status"] == "no_go"
        assert (
            "ClusterVersion reports Upgradeable=False (AdminAckRequired): "
            "Kubernetes 1.32 removes APIs; admin-ack required"
        ) in data["reason"]

    def test_any_upgradeable_false_blocks_a_minor_upgrade(self):
        cv = _upgradeable_false(
            _cv(), "ClusterOperatorsNotUpgradeable", "Cluster operator machine-config should not be upgraded"
        )
        data = _compute(cv=cv, arcs=_arcs(in_use=False))
        assert data["status"] == "no_go"
        assert "Upgradeable=False (ClusterOperatorsNotUpgradeable)" in data["reason"]
        assert "machine-config should not be upgraded" in data["reason"]

    def test_gate_scoped_by_current_minor_prefix(self):
        gates = {
            "ack-4.18-cgroups-v1-removal": "cgroups v1 is removed.",  # no -in- suffix, current minor prefix
            "ack-4.17-old-step": "Gate of an earlier step.",  # earlier step: out of scope
            "ack-4.19-next-step": "Gate of a later step.",  # step out of 4.19: beyond a 4.19 target
            "unrecognised-gate-key": "Unknown shape.",  # conservative: in scope
        }
        data = _compute(arcs=_arcs(in_use=False), gates={"data": gates}, acks={"data": {}})
        scoped = {g["key"]: g["release"] for g in data["gates"]}
        assert scoped == {"ack-4.18-cgroups-v1-removal": "4.19", "unrecognised-gate-key": None}
        assert data["status"] == "no_go"
        assert "2 admin gates for 4.19 not acknowledged" in data["reason"]

    def test_gate_key_with_unexpected_kube_minor_warns(self):
        key = "ack-4.18-kube-1.33-api-removals-in-4.19"
        data = _compute(arcs=_arcs(in_use=False), gates={"data": {key: "x"}}, acks={"data": {key: "true"}})
        assert data["status"] == "go"
        assert data["warnings"] and "Kubernetes 1.33" in data["warnings"][0]
        assert _compute()["warnings"] == []

    def test_caller_identity_is_sanitised_and_capped(self):
        nasty = "evil\x1b[31m\nuser" + "x" * 300
        data = _compute(arcs=[_arc(FLOWSCHEMA, "1.32", 1, [_hour((nasty, "agent\x00/1", 1))])])
        [caller] = data["inUse"][0]["callers"]
        assert caller["username"].startswith("evil[31muser")
        assert len(caller["username"]) == 120
        assert caller["userAgent"] == "agent/1"

    def test_conditional_update_counts_as_offered(self):
        data = _compute(cv=_cv(updates=("4.18.5",), conditional=("4.19.0",)))
        assert data["target"]["ocp"] == "4.19"

    def test_requested_target_two_minors_ahead(self):
        data = _compute(cv=_cv(updates=()), target="4.20")
        assert data["target"] == {"ocp": "4.20", "kubernetes": "1.33", "source": "requested"}
        assert {a["name"] for a in data["inUse"]} == {FLOWSCHEMA, LATER}
        assert [g["key"] for g in data["gates"]] == [GATE_419, GATE_420]
        assert data["status"] == "no_go"
        assert "1 admin gate for 4.20 not acknowledged" in data["reason"]

    def test_go_two_minors_ahead_says_later_gates_are_not_covered(self):
        acks = {"data": {GATE_419: "true", GATE_420: "true"}}
        data = _compute(cv=_cv(updates=()), arcs=[_arc(PRIORITY, "1.32", 0)], acks=acks, target="4.20")
        assert data["status"] == "go"
        assert "Only the admin gates published so far (for the step out of 4.18)" in data["reason"]

    def test_removal_releases_sort_numerically(self):
        arcs = [_arc("a.v1beta1.x.io", "1.100", 0), _arc("b.v1beta1.x.io", "1.40", 0)]
        data = _compute(arcs=arcs)
        assert [a["removedInRelease"] for a in data["scheduledForRemoval"]] == ["1.40", "1.100"]

    def test_requested_target_not_newer_is_unknown(self):
        data = _compute(target="4.18.9")
        assert data["status"] == "unknown"
        assert data["cause"] == "bad_target"

    def test_clusterversion_not_found_is_not_openshift(self):
        data = _compute(cv=_err(404))
        assert data["status"] == "unknown"
        assert data["cause"] == "not_openshift"
        assert "not an OpenShift cluster" in data["reason"]

    def test_clusterversion_forbidden_is_could_not_check(self):
        data = _compute(cv=_err(403, "forbidden"))
        assert data["status"] == "unknown"
        assert data["cause"] == "read_failed"
        assert "not an OpenShift cluster" not in data["reason"]

    def test_callers_beyond_the_cap_are_counted(self):
        users = [(f"user-{i:02d}", "kubectl", 1) for i in range(15)]
        data = _compute(arcs=[_arc(FLOWSCHEMA, "1.32", 15, [_hour(*users)])])
        [api] = data["inUse"]
        assert len(api["callers"]) == 10
        assert api["callerCount"] == 15
        assert "(+5 more callers)" in render_upgrade_api_removals(data)


def _wire(mock_k8s, cv=None, arcs=None, gates=None, acks=None):
    custom, core = mock_k8s["custom"], mock_k8s["core"]
    if isinstance(cv, Exception):
        custom.get_cluster_custom_object.side_effect = cv
    else:
        custom.get_cluster_custom_object.side_effect = None
        custom.get_cluster_custom_object.return_value = _cv() if cv is None else cv
    if isinstance(arcs, Exception):
        custom.list_cluster_custom_object.side_effect = arcs
    else:
        custom.list_cluster_custom_object.side_effect = None
        custom.list_cluster_custom_object.return_value = {"items": _arcs() if arcs is None else arcs}
    cms = {
        ("admin-gates", "openshift-config-managed"): GATES if gates is None else gates,
        ("admin-acks", "openshift-config"): {GATE_419: "true"} if acks is None else acks,
    }

    def _read(name, namespace):
        value = cms[(name, namespace)]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(data=copy.deepcopy(value))

    core.read_namespaced_config_map.side_effect = _read


class TestTool:
    def test_no_go_report(self, mock_k8s):
        _wire(mock_k8s)
        text = check_upgrade_api_removals.call({})
        assert text.startswith("Verdict: NO-GO — Upgrade to 4.19 is blocked")
        assert "Current: OpenShift 4.18.3 (Kubernetes 1.31)" in text
        assert "Target: OpenShift 4.19 (Kubernetes 1.32) (next minor on offer)" in text
        assert f"- {FLOWSCHEMA} (removed in 1.32): 130 requests / 24h" in text
        assert "alice (kubectl/v1.31.0): 20" in text
        assert "cluster-version-operator/v1): 90 [platform]" in text
        assert f"- {PRIORITY} (removed in 1.32)" in text
        assert f"- {GATE_419}: acknowledged" in text
        mock_k8s["custom"].list_cluster_custom_object.assert_called_once_with(
            "apiserver.openshift.io", "v1", "apirequestcounts"
        )

    def test_go_report(self, mock_k8s):
        _wire(mock_k8s, arcs=_arcs(in_use=False))
        text = check_upgrade_api_removals.call({"target_version": "4.19"})
        assert text.startswith("Verdict: GO")
        assert "(requested)" in text

    def test_unacked_gate_tells_how_to_ack(self, mock_k8s):
        _wire(mock_k8s, arcs=_arcs(in_use=False), acks={})
        text = check_upgrade_api_removals.call({})
        assert "Verdict: NO-GO" in text
        assert f"- {GATE_419}: NOT acknowledged" in text
        assert "openshift-config/admin-acks" in text

    def test_forbidden_is_unknown_never_go(self, mock_k8s):
        _wire(mock_k8s, arcs=ApiException(status=403, reason="Forbidden"))
        text = check_upgrade_api_removals.call({})
        assert text.startswith("Verdict: UNKNOWN — Could not check")
        assert "Not checked: apiRequestCounts" in text

    def test_server_error_on_configmap_is_unknown(self, mock_k8s):
        _wire(mock_k8s, arcs=_arcs(in_use=False), acks=ApiException(status=500, reason="Internal Server Error"))
        text = check_upgrade_api_removals.call({})
        assert text.startswith("Verdict: UNKNOWN")
        assert "acknowledgement unknown" in text

    def test_apirequestcount_not_served(self, mock_k8s):
        _wire(mock_k8s, arcs=ApiException(status=404, reason="Not Found"))
        text = check_upgrade_api_removals.call({})
        assert text.startswith("Verdict: UNKNOWN")
        assert "not available" in text

    def test_planning_window_label(self, mock_k8s):
        _wire(mock_k8s, cv=_cv(updates=("4.18.5",)))
        text = check_upgrade_api_removals.call({})
        assert "Target: OpenShift 4.19 (Kubernetes 1.32) (next minor; not yet offered on channel stable-4.19)" in text

    def test_no_target_skips_listing_apirequestcounts(self, mock_k8s):
        _wire(mock_k8s)
        text = check_upgrade_api_removals.call({"target_version": "4.17"})
        assert text.startswith("Verdict: UNKNOWN — Cannot decide")
        mock_k8s["custom"].list_cluster_custom_object.assert_not_called()
        mock_k8s["core"].read_namespaced_config_map.assert_not_called()

    def test_rejects_malformed_target_without_reading(self, mock_k8s):
        _wire(mock_k8s)
        text = check_upgrade_api_removals.call({"target_version": "latest; rm -rf"})
        assert text.startswith("Invalid target_version")
        mock_k8s["custom"].get_cluster_custom_object.assert_not_called()

    def test_registered_read_only(self):
        assert "check_upgrade_api_removals" in TOOL_REGISTRY
        assert "check_upgrade_api_removals" not in WRITE_TOOLS
        assert "check_upgrade_api_removals" in TOOL_CATEGORIES["operations"]["tools"]


class TestListDeprecatedApis:
    def test_reports_real_removals_not_multi_version_groups(self, mock_k8s):
        _wire(mock_k8s)
        text = list_deprecated_apis.call({})
        assert "non-preferred" not in text
        assert FLOWSCHEMA in text
        assert "Deprecated and scheduled for removal in a later release:" in text
        assert f"- {LATER} (removed in 1.33): 7 requests / 24h" in text
        assert "bob (argocd/v2): 7" in text
        assert GONE not in text

    def test_forbidden_is_not_a_clean_bill(self, mock_k8s):
        _wire(mock_k8s, arcs=ApiException(status=403, reason="Forbidden"))
        text = list_deprecated_apis.call({})
        assert "UNKNOWN" in text
        assert "No deprecated" not in text


class TestInboxGenerator:
    def test_no_go_creates_one_plain_item(self, mock_k8s):
        from sre_agent.inbox_generators import gen_upgrade_api_removals

        _wire(mock_k8s)
        [item] = gen_upgrade_api_removals()
        assert item["title"] == "Upgrade to 4.19 blocked: 1 removed API still in use"
        assert item["correlation_key"] == "upgrade_api_removals:4.19"
        assert item["metadata"] == {"generator": "upgrade_api_removals", "urgency_hours": 72}
        assert item["due_date"] > 0
        assert f"{FLOWSCHEMA} (removed in Kubernetes 1.32): 130 requests/24h" in item["summary"]
        assert "alice (kubectl/v1.31.0): 20" in item["summary"]
        assert "[platform]" in item["summary"]
        assert item["resources"] == [{"kind": "APIRequestCount", "name": FLOWSCHEMA, "namespace": ""}]

    def test_unacked_gate_item(self, mock_k8s):
        from sre_agent.inbox_generators import gen_upgrade_api_removals

        _wire(mock_k8s, arcs=_arcs(in_use=False), acks={})
        [item] = gen_upgrade_api_removals()
        assert item["title"] == "Upgrade to 4.19 blocked: 1 admin gate not acknowledged"
        assert GATE_419 in item["summary"]

    def test_go_produces_nothing(self, mock_k8s):
        from sre_agent.inbox_generators import gen_upgrade_api_removals

        _wire(mock_k8s, arcs=_arcs(in_use=False))
        assert gen_upgrade_api_removals() == []

    def test_planning_window_creates_item(self, mock_k8s):
        from sre_agent.inbox_generators import gen_upgrade_api_removals

        _wire(mock_k8s, cv=_cv(updates=("4.18.5",)), acks={})
        [item] = gen_upgrade_api_removals()
        assert item["title"] == "Upgrade to 4.19 blocked: 1 removed API still in use"
        assert f"Admin gate not acknowledged: {GATE_419}" in item["summary"]

    def test_upgradeable_false_item(self, mock_k8s):
        from sre_agent.inbox_generators import gen_upgrade_api_removals

        cv = _upgradeable_false(_cv(), "ClusterOperatorsNotUpgradeable", "machine-config is not upgradeable")
        _wire(mock_k8s, cv=cv, arcs=_arcs(in_use=False))
        [item] = gen_upgrade_api_removals()
        assert (
            item["title"] == "Upgrade to 4.19 blocked: ClusterVersion not upgradeable (ClusterOperatorsNotUpgradeable)"
        )
        assert "machine-config is not upgradeable" in item["summary"]

    def test_not_openshift_produces_nothing(self, mock_k8s):
        from sre_agent.inbox_generators import gen_upgrade_api_removals

        _wire(mock_k8s, cv=ApiException(status=404, reason="Not Found"))
        assert gen_upgrade_api_removals() == []

    def test_verdict_is_cached_but_failures_are_not(self, mock_k8s):
        from sre_agent.inbox_generators import GeneratorUnavailableError, gen_upgrade_api_removals

        _wire(mock_k8s, arcs=ApiException(status=403, reason="Forbidden"))
        with pytest.raises(GeneratorUnavailableError):
            gen_upgrade_api_removals()
        _wire(mock_k8s)
        assert len(gen_upgrade_api_removals()) == 1
        assert len(gen_upgrade_api_removals()) == 1
        # Two calls after the failure, one listing: the second came from the cache.
        assert mock_k8s["custom"].list_cluster_custom_object.call_count == 2

    @pytest.mark.parametrize("status", [403, 404, 500])
    def test_unreadable_source_raises_unavailable(self, mock_k8s, status):
        from sre_agent.inbox_generators import GeneratorUnavailableError, gen_upgrade_api_removals

        _wire(mock_k8s, arcs=ApiException(status=status, reason="x"))
        with pytest.raises(GeneratorUnavailableError):
            gen_upgrade_api_removals()

    def test_registered(self):
        from sre_agent.inbox_generators import TASK_GENERATORS

        assert "upgrade_api_removals" in [n for n, _ in TASK_GENERATORS]


class TestInboxCycle:
    """Through run_generator_cycle and the inbox table (disposable test DB)."""

    def test_item_created_kept_on_failure_and_resolved_on_go(self, mock_k8s):
        from sre_agent import inbox_generators as g
        from sre_agent.db import get_database
        from sre_agent.inbox import get_inbox_item, run_generator_cycle

        db = get_database()
        db.execute("DELETE FROM inbox_items")
        db.commit()
        quiet = {
            "sweep_stale_items": lambda: 0,
            "expire_untouched_items": lambda: 0,
            "agent_process_inbox": lambda: None,
        }
        try:
            with (
                patch.object(g, "TASK_GENERATORS", [("upgrade_api_removals", g.gen_upgrade_api_removals)]),
                patch.multiple("sre_agent.inbox", **quiet),
            ):
                _wire(mock_k8s)
                run_generator_cycle()
                row = db.fetchone(
                    "SELECT id FROM inbox_items WHERE correlation_key = ?", ("upgrade_api_removals:4.19",)
                )
                assert row
                item = get_inbox_item(row["id"])
                assert item["title"] == "Upgrade to 4.19 blocked: 1 removed API still in use"

                g._reset_upgrade_api_cache()
                _wire(mock_k8s, arcs=ApiException(status=403, reason="Forbidden"))
                run_generator_cycle()
                assert get_inbox_item(row["id"])["status"] not in ("resolved", "archived")

                g._reset_upgrade_api_cache()
                _wire(mock_k8s, arcs=_arcs(in_use=False))
                run_generator_cycle()
                assert get_inbox_item(row["id"])["status"] == "resolved"
        finally:
            db.execute("DELETE FROM inbox_items")
            db.commit()
