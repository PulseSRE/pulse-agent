"""find_config_consumers / restart_consumers (JTBD E8, J9: config change + restart).

The matching cases are ported from pulse-ui's
``src/kubeview/engine/__tests__/configConsumers.test.ts`` so the agent and the
ConfigMap/Secret page's "Used by" section give the same answer.
"""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest

from sre_agent import k8s_client
from sre_agent.errors import ToolError
from sre_agent.k8s_client import user_token_context
from sre_agent.k8s_tools import config_consumers as cc


def _wl(kind: str, name: str, spec: dict, **meta) -> dict:
    if kind == "CronJob":
        body = {"jobTemplate": {"spec": {"template": {"spec": spec}}}}
    else:
        body = {"template": {"metadata": {"labels": {"app": name}}, "spec": spec}}
    return {"kind": kind, "metadata": {"name": name, "namespace": "payments", **meta}, "spec": body}


API = _wl(
    "Deployment",
    "api",
    {
        "containers": [
            {
                "name": "api",
                "env": [
                    {"name": "CACHE_SIZE", "valueFrom": {"configMapKeyRef": {"name": "api-cfg", "key": "CACHE_SIZE"}}},
                    {"name": "LOG_LEVEL", "valueFrom": {"configMapKeyRef": {"name": "api-cfg", "key": "LOG_LEVEL"}}},
                    {"name": "PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "password"}}},
                ],
            }
        ]
    },
)
WORKER = _wl(
    "Deployment", "worker", {"containers": [{"name": "w", "envFrom": [{"configMapRef": {"name": "api-cfg"}}]}]}
)
SYNC = _wl(
    "Deployment",
    "pricing-sync",
    {
        "containers": [{"name": "s", "volumeMounts": [{"name": "cfg", "mountPath": "/etc/api-cfg"}]}],
        "volumes": [{"name": "cfg", "configMap": {"name": "api-cfg"}}],
    },
)
SUBPATH = _wl(
    "StatefulSet",
    "db",
    {
        "containers": [
            {"name": "db", "volumeMounts": [{"name": "cfg", "mountPath": "/etc/my.cnf", "subPath": "my.cnf"}]}
        ],
        "volumes": [{"name": "cfg", "configMap": {"name": "api-cfg", "items": [{"key": "my.cnf", "path": "my.cnf"}]}}],
    },
)
PROJECTED = _wl(
    "DaemonSet",
    "agent",
    {
        "containers": [{"name": "a", "volumeMounts": [{"name": "all", "mountPath": "/etc/all"}]}],
        "volumes": [
            {"name": "all", "projected": {"sources": [{"configMap": {"name": "api-cfg"}}, {"secret": {"name": "db"}}]}}
        ],
        "imagePullSecrets": [{"name": "quay-pull"}],
    },
)
CRON = _wl("CronJob", "nightly", {"containers": [{"name": "c", "envFrom": [{"configMapRef": {"name": "api-cfg"}}]}]})
CRON_RUN = _wl(
    "Job",
    "nightly-123",
    {"containers": [{"name": "c", "envFrom": [{"configMapRef": {"name": "api-cfg"}}]}]},
    ownerReferences=[{"kind": "CronJob", "name": "nightly"}],
)
ONE_OFF = _wl("Job", "migrate", {"containers": [{"name": "m", "envFrom": [{"configMapRef": {"name": "api-cfg"}}]}]})
UNRELATED = _wl("Deployment", "web", {"containers": [{"name": "web"}]})


def _spec(w: dict) -> dict:
    return w["spec"]["template"]["spec"]


# ---------------------------------------------------------------------------
# Matching: same answers as the UI engine
# ---------------------------------------------------------------------------


def test_env_value_from_keys_and_vars():
    [u] = cc.usages_in(_spec(API), "ConfigMap", "api-cfg")
    assert u["how"] == "env" and u["container"] == "api"
    assert u["keys"] == ["CACHE_SIZE", "LOG_LEVEL"] and u["envVars"] == ["CACHE_SIZE", "LOG_LEVEL"]
    assert (
        cc.describe_usage(u, "ConfigMap")
        == "env CACHE_SIZE, LOG_LEVEL in api from keys CACHE_SIZE, LOG_LEVEL via configMapKeyRef"
    )


def test_configmap_reference_is_not_a_secret_reference():
    assert cc.usages_in(_spec(API), "Secret", "api-cfg") == []
    assert cc.usages_in(_spec(API), "Secret", "db")[0]["keys"] == ["password"]


def test_projected_volumes_and_image_pull_secrets():
    spec = _spec(PROJECTED)
    [u] = cc.usages_in(spec, "ConfigMap", "api-cfg")
    assert u["how"] == "projected"
    assert u["mounts"] == [{"container": "a", "mountPath": "/etc/all", "subPath": False}]
    assert cc.usages_in(spec, "Secret", "db")[0]["how"] == "projected"
    assert cc.usages_in(spec, "Secret", "quay-pull") == [{"how": "imagePullSecret", "keys": []}]
    assert cc.usages_in(spec, "ConfigMap", "quay-pull") == []


def test_pick_up_env_restart_volume_file_subpath_restart():
    found = cc.find_consumers("ConfigMap", "api-cfg", [API, WORKER, SYNC, SUBPATH, PROJECTED])
    assert {c["name"]: c["pickUp"] for c in found} == {
        "api": "restart",
        "worker": "restart",
        "pricing-sync": "file",
        "db": "restart",
        "agent": "file",
    }
    assert cc.pick_up_of("Deployment", [{"how": "imagePullSecret", "keys": []}]) == "next-pull"
    assert cc.pick_up_of("CronJob", [{"how": "envFrom", "keys": []}]) == "next-run"
    assert cc.pick_up_of("Job", [{"how": "envFrom", "keys": []}]) == "rerun"


def test_order_and_cronjob_stands_for_its_jobs():
    found = cc.find_consumers("ConfigMap", "api-cfg", [UNRELATED, WORKER, CRON_RUN, ONE_OFF, CRON, API, SYNC])
    assert [f"{c['kind']}/{c['name']}" for c in found] == [
        "Deployment/api",
        "Deployment/pricing-sync",
        "Deployment/worker",
        "CronJob/nightly",
        "Job/migrate",
    ]


def test_service_account_pull_secret_reaches_workloads_running_as_it():
    sas = [
        {"metadata": {"name": "default"}, "imagePullSecrets": [{"name": "quay-pull"}]},
        {"metadata": {"name": "builder"}, "imagePullSecrets": [{"name": "other"}]},
    ]
    as_default = _wl("Deployment", "web", {"containers": [{"name": "w"}]})
    as_builder = _wl("Deployment", "build", {"serviceAccountName": "builder", "containers": [{"name": "b"}]})
    found = cc.find_consumers("Secret", "quay-pull", [as_default, as_builder], sas)
    assert [c["name"] for c in found] == ["web"]
    assert found[0]["uses"] == [{"how": "serviceAccountPullSecret", "keys": [], "serviceAccount": "default"}]
    assert found[0]["pickUp"] == "next-pull"
    assert cc.describe_usage(found[0]["uses"][0], "Secret") == "image pull secret of its ServiceAccount default"


def test_volume_driver_credentials():
    spec = {
        "containers": [{"name": "c", "volumeMounts": [{"name": "vault", "mountPath": "/mnt/secrets"}]}],
        "volumes": [
            {
                "name": "vault",
                "csi": {"driver": "secrets-store.csi.k8s.io", "nodePublishSecretRef": {"name": "vault-creds"}},
            },
            {"name": "ceph", "cephfs": {"secretRef": {"name": "ceph-key"}}},
            {"name": "r", "rbd": {"secretRef": {"name": "ceph-key"}}},
            {"name": "az", "azureFile": {"secretName": "az-share", "shareName": "s"}},
        ],
    }
    w = _wl("Deployment", "app", spec)
    [csi] = cc.find_consumers("Secret", "vault-creds", [w])
    assert csi["uses"][0]["how"] == "volumeSecretRef" and csi["uses"][0]["driver"] == "csi secrets-store.csi.k8s.io"
    assert csi["pickUp"] == "mount"
    assert (
        cc.describe_usage(csi["uses"][0], "Secret")
        == "credentials of a csi secrets-store.csi.k8s.io volume mounted at /mnt/secrets"
    )
    assert [u["driver"] for u in cc.find_consumers("Secret", "ceph-key", [w])[0]["uses"]] == ["cephfs", "rbd"]
    assert len(cc.find_consumers("Secret", "az-share", [w])) == 1


def test_deploymentconfigs_scanned_like_deployments():
    dc = {
        "kind": "DeploymentConfig",
        "metadata": {"name": "legacy"},
        "spec": {
            "template": {"spec": {"containers": [{"name": "l", "envFrom": [{"configMapRef": {"name": "api-cfg"}}]}]}}
        },
    }
    [c] = cc.find_consumers("ConfigMap", "api-cfg", [dc])
    assert c["kind"] == "DeploymentConfig" and c["pickUp"] == "restart"


# ---------------------------------------------------------------------------
# Tools against a fake API server
# ---------------------------------------------------------------------------


def _call(tool, **args):
    out = tool.call(args)
    return out[0] if isinstance(out, tuple) else out


class FakeCluster:
    """Lists by path, records patches; a path mapped to a ToolError fails."""

    def __init__(self, workloads: list[dict], services: list[dict] | None = None, sas: list[dict] | None = None):
        workloads = copy.deepcopy(workloads)
        self.lists: dict[str, list[dict] | ToolError] = {}
        for kind, path in cc._LIST_PATHS.items():
            self.lists[path.format(ns="payments")] = [w for w in workloads if w["kind"] == kind]
        self.lists["/api/v1/namespaces/payments/services"] = services or []
        self.lists["/api/v1/namespaces/payments/serviceaccounts"] = sas or []
        self.paths: list[str] = []
        self.patches: list[tuple[str, str, dict, str | None]] = []
        self.refuse: dict[tuple[str, bool], str] = {}  # (name, dry_run) -> message

    def get_raw_json(self, path, operation="", **_):
        self.paths.append(path)
        got = self.lists.get(path, [])
        return got if isinstance(got, ToolError) else {"items": got}

    def _patcher(self, kind):
        def fn(name, namespace, body, **kwargs):
            dry = kwargs.get("dry_run")
            self.patches.append((f"{kind}/{name}", namespace, body, dry))
            msg = self.refuse.get((name, dry == "All"))
            if msg:
                from kubernetes.client.rest import ApiException

                raise ApiException(status=403, reason=msg)
            return MagicMock()

        return fn

    def apps(self):
        a = MagicMock()
        a.patch_namespaced_deployment.side_effect = self._patcher("Deployment")
        a.patch_namespaced_stateful_set.side_effect = self._patcher("StatefulSet")
        a.patch_namespaced_daemon_set.side_effect = self._patcher("DaemonSet")
        return a

    def real_patches(self):
        return [p for p in self.patches if p[3] is None]


@pytest.fixture
def cluster():
    web = _wl("Deployment", "api", _spec(API))
    fc = FakeCluster(
        [web, WORKER, SYNC, SUBPATH, PROJECTED, CRON, CRON_RUN, ONE_OFF, UNRELATED],
        services=[{"metadata": {"name": "api"}, "spec": {"selector": {"app": "api"}}}],
    )
    apps = fc.apps()
    with (
        patch.object(k8s_client, "get_raw_json", side_effect=fc.get_raw_json),
        patch.object(k8s_client, "get_apps_client", return_value=apps),
        patch("sre_agent.monitor.scanner_health.report_api_error"),
    ):
        yield fc


@pytest.fixture
def as_user():
    with user_token_context("alice-token"):
        yield


def test_find_lists_every_consumer_and_says_who_needs_a_restart(cluster):
    text = _call(cc.find_config_consumers, kind="ConfigMap", name="api-cfg", namespace="payments")
    for wl in ("Deployment api", "Deployment worker", "Deployment pricing-sync", "StatefulSet db", "DaemonSet agent"):
        assert wl in text
    assert "CronJob nightly: picks up changes at the next scheduled run" in text
    assert "nightly-123" not in text  # the CronJob stands for its Jobs
    assert "web" not in text
    assert "keep the old values until restarted: Deployment api, Deployment worker, StatefulSet db" in text
    assert "Not checked: bare Pods" in text


def test_find_names_kinds_it_could_not_read_and_tolerates_missing_deploymentconfigs(cluster):
    cluster.lists["/apis/apps.openshift.io/v1/namespaces/payments/deploymentconfigs"] = ToolError(
        message="not found", category="not_found", status_code=404
    )
    cluster.lists["/apis/batch/v1/namespaces/payments/jobs"] = ToolError(
        message="jobs is forbidden", category="forbidden", status_code=403
    )
    text = _call(cc.find_config_consumers, kind="ConfigMap", name="api-cfg", namespace="payments")
    assert "Could not check (the list may be incomplete): Jobs: jobs is forbidden" in text
    assert "DeploymentConfigs" not in text.split("Could not check")[1]


def test_find_never_reads_a_secret(cluster):
    cluster.lists["/api/v1/namespaces/payments/serviceaccounts"] = [
        {"metadata": {"name": "default"}, "imagePullSecrets": [{"name": "quay-pull"}]}
    ]
    text = _call(cc.find_config_consumers, kind="Secret", name="db", namespace="payments")
    assert "Deployment api" in text and "DaemonSet agent" in text
    assert not any("/secrets" in p for p in cluster.paths)
    assert "no Secret value was read" in text


def test_find_rejects_other_kinds_and_bad_names(cluster):
    assert "ConfigMap or Secret" in _call(cc.find_config_consumers, kind="Deployment", name="x", namespace="payments")
    assert "namespace is required" in _call(cc.find_config_consumers, kind="ConfigMap", name="x", namespace="")
    assert cluster.paths == []


def test_restart_dry_runs_then_restarts_only_the_ones_that_need_it_serving_last(cluster, as_user):
    text = _call(cc.restart_consumers, kind="ConfigMap", name="api-cfg", namespace="payments")
    dry = [p for p in cluster.patches if p[3] == "All"]
    real = cluster.real_patches()
    # Every patch dry-run first, then the same set for real.
    assert [p[0] for p in dry] == [p[0] for p in real]
    assert len(dry) == 3 and len(cluster.patches) == 6
    # api is selected by a Service: last. File-mounted, CronJob and Job consumers are not restarted.
    assert [p[0] for p in real] == ["Deployment/worker", "StatefulSet/db", "Deployment/api"]
    ann = real[0][2]["spec"]["template"]["metadata"]["annotations"]
    assert list(ann) == [cc.RESTART_ANNOTATION]
    assert {p[2]["spec"]["template"]["metadata"]["annotations"][cc.RESTART_ANNOTATION] for p in real} == set(
        ann.values()
    )
    assert text.startswith("Restarted 3 workload(s)")
    assert "not restarted (no restart needed): CronJob nightly" in text
    assert "not restarted (no restart needed): Deployment pricing-sync" in text


def test_restart_refuses_everything_when_a_dry_run_fails(cluster, as_user):
    cluster.refuse[("db", True)] = "statefulsets.apps is forbidden"
    text = _call(cc.restart_consumers, kind="ConfigMap", name="api-cfg", namespace="payments")
    assert cluster.real_patches() == []
    assert text.startswith("Nothing restarted: the server-side dry run refused")
    assert "StatefulSet db" in text


def test_restart_reports_partial_failure_and_stops(cluster, as_user):
    cluster.refuse[("db", False)] = "conflict"
    text = _call(cc.restart_consumers, kind="ConfigMap", name="api-cfg", namespace="payments")
    assert [p[0] for p in cluster.real_patches()] == ["Deployment/worker", "StatefulSet/db"]
    assert text.startswith("Partly done: 1 of 3 restarts started")
    assert "- restarted: Deployment worker" in text
    assert "- FAILED: StatefulSet db" in text
    assert "- not attempted: Deployment api" in text


def test_restart_skips_ondelete_and_paused(cluster, as_user):
    for w in cluster.lists["/apis/apps/v1/namespaces/payments/statefulsets"]:
        w["spec"]["updateStrategy"] = {"type": "OnDelete"}
    for w in cluster.lists["/apis/apps/v1/namespaces/payments/deployments"]:
        if w["metadata"]["name"] == "worker":
            w["spec"]["paused"] = True
    text = _call(cc.restart_consumers, kind="ConfigMap", name="api-cfg", namespace="payments")
    assert [p[0] for p in cluster.real_patches()] == ["Deployment/api"]
    assert "OnDelete" in text and "paused" in text


def test_restart_never_writes_as_the_service_account(cluster):
    # No forwarded token: refused before any read or write.
    text = _call(cc.restart_consumers, kind="ConfigMap", name="api-cfg", namespace="payments")
    assert text.startswith("Nothing restarted: Your access token did not reach Pulse")
    assert cluster.patches == [] and cluster.paths == []


def test_restart_refuses_when_token_forwarding_is_off(cluster, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_TOKEN_FORWARDING", "false")
    from sre_agent.config import _reset_settings

    _reset_settings()
    try:
        with patch.object(k8s_client, "get_current_user_token", return_value="tok"):
            text = _call(cc.restart_consumers, kind="ConfigMap", name="api-cfg", namespace="payments")
            preview = cc.preview_restart_consumers({"kind": "ConfigMap", "name": "api-cfg", "namespace": "payments"})
        assert "token forwarding is off" in text
        assert preview["valid"] is False and "token forwarding is off" in preview["error"]
        assert cluster.patches == []
    finally:
        _reset_settings()


def test_restart_with_nothing_to_restart(cluster, as_user):
    text = _call(cc.restart_consumers, kind="Secret", name="quay-pull", namespace="payments")
    assert text.startswith("Nothing restarted: no consumer of Secret quay-pull needs a restart")
    assert cluster.patches == []


def test_preview_lists_every_workload_and_the_dry_run(cluster, as_user):
    p = cc.preview_restart_consumers({"kind": "ConfigMap", "name": "api-cfg", "namespace": "payments"})
    assert p["kind"] == "restart_consumers" and p["valid"] is True
    assert [(r["kind"], r["name"]) for r in p["restart"]] == [
        ("Deployment", "worker"),
        ("StatefulSet", "db"),
        ("Deployment", "api"),
    ]
    assert p["restart"][2]["services"] == ["api"]
    assert {(r["kind"], r["name"]) for r in p["notRestarted"]} == {
        ("Deployment", "pricing-sync"),
        ("DaemonSet", "agent"),
        ("CronJob", "nightly"),
        ("Job", "migrate"),
    }
    assert p["dryRun"] == {"passed": True, "errors": []}
    assert cluster.real_patches() == []  # a preview never writes


def test_preview_is_invalid_when_the_dry_run_fails(cluster, as_user):
    cluster.refuse[("api", True)] = "admission webhook denied"
    p = cc.preview_restart_consumers({"kind": "ConfigMap", "name": "api-cfg", "namespace": "payments"})
    assert p["valid"] is False and "Deployment api" in p["error"]
    assert p["dryRun"]["passed"] is False


def test_preview_flags_gitops_managed_workloads(cluster, as_user):
    for w in cluster.lists["/apis/apps/v1/namespaces/payments/deployments"]:
        if w["metadata"]["name"] == "worker":
            w["metadata"]["annotations"] = {
                "argocd.argoproj.io/tracking-id": "payments-app:apps/Deployment:payments/worker"
            }
    p = cc.preview_restart_consumers({"kind": "ConfigMap", "name": "api-cfg", "namespace": "payments"})
    by = {r["name"]: r["gitops"] for r in p["restart"]}
    assert by == {"worker": "payments-app", "db": None, "api": None}
    text = _call(cc.restart_consumers, kind="ConfigMap", name="api-cfg", namespace="payments")
    assert "Managed by Argo CD (payments-app)" in text and "propose_git_change" in text


def test_helm_instance_label_is_not_argo():
    w = {"metadata": {"labels": {"app.kubernetes.io/instance": "r", "app.kubernetes.io/managed-by": "Helm"}}}
    assert cc.gitops_owner(w) is None


def test_restart_consumers_is_a_confirm_gated_write_with_a_preview():
    from sre_agent.confirm_preview import has_preview
    from sre_agent.k8s_tools import WRITE_TOOLS
    from sre_agent.tool_registry import WRITE_TOOL_NAMES

    assert "restart_consumers" in WRITE_TOOLS and "restart_consumers" in WRITE_TOOL_NAMES
    assert "find_config_consumers" not in WRITE_TOOL_NAMES
    assert has_preview("restart_consumers")


# ---------------------------------------------------------------------------
# Reach: the sre skill offers both for the obvious questions
# ---------------------------------------------------------------------------

CONSUMER_QUERY = "I changed the api-cfg ConfigMap in payments, restart everything that uses it"


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name

    def to_dict(self) -> dict:
        return {"name": self.name}


@pytest.mark.parametrize("tool", ["find_config_consumers", "restart_consumers"])
def test_sre_mode_keeps_the_tools_under_the_budget_cap(tool):
    from sre_agent.agent import TOOL_MAP
    from sre_agent.skill_loader import MAX_TOOL_BUDGET, select_tools

    mcp = [{"name": f"mcp_extra_tool_{i}"} for i in range(40)]
    all_tools = list(TOOL_MAP.values()) + [_FakeTool(t["name"]) for t in mcp]
    tool_map = {t.name: t for t in all_tools}
    with patch("sre_agent.mcp_client.list_mcp_tools", return_value=mcp):
        _, _, offered = select_tools(CONSUMER_QUERY, all_tools, tool_map, "sre")
    assert len(offered) == MAX_TOOL_BUDGET
    assert tool in offered


def test_sre_skill_offers_both():
    from sre_agent.skill_loader import build_config_from_skill, get_skill, load_skills
    from sre_agent.tool_predictor import PredictionResult

    load_skills()
    skill = get_skill("sre")
    assert skill is not None
    with (
        patch("sre_agent.tool_predictor.predict_tools", return_value=PredictionResult(tools=[], confidence="low")),
        patch("sre_agent.tool_predictor.llm_pick_tools", return_value=[]),
    ):
        config = build_config_from_skill(skill, query=CONSUMER_QUERY)
    assert {"find_config_consumers", "restart_consumers"} <= set(config["tool_map"])
    assert "restart_consumers" in config["write_tools"]
