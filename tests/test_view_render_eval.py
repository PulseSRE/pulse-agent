"""The rendered-page eval's mock cluster, scripted model and report.

The eval itself needs a browser, a UI and a model, so it runs nightly. These
cover the pieces that decide whether its pages show anything meaningful: the
mock cluster's answers must agree with each other, the PromQL evaluator must
compute what Prometheus would, and the report must count what it saw.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("promql_parser")

from sre_agent.evals import view_render_eval as vre
from sre_agent.evals.mock_cluster import promql, scripted_model
from sre_agent.evals.mock_cluster import scenario as sc
from sre_agent.evals.mock_cluster.server import build_k8s_app, build_prometheus_app

T = time.time()


def scalar(query: str) -> float:
    kind, value = promql.evaluate(query, T)
    assert kind == "vector" and len(value) == 1, (query, value)
    return value[0][1]


class TestPromQL:
    def test_counts_agree_with_the_scenario(self):
        assert scalar("sum(kube_node_status_condition{condition='Ready',status='true'})") == len(sc.NODES)
        running = sum(1 for p in sc.PODS if p[4] == "Running")
        assert scalar("count(kube_pod_status_phase{phase='Running'} == 1)") == running
        assert scalar("count(ALERTS{alertstate='firing'}) or vector(0)") == len(sc.ALERTS)

    def test_metrics_real_exporters_do_not_publish_are_absent(self):
        # A modelled-but-fictional metric hides queries that read 0 on real clusters.
        assert promql.evaluate("kube_event_count", T) == ("vector", [])

    def test_rate_of_a_counter_is_its_modelled_rate(self):
        cores = scalar('sum(rate(container_cpu_usage_seconds_total{namespace="production"}[5m]))')
        expected = sum(p[7] for p in sc.PODS if p[2] == "production" and p[4] == "Running")
        assert cores == pytest.approx(expected, rel=0.1)

    def test_increase_scales_rate_by_the_window(self):
        per_second = scalar('sum(rate(kube_pod_container_status_restarts_total{pod="cache-7f9d8b-h3jxw"}[5m]))')
        per_hour = scalar('sum(increase(kube_pod_container_status_restarts_total{pod="cache-7f9d8b-h3jxw"}[1h]))')
        assert per_hour == pytest.approx(per_second * 3600)
        assert per_hour > 1  # the crash loop is visible as restarts, not a flat line

    def test_ratio_of_two_aggregations(self):
        used = scalar("100 - (sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)) * 100")
        expected = sum(n[5] for n in sc.NODES) / len(sc.NODES)
        assert used == pytest.approx(expected, abs=3)

    def test_topk_by_label_ranks_the_heaviest_pods(self):
        _, top = promql.evaluate(
            'topk(2, sum by (pod) (container_memory_working_set_bytes{namespace="production"}))', T
        )
        assert [lbl["pod"] for lbl, _ in top] == ["checkout-api-5d8f9c-x2k7q", "checkout-api-5d8f9c-p9q4m"]
        assert all(set(lbl) == {"pod"} for lbl, _ in top)

    def test_vector_matching_on_a_label(self):
        _, by_node = promql.evaluate(
            "sum by (node) (rate(container_cpu_usage_seconds_total[5m]))"
            ' / on(node) sum by (node) (kube_node_status_allocatable{resource="cpu"}) * 100',
            T,
        )
        assert {lbl["node"] for lbl, _ in by_node} <= {n[0] for n in sc.NODES}
        assert all(0 < v < 100 for _, v in by_node)

    def test_comparison_filters_and_bool_keeps(self):
        _, pending = promql.evaluate('sum by (namespace) (kube_pod_status_phase{phase="Pending"}) > 0', T)
        assert [lbl["namespace"] for lbl, _ in pending] == ["staging"]
        _, flags = promql.evaluate('sum by (namespace) (kube_pod_status_phase{phase="Pending"}) > bool 0', T)
        assert len(flags) > 1 and {v for _, v in flags} == {0.0, 1.0}

    def test_regex_matchers_anchor(self):
        assert scalar("sum(rate(apiserver_request_total{code=~'5..'}[5m]))") < 1
        assert scalar("sum(rate(apiserver_request_total{code!~'5..'}[5m]))") > 100

    def test_unknown_metric_is_empty_not_an_error(self):
        assert promql.evaluate("sum(rate(made_up_metric_total[5m]))", T) == ("vector", [])

    def test_invalid_query_raises(self):
        with pytest.raises(ValueError):
            promql.evaluate("sum(rate(x[5m]", T)


class TestPrometheusAPI:
    client = TestClient(build_prometheus_app())

    def test_instant_query(self):
        body = self.client.get("/api/v1/query", params={"query": "count(up)"}).json()
        assert body["status"] == "success" and body["data"]["resultType"] == "vector"
        assert float(body["data"]["result"][0]["value"][1]) > 0

    def test_range_query_returns_one_series_per_label_set(self):
        end = T
        body = self.client.get(
            "/api/v1/query_range",
            params={"query": "sum by (node) (node_load1)", "start": end - 3600, "end": end, "step": "60s"},
        ).json()
        result = body["data"]["result"]
        assert {s["metric"]["node"] for s in result} == {n[0] for n in sc.NODES}
        assert all(len(s["values"]) == 61 for s in result)

    def test_bad_query_is_a_400_like_prometheus(self):
        resp = self.client.get("/api/v1/query", params={"query": "sum("})
        assert resp.status_code == 400 and resp.json()["status"] == "error"

    def test_rules_carry_the_firing_alerts(self):
        groups = self.client.get("/api/v1/rules", params={"type": "alert"}).json()["data"]["groups"]
        firing = [a["labels"]["alertname"] for g in groups for r in g["rules"] for a in r["alerts"]]
        assert sorted(firing) == sorted(a[0] for a in sc.ALERTS)


class TestKubernetesAPI:
    client = TestClient(build_k8s_app())

    def test_namespaced_pods(self):
        items = self.client.get("/api/v1/namespaces/production/pods").json()["items"]
        assert {p["metadata"]["name"] for p in items} == {p[0] for p in sc.PODS if p[2] == "production"}
        cache = next(p for p in items if p["metadata"]["name"].startswith("cache-"))
        waiting = cache["status"]["containerStatuses"][0]["state"]["waiting"]
        assert waiting["reason"] == "CrashLoopBackOff"

    def test_table_api_when_asked(self):
        body = self.client.get(
            "/api/v1/nodes", headers={"Accept": "application/json;as=Table;g=meta.k8s.io;v=v1"}
        ).json()
        assert body["kind"] == "Table" and len(body["rows"]) == len(sc.NODES)

    def test_label_selector(self):
        items = self.client.get(
            "/apis/apps/v1/namespaces/production/deployments", params={"labelSelector": "app=cache"}
        ).json()["items"]
        assert [d["metadata"]["name"] for d in items] == ["cache"]

    def test_pending_pod_is_recent(self):
        pod = self.client.get("/api/v1/namespaces/staging/pods/checkout-api-8b2c1-t6vns").json()
        assert pod["metadata"]["creationTimestamp"] == sc.ago(25)

    def test_unknown_resource_is_404_status(self):
        resp = self.client.get("/api/v1/namespaces/production/widgets")
        assert resp.status_code == 404 and resp.json()["kind"] == "Status"


OFFERED_TOOLS = [
    {"name": n, "input_schema": {}}
    for n in ("namespace_summary", "get_prometheus_query", "get_events", "create_dashboard")
]


class TestScriptedModel:
    client = TestClient(scripted_model.build_app())

    def _turn(self, assistant_turns: int) -> dict:
        messages = [{"role": "user", "content": "build a dashboard"}]
        for _ in range(assistant_turns):
            messages += [{"role": "assistant", "content": []}, {"role": "user", "content": []}]
        return self.client.post(
            "/v1/messages", json={"model": "m", "tools": OFFERED_TOOLS, "messages": messages}
        ).json()

    def test_plays_the_script_skipping_tools_not_offered(self):
        first = [b["name"] for b in self._turn(0)["content"]]
        assert first == ["namespace_summary"]  # cluster_metrics was not offered
        assert [b["name"] for b in self._turn(1)["content"]] == ["get_prometheus_query", "get_events"]
        assert [b["name"] for b in self._turn(2)["content"]] == ["create_dashboard"]
        final = self._turn(3)
        assert final["stop_reason"] == "end_turn" and final["content"][0]["type"] == "text"

    def test_usage_has_integer_cache_fields(self):
        usage = self._turn(0)["usage"]
        assert usage["cache_read_input_tokens"] == 0 and usage["cache_creation_input_tokens"] == 0

    def test_streams_tool_input_as_json(self):
        with self.client.stream(
            "POST", "/v1/messages", json={"model": "m", "tools": OFFERED_TOOLS, "stream": True, "messages": []}
        ) as resp:
            events = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]
        deltas = [e["delta"]["partial_json"] for e in events if e["type"] == "content_block_delta"]
        assert json.loads(deltas[0]) == {"namespace": "production"}
        assert events[-1]["type"] == "message_stop"


class TestReport:
    def test_cells_classified_from_their_text(self):
        result = vre.CaseResult(id="x", prompt="p")
        vre._check_cells(
            result,
            [
                {"title": "CPU", "text": "CPU 6.4 cores", "scrolls": False, "clipped": False},
                {"title": "Memory", "text": "Query failed: bad_data", "scrolls": False, "clipped": False},
                {"title": "Alerts", "text": "No data", "scrolls": False, "clipped": True},
                {"title": "Logs", "text": "line\nline", "scrolls": True, "clipped": True},
            ],
        )
        assert result.cells == 4
        assert result.broken_cells == ["Memory"]
        assert result.empty_cells == ["Alerts"]
        assert result.overflowing_cells == ["Alerts"]  # Logs scrolls, so its overflow is reachable

    def test_markdown_flags_scripted_runs_and_averages_scores(self):
        good = vre.CaseResult(
            id="a",
            prompt="p",
            cells=3,
            kinds=["chart"],
            judge={d: 4 for d in vre.JUDGE_DIMENSIONS} | {"issues": ["axis unit wrong"]},
        )
        bad = vre.CaseResult(id="b", prompt="p", agent_error="no dashboard was saved")
        md = vre.render_markdown([good, bad], {"scripted": True, "model": "scripted", "judge_model": ""})
        assert "Scripted model" in md
        assert "| b | 0 () |" in md and "no dashboard was saved" in md
        assert "Mean judge score: **4.00**" in md and "axis unit wrong" in md

    def test_judge_json_is_extracted_from_prose(self):
        assert vre._parse_json('Here you go: {"layout": 3}')["layout"] == 3
        assert "error" in vre._parse_json("no json here")
