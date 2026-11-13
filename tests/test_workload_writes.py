"""patch_workload_container and the StatefulSet/DaemonSet verbs (JTBD F2/F3/F5/F6, E2/E5/E7, L3).

A fake API server holds the objects: reads go through ``get_raw_json`` and
writes through ``send_patch``, which applies JSON Patch (including ``test``
ops, so a stale resourceVersion is refused like the real server refuses it).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from sre_agent import k8s_client
from sre_agent.errors import ToolError
from sre_agent.k8s_tools import workload_writes as ww

NS = "payments"


def _deployment(**over: Any) -> dict:
    obj = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "api",
            "namespace": NS,
            "uid": "dep-uid",
            "resourceVersion": "100",
            "annotations": {"deployment.kubernetes.io/revision": "4"},
            "labels": {"app": "api"},
        },
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "api"}},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": "25%"}},
            "template": {
                "metadata": {"labels": {"app": "api"}},
                "spec": {
                    "serviceAccountName": "api-sa",
                    "containers": [
                        {
                            "name": "api",
                            "image": "quay.io/acme/api:v4",
                            "resources": {"limits": {"memory": "512Mi"}, "requests": {"memory": "256Mi"}},
                            "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 10},
                        }
                    ],
                },
            },
        },
    }
    for k, v in over.items():
        obj[k] = v
    return obj


def _statefulset() -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": NS, "uid": "sts-uid", "resourceVersion": "7"},
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "db"}},
            "updateStrategy": {"type": "RollingUpdate"},
            "template": {
                "metadata": {"labels": {"app": "db"}},
                "spec": {"containers": [{"name": "postgres", "image": "postgres:16.2"}]},
            },
        },
        "status": {"updateRevision": "db-bbb", "currentRevision": "db-bbb"},
    }


def _cr(name: str, owner_kind: str, uid: str, revision: int, image: str, app: str = "db") -> dict:
    return {
        "metadata": {
            "name": name,
            "namespace": NS,
            "ownerReferences": [{"kind": owner_kind, "name": "x", "uid": uid}],
        },
        "revision": revision,
        "data": {
            "spec": {
                "template": {
                    "$patch": "replace",
                    "metadata": {"labels": {"app": app, "controller-revision-hash": name}},
                    "spec": {"containers": [{"name": "postgres" if app == "db" else "agent", "image": image}]},
                }
            }
        },
    }


class FakeAPI:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.lists: dict[str, dict] = {}
        self.secrets: set[str] = set()
        self.reads: list[tuple[str, str]] = []
        self.patches: list[dict] = []
        self.fail_dry_run: ToolError | None = None
        self.bump_after_dry_run = False

    def put(self, obj: dict) -> None:
        self.objects[ww.workload_path(obj["kind"], obj["metadata"]["namespace"], obj["metadata"]["name"])] = obj

    def get_raw_json(self, path: str, operation: str = "", *, query=None, accept: str = ""):
        self.reads.append((path, accept))
        if "/secrets/" in path:
            assert "PartialObjectMetadata" in accept, "a Secret is only ever read as metadata"
            name = path.rsplit("/", 1)[1]
            if name in self.secrets:
                return {"kind": "PartialObjectMetadata", "metadata": {"name": name}}
            return ToolError(message=f'secrets "{name}" not found', category="not_found", status_code=404)
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        if path in self.lists:
            return copy.deepcopy(self.lists[path])
        return ToolError(message="not found", category="not_found", status_code=404)

    def send_patch(self, path: str, body: Any, content_type: str, *, dry_run: bool, operation: str):
        self.patches.append({"path": path, "body": copy.deepcopy(body), "type": content_type, "dry_run": dry_run})
        if dry_run and self.fail_dry_run:
            return self.fail_dry_run
        scale = path.endswith("/scale")
        key = path.removesuffix("/scale")
        obj = self.objects[key]
        if content_type == ww.JSON_PATCH:
            try:
                new = ww.apply_json_patch(obj, body)
            except ww.WorkloadWriteError as e:
                return ToolError(
                    message=f"the server rejected our request: {e} (test operation)", category="validation"
                )
        elif content_type == ww.MERGE_PATCH:
            rv = (body.get("metadata") or {}).get("resourceVersion")
            if rv and rv != obj["metadata"]["resourceVersion"]:
                return ToolError(message="the object has been modified", category="conflict", status_code=409)
            new = copy.deepcopy(obj)
            new["spec"]["replicas"] = body["spec"]["replicas"]
        else:
            new = copy.deepcopy(obj)
            ann = new["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})
            ann.update(body["spec"]["template"]["metadata"]["annotations"])
        if dry_run:
            if self.bump_after_dry_run:
                obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
            return new if not scale else {"spec": {"replicas": new["spec"]["replicas"]}}
        new["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
        self.objects[key] = new
        return new if not scale else {"spec": {"replicas": new["spec"]["replicas"]}}

    @property
    def writes(self) -> list[dict]:
        return [p for p in self.patches if not p["dry_run"]]


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    fake.put(_deployment())
    monkeypatch.setattr(k8s_client, "get_raw_json", fake.get_raw_json)
    monkeypatch.setattr(k8s_client, "get_current_user_token", lambda: "user-token")
    monkeypatch.setattr(ww, "send_patch", fake.send_patch)
    monkeypatch.setattr("sre_agent.gitops_tools.check_argo_auto_sync", lambda ns, kind="", name="": None)
    return fake


def _patch(**kw):
    args = {"kind": "Deployment", "namespace": NS, "name": "api"} | kw
    return ww.patch_workload_container.func(**args)


def _container(fake: FakeAPI, kind="Deployment", name="api", i=0) -> dict:
    return fake.objects[ww.workload_path(kind, NS, name)]["spec"]["template"]["spec"]["containers"][i]


# ---------------------------------------------------------------------------
# patch_workload_container: the same patch as the UI's guided fixes
# ---------------------------------------------------------------------------


def test_memory_limit_is_the_ui_memory_fix_patch(api):
    out = _patch(memory_limit="768Mi", reason="OOMKilled")
    assert "Applied" in out
    assert "limits.memory: 512Mi -> 768Mi" in out
    write = api.writes[0]
    assert write["type"] == ww.JSON_PATCH
    # podFixes.ts memoryFix: rvTest, container name test, add limits/memory; then withChangeCause.
    assert write["body"][:3] == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "100"},
        {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": "api"},
        {"op": "add", "path": "/spec/template/spec/containers/0/resources/limits/memory", "value": "768Mi"},
    ]
    assert write["body"][3]["path"] == "/metadata/annotations/kubernetes.io~1change-cause"
    assert "OOMKilled" in write["body"][3]["value"]
    assert len(write["body"]) == 4
    assert _container(api)["resources"]["limits"]["memory"] == "768Mi"
    assert _container(api)["resources"]["requests"]["memory"] == "256Mi"


def test_dry_run_is_sent_first_with_the_same_body(api):
    _patch(cpu_limit="500m")
    assert [p["dry_run"] for p in api.patches] == [True, False]
    assert api.patches[0]["body"] == api.writes[0]["body"]


def test_resources_created_with_parents_when_absent(api):
    dep = _deployment()
    del dep["spec"]["template"]["spec"]["containers"][0]["resources"]
    api.put(dep)
    _patch(memory_limit="1Gi", memory_request="512Mi")
    body = api.writes[0]["body"]
    assert {
        "op": "add",
        "path": "/spec/template/spec/containers/0/resources",
        "value": {"limits": {"memory": "1Gi"}},
    } in body
    assert {
        "op": "add",
        "path": "/spec/template/spec/containers/0/resources/requests",
        "value": {"memory": "512Mi"},
    } in body
    assert _container(api)["resources"] == {"limits": {"memory": "1Gi"}, "requests": {"memory": "512Mi"}}


def test_dry_run_failure_refuses_and_writes_nothing(api):
    api.fail_dry_run = ToolError(message="exceeded quota: compute-resources", category="quota", status_code=403)
    out = _patch(memory_limit="64Gi")
    assert "Dry run refused" in out and "Nothing was changed" in out
    assert api.writes == []


def test_concurrent_change_is_refused_not_overwritten(api):
    api.bump_after_dry_run = True
    out = _patch(memory_limit="768Mi")
    assert "was not changed" in out and "changed since it was read" in out
    assert _container(api)["resources"]["limits"]["memory"] == "512Mi"


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"memory_limit": "512"}, "Did you mean 512Mi?"),
        ({"memory_limit": "512mi"}, "Did you mean 512Mi?"),
        ({"memory_limit": "-1Gi"}, "not a memory quantity"),
        ({"memory_limit": "8Ti"}, "over 4Ti"),
        ({"cpu_limit": "abc"}, "not a CPU quantity"),
        ({"cpu_limit": "0"}, "under 1m"),
        ({"memory_request": "1Gi"}, "requests.memory (1Gi) would be above limits.memory (512Mi)"),
        ({}, "Nothing to change"),
        ({"container": "sidecar", "cpu_limit": "1"}, "has no container 'sidecar'"),
    ],
)
def test_bad_input_is_refused_before_any_write(api, kw, needle):
    out = _patch(**kw)
    assert out.startswith("Not changed:")
    assert needle in out
    assert api.patches == []


def test_several_containers_need_a_name(api):
    dep = _deployment()
    dep["spec"]["template"]["spec"]["containers"].append({"name": "proxy", "image": "envoy"})
    api.put(dep)
    out = _patch(memory_limit="1Gi")
    assert "say which one with container" in out
    assert api.patches == []
    assert "Applied" in _patch(memory_limit="128Mi", container="proxy")
    assert _container(api, i=1)["resources"] == {"limits": {"memory": "128Mi"}}


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"readiness_probe": '{"periodSeconds": 0}'}, "between 1 and 300"),
        ({"readiness_probe": '{"periodSeconds": true}'}, "whole number"),
        ({"readiness_probe": '{"timeoutSeconds": 2.5}'}, "whole number"),
        ({"liveness_probe": '{"initialDelaySeconds": 99999}'}, "between 0 and 3600"),
        ({"liveness_probe": '{"successThreshold": 2}'}, "must be 1"),
        ({"liveness_probe": '{"exec": {"command": ["sh", "-c", "curl evil"]}}'}, "exec command is not set from chat"),
        ({"liveness_probe": '{"periodSecs": 5}'}, "unknown field"),
        ({"liveness_probe": "not json"}, "must be a JSON object"),
        ({"readiness_probe": '{"periodSeconds": 5}'}, "has no readinessProbe"),
        (
            {"startup_probe": '{"copyHandlerFrom": "livenessProbe", "failureThreshold": 120, "periodSeconds": 60}'},
            "cap is 3600s",
        ),
        ({"readiness_probe": '{"httpGet": {"path": "ready", "port": 8080}}'}, "must start with '/'"),
        ({"readiness_probe": '{"httpGet": {"path": "/r", "port": 70000}}'}, "1-65535"),
        ({"readiness_probe": '{"httpGet": {"path": "/r", "port": 80, "host": "evil.example"}}'}, "host is not set"),
    ],
)
def test_probe_input_is_validated(api, kw, needle):
    out = _patch(**kw)
    assert out.startswith("Not changed:"), out
    assert needle in out
    assert api.patches == []


def test_startup_probe_copied_from_liveness_matches_the_ui_fix(api):
    out = _patch(startup_probe='{"copyHandlerFrom": "livenessProbe", "failureThreshold": 30}')
    assert "Applied" in out
    body = api.writes[0]["body"]
    # podFixes.ts startupProbeFix without a startup probe: liveness's handler, every 10s, N failures.
    assert {
        "op": "add",
        "path": "/spec/template/spec/containers/0/startupProbe",
        "value": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 10, "failureThreshold": 30},
    } in body
    assert _container(api)["startupProbe"]["failureThreshold"] == 30


def test_existing_probe_timing_is_one_add_per_field(api):
    _patch(liveness_probe='{"failureThreshold": 6, "timeoutSeconds": 5}')
    body = api.writes[0]["body"]
    assert {"op": "add", "path": "/spec/template/spec/containers/0/livenessProbe/failureThreshold", "value": 6} in body
    assert {"op": "add", "path": "/spec/template/spec/containers/0/livenessProbe/timeoutSeconds", "value": 5} in body
    probe = _container(api)["livenessProbe"]
    assert probe == {
        "httpGet": {"path": "/healthz", "port": 8080},
        "periodSeconds": 10,
        "failureThreshold": 6,
        "timeoutSeconds": 5,
    }


def test_probe_handler_swap_removes_the_old_handler(api):
    _patch(liveness_probe='{"tcpSocket": {"port": 8080}}')
    probe = _container(api)["livenessProbe"]
    assert "httpGet" not in probe and probe["tcpSocket"] == {"port": 8080}


def test_pull_secret_must_exist_and_is_checked_as_metadata_only(api):
    out = _patch(add_image_pull_secrets="quay-creds")
    assert "does not exist in namespace payments" in out
    assert api.patches == []
    secret_reads = [r for r in api.reads if "/secrets/" in r[0]]
    assert secret_reads and all("PartialObjectMetadata" in accept for _, accept in secret_reads)


def test_pull_secret_appended_when_template_lists_some(api):
    dep = _deployment()
    dep["spec"]["template"]["spec"]["imagePullSecrets"] = [{"name": "old"}]
    api.put(dep)
    api.secrets.add("quay-creds")
    _patch(add_image_pull_secrets="quay-creds")
    assert {
        "op": "add",
        "path": "/spec/template/spec/imagePullSecrets/-",
        "value": {"name": "quay-creds"},
    } in api.writes[0]["body"]
    tpl = api.objects[ww.workload_path("Deployment", NS, "api")]["spec"]["template"]["spec"]
    assert tpl["imagePullSecrets"] == [{"name": "old"}, {"name": "quay-creds"}]


def test_first_template_pull_secret_keeps_the_service_accounts(api):
    api.secrets.add("quay-creds")
    api.objects[f"/api/v1/namespaces/{NS}/serviceaccounts/api-sa"] = {"imagePullSecrets": [{"name": "sa-dockercfg"}]}
    plan = ww.plan_patch_workload_container(
        {"kind": "Deployment", "namespace": NS, "name": "api", "add_image_pull_secrets": "quay-creds"}
    )
    assert plan.valid
    assert any("ServiceAccount api-sa's pull secrets" in w for w in plan.warnings)
    out = _patch(add_image_pull_secrets="quay-creds")
    assert "Applied" in out
    tpl = api.objects[ww.workload_path("Deployment", NS, "api")]["spec"]["template"]["spec"]
    assert tpl["imagePullSecrets"] == [{"name": "sa-dockercfg"}, {"name": "quay-creds"}]


def test_refuses_without_token_forwarding(api, monkeypatch):
    from sre_agent.config import _reset_settings

    monkeypatch.setenv("PULSE_AGENT_TOKEN_FORWARDING", "false")
    _reset_settings()
    try:
        out = _patch(memory_limit="768Mi")
        scaled = ww.scale_workload.func("Deployment", NS, "api", 2)
    finally:
        monkeypatch.delenv("PULSE_AGENT_TOKEN_FORWARDING")
        _reset_settings()
    assert "token forwarding is off" in out and "token forwarding is off" in scaled
    assert api.patches == []


def test_refuses_without_the_callers_token(api, monkeypatch):
    monkeypatch.setattr(k8s_client, "get_current_user_token", lambda: None)
    out = _patch(memory_limit="768Mi")
    assert "never changed as Pulse's service account" in out
    assert api.patches == []
    out = ww.restart_workload.func("StatefulSet", NS, "db")
    assert "never changed as Pulse's service account" in out


def test_gitops_managed_object_is_called_out(api):
    dep = _deployment()
    dep["metadata"]["annotations"]["argocd.argoproj.io/tracking-id"] = "payments-app:apps/Deployment:payments/api"
    api.put(dep)
    plan = ww.plan_patch_workload_container(
        {"kind": "Deployment", "namespace": NS, "name": "api", "memory_limit": "1Gi"}
    )
    preview = plan.to_preview()
    assert preview["gitops"] == {
        "tool": "Argo CD",
        "app": "payments-app",
        "detail": "annotation argocd.argoproj.io/tracking-id",
    }
    out = _patch(memory_limit="1Gi")
    assert "propose_git_change" in out


def test_helm_instance_label_is_not_mistaken_for_argo():
    owner = ww.gitops_owner(
        {"metadata": {"labels": {"app.kubernetes.io/managed-by": "Helm", "app.kubernetes.io/instance": "pay"}}}
    )
    assert owner is not None and owner["tool"] == "Helm"


def test_argo_auto_sync_refuses(api, monkeypatch):
    monkeypatch.setattr(
        "sre_agent.gitops_tools.check_argo_auto_sync",
        lambda ns, kind="", name="": "WARNING: ... Use propose_git_change to make a permanent change via PR instead.",
    )
    out = _patch(memory_limit="1Gi")
    assert out.startswith("Not changed:") and "propose_git_change" in out
    assert api.patches == []


def test_confirm_preview_shows_the_diff(api, monkeypatch):
    from sre_agent import confirm_preview

    assert confirm_preview.has_preview("patch_workload_container")
    preview = confirm_preview.build_preview(
        "patch_workload_container", {"kind": "deployment", "namespace": NS, "name": "api", "memory_limit": "768Mi"}
    )
    assert preview is not None
    assert preview["kind"] == "workload_change"
    assert preview["valid"] is True
    assert preview["target"] == {"kind": "Deployment", "namespace": NS, "name": "api", "container": "api"}
    assert {"field": "resources.limits.memory", "before": "512Mi", "after": "768Mi"} in preview["diff"]
    assert preview["dryRun"]["state"] == "passed"
    assert preview["patch"]["type"] == ww.JSON_PATCH
    assert "Rolling restart of 3 pods" in preview["impact"]
    assert "revision 4" in preview["undo"]
    assert api.writes == []  # a preview never writes


def test_preview_of_invalid_input_says_why(api):
    from sre_agent import confirm_preview

    preview = confirm_preview.build_preview(
        "patch_workload_container", {"kind": "Deployment", "namespace": NS, "name": "api", "memory_limit": "512"}
    )
    assert preview is not None and preview["valid"] is False
    assert "Did you mean 512Mi" in preview["error"]
    assert preview["dryRun"]["state"] == "unavailable"


# ---------------------------------------------------------------------------
# scale / restart / rollback for StatefulSet and DaemonSet
# ---------------------------------------------------------------------------


def test_scale_statefulset_uses_the_scale_subresource_with_rv(api):
    api.put(_statefulset())
    out = ww.scale_workload.func("StatefulSet", NS, "db", 5)
    assert "from 3 to 5" in out
    w = api.writes[0]
    assert w["path"].endswith("/statefulsets/db/scale")
    assert w["body"] == {"metadata": {"resourceVersion": "7"}, "spec": {"replicas": 5}}
    assert api.objects[ww.workload_path("StatefulSet", NS, "db")]["spec"]["replicas"] == 5


def test_scale_daemonset_is_refused(api):
    out = ww.scale_workload.func("DaemonSet", NS, "agent", 2)
    assert "has no replica count" in out
    assert api.patches == []


@pytest.mark.parametrize("replicas", [-1, 101])
def test_scale_bounds(api, replicas):
    api.put(_statefulset())
    assert "between 0 and 100" in ww.scale_workload.func("StatefulSet", NS, "db", replicas)
    assert api.patches == []


def test_scale_warns_about_an_hpa(api):
    api.put(_statefulset())
    api.lists[f"/apis/autoscaling/v2/namespaces/{NS}/horizontalpodautoscalers"] = {
        "items": [
            {
                "metadata": {"name": "db-hpa"},
                "spec": {"scaleTargetRef": {"kind": "StatefulSet", "name": "db"}, "minReplicas": 2, "maxReplicas": 6},
            }
        ]
    }
    plan = ww.plan_scale_workload({"kind": "StatefulSet", "namespace": NS, "name": "db", "replicas": 5})
    assert any("db-hpa" in w for w in plan.warnings)


def test_restart_statefulset(api):
    api.put(_statefulset())
    out = ww.restart_workload.func("statefulset", NS, "db")
    assert "Rolling restart of StatefulSet db triggered" in out
    assert "one at a time from the highest ordinal" in out
    w = api.writes[0]
    assert w["type"] == ww.STRATEGIC_PATCH
    ann = w["body"]["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in ann


def test_restart_on_delete_warns(api):
    sts = _statefulset()
    sts["spec"]["updateStrategy"] = {"type": "OnDelete"}
    api.put(sts)
    plan = ww.plan_restart_workload({"kind": "StatefulSet", "namespace": NS, "name": "db"})
    assert any("OnDelete" in w for w in plan.warnings)


def test_restart_paused_deployment_refused(api):
    dep = _deployment()
    dep["spec"]["paused"] = True
    api.put(dep)
    assert "is paused" in ww.restart_workload.func("Deployment", NS, "api")
    assert api.patches == []


def test_rollback_statefulset_restores_the_previous_controller_revision(api):
    api.put(_statefulset())
    api.lists[f"/apis/apps/v1/namespaces/{NS}/controllerrevisions"] = {
        "items": [
            _cr("db-aaa", "StatefulSet", "sts-uid", 1, "postgres:16.1"),
            _cr("db-bbb", "StatefulSet", "sts-uid", 2, "postgres:16.2"),
            _cr("other-zzz", "StatefulSet", "other-uid", 9, "postgres:9"),
        ]
    }
    plan = ww.plan_rollback_workload({"kind": "StatefulSet", "namespace": NS, "name": "db"})
    assert plan.valid, plan.error
    assert plan.diff[0]["field"].endswith(".image")
    assert plan.diff[0]["before"] == "postgres:16.2" and plan.diff[0]["after"] == "postgres:16.1"
    out = ww.rollback_workload.func("StatefulSet", NS, "db", 0)
    assert "revision 1" in out
    body = api.writes[0]["body"]
    assert body[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "7"}
    replaced = body[1]
    assert replaced["op"] == "replace" and replaced["path"] == "/spec/template"
    assert "$patch" not in replaced["value"]
    assert "controller-revision-hash" not in replaced["value"]["metadata"]["labels"]
    assert replaced["value"]["spec"]["containers"][0]["image"] == "postgres:16.1"


def test_rollback_daemonset_current_is_the_highest_revision(api):
    ds = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": "agent", "namespace": NS, "uid": "ds-uid", "resourceVersion": "3"},
        "spec": {
            "selector": {"matchLabels": {"app": "agent"}},
            "template": {
                "metadata": {"labels": {"app": "agent"}},
                "spec": {"containers": [{"name": "agent", "image": "a:3"}]},
            },
        },
        "status": {"desiredNumberScheduled": 4},
    }
    api.put(ds)
    api.lists[f"/apis/apps/v1/namespaces/{NS}/controllerrevisions"] = {
        "items": [
            _cr("agent-1", "DaemonSet", "ds-uid", 1, "a:1", app="agent"),
            _cr("agent-2", "DaemonSet", "ds-uid", 2, "a:2", app="agent"),
            _cr("agent-3", "DaemonSet", "ds-uid", 3, "a:3", app="agent"),
        ]
    }
    assert "revision 7 not found" in ww.rollback_workload.func("DaemonSet", NS, "agent", 7).lower()
    assert "is the current one" in ww.rollback_workload.func("DaemonSet", NS, "agent", 3)
    assert api.patches == []
    out = ww.rollback_workload.func("DaemonSet", NS, "agent", 0)
    assert "revision 2" in out
    assert (
        api.objects[ww.workload_path("DaemonSet", NS, "agent")]["spec"]["template"]["spec"]["containers"][0]["image"]
        == "a:2"
    )


def test_rollback_deployment_uses_replicasets(api):
    rs = lambda rev, image, uid="dep-uid": {  # noqa: E731
        "metadata": {
            "name": f"api-{rev}",
            "annotations": {"deployment.kubernetes.io/revision": str(rev)},
            "ownerReferences": [{"kind": "Deployment", "uid": uid}],
        },
        "spec": {
            "template": {
                "metadata": {"labels": {"app": "api", "pod-template-hash": f"h{rev}"}},
                "spec": {"containers": [{"name": "api", "image": image}]},
            }
        },
    }
    api.lists[f"/apis/apps/v1/namespaces/{NS}/replicasets"] = {
        "items": [rs(3, "quay.io/acme/api:v3"), rs(4, "quay.io/acme/api:v4"), rs(9, "x", uid="someone-else")]
    }
    out = ww.rollback_workload.func("Deployment", NS, "api", 0)
    assert "revision 3" in out
    tpl = api.objects[ww.workload_path("Deployment", NS, "api")]["spec"]["template"]
    assert tpl["spec"]["containers"][0]["image"] == "quay.io/acme/api:v3"
    assert "pod-template-hash" not in tpl["metadata"]["labels"]


def test_rollback_previews_for_every_new_verb(api):
    from sre_agent import confirm_preview

    api.put(_statefulset())
    for tool, inp in [
        ("scale_workload", {"kind": "StatefulSet", "namespace": NS, "name": "db", "replicas": 2}),
        ("restart_workload", {"kind": "StatefulSet", "namespace": NS, "name": "db"}),
    ]:
        preview = confirm_preview.build_preview(tool, inp)
        assert preview is not None and preview["kind"] == "workload_change" and preview["valid"], preview
    assert api.writes == []


def test_unknown_kind_refused(api):
    assert "kind must be" in ww.restart_workload.func("CronJob", NS, "x")


def test_write_ledger_records_the_kind_from_input():
    from sre_agent.rbac_history import agent_write_target

    assert agent_write_target("patch_workload_container", {"kind": "StatefulSet", "namespace": NS, "name": "db"}) == (
        "StatefulSet",
        NS,
        "db",
    )
    assert agent_write_target("restart_workload", {"kind": "DaemonSet", "namespace": NS, "name": "a"})[0] == "DaemonSet"


def test_new_tools_are_registered_writes():
    from sre_agent.k8s_tools import ALL_TOOLS, WRITE_TOOLS

    names = {t.name for t in ALL_TOOLS}
    for tool in ("patch_workload_container", "scale_workload", "restart_workload", "rollback_workload"):
        assert tool in names and tool in WRITE_TOOLS
