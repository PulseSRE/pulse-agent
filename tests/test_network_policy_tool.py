"""create_network_policy keeps the OpenShift router and DNS working by default.

A bare deny-all-ingress blocks the router (every Route in the namespace breaks)
and a bare deny-all-egress blocks DNS. The tool mirrors pulse-ui's reviewed
templates (`engine/createTemplates.ts`): a router allow policy beside the
ingress deny, and a DNS rule in the egress deny. `strict=True` is the bare deny.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from sre_agent.k8s_tools.advanced import (
    ROUTER_ALLOW_POLICY_NAME,
    create_network_policy,
    network_policy_manifests,
)

ROUTER_FROM = [
    {"namespaceSelector": {"matchLabels": {"policy-group.network.openshift.io/ingress": ""}}},
    {"namespaceSelector": {"matchLabels": {"policy-group.network.openshift.io/host-network": ""}}},
]
DNS_RULE = {
    "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}}}],
    "ports": [
        {"protocol": "UDP", "port": 5353},
        {"protocol": "TCP", "port": 5353},
        {"protocol": "UDP", "port": 53},
        {"protocol": "TCP", "port": 53},
    ],
}


def _created(networking: MagicMock) -> list[dict]:
    """Bodies of the real (non-dry-run) creates, in call order."""
    return [c.args[1] for c in networking.create_namespaced_network_policy.call_args_list if "dry_run" not in c.kwargs]


def _dry_runs(networking: MagicMock) -> list[dict]:
    return [c.args[1] for c in networking.create_namespaced_network_policy.call_args_list if "dry_run" in c.kwargs]


def _conflict() -> ApiException:
    e = ApiException(status=409, reason="Conflict")
    e.body = '{"message": "networkpolicies \\"x\\" already exists"}'
    return e


# ── Manifests ──────────────────────────────────────────────────────────────


def test_default_deny_ingress_lets_the_router_in_first():
    policies = network_policy_manifests("shop", "default-deny-ingress", "deny-all-ingress")
    assert [p["metadata"]["name"] for p in policies] == [ROUTER_ALLOW_POLICY_NAME, "default-deny-ingress"]
    allow, deny = policies
    assert allow["metadata"]["namespace"] == "shop"
    assert allow["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": ROUTER_FROM}]}
    assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}


def test_default_deny_egress_allows_dns_only():
    (deny,) = network_policy_manifests("shop", "default-deny-egress-allow-dns", "deny-all-egress")
    assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Egress"], "egress": [DNS_RULE]}


def test_deny_all_keeps_router_and_dns():
    allow, deny = network_policy_manifests("shop", "default-deny-all-allow-dns", "deny-all")
    assert allow["metadata"]["name"] == ROUTER_ALLOW_POLICY_NAME
    assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "egress": [DNS_RULE]}


def test_strict_variants_are_the_bare_deny():
    assert network_policy_manifests("shop", "d", "deny-all-ingress", strict=True) == [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "d", "namespace": "shop"},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"]},
        }
    ]
    (egress,) = network_policy_manifests("shop", "d", "deny-all-egress", strict=True)
    assert egress["spec"] == {"podSelector": {}, "policyTypes": ["Egress"]}
    (both,) = network_policy_manifests("shop", "d", "deny-all", strict=True)
    assert both["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}


# ── Tool ───────────────────────────────────────────────────────────────────


def test_tool_default_creates_router_allow_then_deny(mock_k8s):
    net = mock_k8s["networking"]
    result = create_network_policy.call({"namespace": "shop"})
    created = _created(net)
    assert [b["metadata"]["name"] for b in created] == [ROUTER_ALLOW_POLICY_NAME, "default-deny-ingress"]
    # Both were dry-run before either was created.
    assert len(_dry_runs(net)) == 2
    calls = net.create_namespaced_network_policy.call_args_list
    assert all("dry_run" in c.kwargs for c in calls[:2])
    assert "Routes keep working" in str(result)
    assert "Egress is not restricted" in str(result)


def test_tool_egress_allows_dns_and_says_so(mock_k8s):
    result = create_network_policy.call({"namespace": "shop", "policy_type": "deny-all-egress"})
    (body,) = _created(mock_k8s["networking"])
    assert body["metadata"]["name"] == "default-deny-egress-allow-dns"
    assert body["spec"]["egress"] == [DNS_RULE]
    assert "DNS to openshift-dns" in str(result)


def test_tool_deny_all_creates_router_allow_and_dns(mock_k8s):
    create_network_policy.call({"namespace": "shop", "policy_type": "deny-all"})
    allow, deny = _created(mock_k8s["networking"])
    assert allow["metadata"]["name"] == ROUTER_ALLOW_POLICY_NAME
    assert deny["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert deny["spec"]["egress"] == [DNS_RULE]


def test_tool_strict_creates_only_the_bare_deny_and_warns(mock_k8s):
    result = create_network_policy.call({"namespace": "shop", "policy_type": "deny-all", "strict": True})
    (body,) = _created(mock_k8s["networking"])
    assert body["metadata"]["name"] == "default-deny-all"
    assert "egress" not in body["spec"]
    assert "Route in the namespace stops working" in str(result)
    assert "no name lookups resolve" in str(result)


def test_tool_keeps_an_existing_router_allow_policy(mock_k8s):
    net = mock_k8s["networking"]

    def create(ns, body, **kw):
        if body["metadata"]["name"] == ROUTER_ALLOW_POLICY_NAME:
            raise _conflict()
        return MagicMock()

    net.create_namespaced_network_policy.side_effect = create
    result = create_network_policy.call({"namespace": "shop"})
    assert [b["metadata"]["name"] for b in _created(net)] == ["default-deny-ingress"]
    assert "already exists" in str(result) and "left unchanged" in str(result)


def test_tool_creates_nothing_when_the_deny_dry_run_fails(mock_k8s):
    net = mock_k8s["networking"]

    def create(ns, body, **kw):
        if body["metadata"]["name"] == "default-deny-ingress":
            raise ApiException(status=403, reason="Forbidden")
        return MagicMock()

    net.create_namespaced_network_policy.side_effect = create
    result = create_network_policy.call({"namespace": "shop"})
    assert _created(net) == []
    assert "Nothing was created" in str(result)


def test_tool_never_leaves_a_deny_without_the_router_allow(mock_k8s):
    """The allow goes first: if it fails for real, the deny is not attempted."""
    net = mock_k8s["networking"]

    def create(ns, body, **kw):
        if body["metadata"]["name"] == ROUTER_ALLOW_POLICY_NAME and "dry_run" not in kw:
            raise ApiException(status=500, reason="Internal Server Error")
        return MagicMock()

    net.create_namespaced_network_policy.side_effect = create
    result = create_network_policy.call({"namespace": "shop"})
    names = [
        c.args[1]["metadata"]["name"]
        for c in net.create_namespaced_network_policy.call_args_list
        if "dry_run" not in c.kwargs
    ]
    assert names == [ROUTER_ALLOW_POLICY_NAME]
    assert "Nothing was created" in str(result)


def test_tool_reports_a_partial_create(mock_k8s):
    net = mock_k8s["networking"]

    def create(ns, body, **kw):
        if body["metadata"]["name"] == "default-deny-ingress" and "dry_run" not in kw:
            raise ApiException(status=500, reason="Internal Server Error")
        return MagicMock()

    net.create_namespaced_network_policy.side_effect = create
    result = str(create_network_policy.call({"namespace": "shop"}))
    assert "failed" in result and f"Already created: {ROUTER_ALLOW_POLICY_NAME}" in result


def test_tool_rejects_bad_input(mock_k8s):
    net = mock_k8s["networking"]
    assert "Unknown policy type" in str(create_network_policy.call({"namespace": "shop", "policy_type": "allow-all"}))
    assert "Error" in str(create_network_policy.call({"namespace": "Bad_NS"}))
    assert "router allow policy" in str(
        create_network_policy.call({"namespace": "shop", "name": ROUTER_ALLOW_POLICY_NAME})
    )
    net.create_namespaced_network_policy.assert_not_called()


def test_rbac_history_names_the_default_policy():
    from sre_agent.rbac_history import agent_write_target

    assert agent_write_target("create_network_policy", {"namespace": "shop"}) == (
        "NetworkPolicy",
        "shop",
        "default-deny-ingress",
    )
    assert agent_write_target("create_network_policy", {"namespace": "shop", "policy_type": "deny-all-egress"})[2] == (
        "default-deny-egress-allow-dns"
    )
