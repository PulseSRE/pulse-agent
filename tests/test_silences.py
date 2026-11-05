"""Silences from chat: matcher safety, who a silence is created as, and the confirm preview.

``create_silence``/``expire_silence`` are write tools (confirmation-gated);
``propose_silence``/``list_silences`` only read. Alertmanager is faked at the
module boundary (``sre_agent.alertmanager``), except in the client tests,
which fake ``urlopen`` to check the request itself.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from sre_agent import alertmanager, silence_tools, silences
from sre_agent.alertmanager import AlertmanagerError
from sre_agent.request_context import set_current_user
from sre_agent.silences import Matcher, SilenceInputError, parse_matchers, validate_matchers

SID = "1c4e6f0a-8a8b-4c1e-9d3f-2b7a5e6c9d10"


def _alert(name, severity="warning", **labels):
    return {
        "labels": {"alertname": name, "severity": severity, **labels},
        "startsAt": "2026-11-05T13:47:00Z",
        "status": {"state": "active"},
    }


FIRING = [
    _alert("KubeNodeUnreachable", node="worker-5"),
    _alert("KubeletDown", node="worker-5"),
    _alert("NodeFilesystemSpaceFillingUp", node="worker-5"),
    _alert("KubeNodeUnreachable", node="worker-6"),
    _alert("KubePodCrashLooping", "critical", namespace="payments", pod="api-7f9c4d-x2k8q"),
]


@pytest.fixture
def as_alice():
    set_current_user("alice@acme.com")
    yield "alice@acme.com"
    set_current_user(None)


@pytest.fixture
def no_user():
    set_current_user(None)
    yield


# ---- parsing and the safety rules ------------------------------------------------


def test_parses_all_four_operators_braces_and_escapes():
    ms = parse_matchers('{node="worker-5", alertname=~"A|B", severity!="info", job!~"x.*", msg="say \\"hi\\""}')
    assert [(m.name, m.op, m.value) for m in ms] == [
        ("node", "=", "worker-5"),
        ("alertname", "=~", "A|B"),
        ("severity", "!=", "info"),
        ("job", "!~", "x.*"),
        ("msg", "=", 'say "hi"'),
    ]
    assert str(ms[4]) == 'msg="say \\"hi\\""'


@pytest.mark.parametrize("text", ["node=worker-5", 'node="a" node="b"', '="x"', 'node=="x"'])
def test_unreadable_matchers_are_refused(text):
    with pytest.raises(SilenceInputError):
        parse_matchers(text)


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty set
        'alertname=~".*"',  # matches everything
        'alertname=~".+"',  # matches every alert that has a name
        'node=""',  # positive but matches only the absence
        'severity!="info"',  # only negative
        'alertname!~"Watchdog", node!="x"',  # only negative
        'namespace=~"(.*)"',
    ],
)
def test_empty_or_all_matching_sets_are_refused(text):
    with pytest.raises(SilenceInputError):
        validate_matchers(parse_matchers(text))


@pytest.mark.parametrize(
    "text",
    [
        'alertname="Watchdog"',
        'alertname=~"Watch.*", namespace="openshift-monitoring"',
        'alertname=~"Watchdog|KubeletDown"',
    ],
)
def test_watchdog_is_never_silenced(text):
    with pytest.raises(SilenceInputError, match="Watchdog"):
        validate_matchers(parse_matchers(text))


@pytest.mark.parametrize(
    "text",
    [
        'node="worker-5"',
        'node="worker-5", alertname=~"KubeNodeUnreachable|KubeletDown"',
        'pod=~"api-.*", severity!="info"',
    ],
)
def test_narrow_sets_pass(text):
    validate_matchers(parse_matchers(text))


def test_bad_regex_and_label_names_are_refused():
    with pytest.raises(SilenceInputError, match="compile"):
        validate_matchers([Matcher("node", "worker-(", is_regex=True)])
    with pytest.raises(SilenceInputError, match="label name"):
        validate_matchers([Matcher("9bad", "x")])


def test_matching_follows_alertmanager_rules():
    ms = parse_matchers('node="worker-5", alertname=~"KubeNodeUnreachable|KubeletDown"')
    hidden = silences.split_by_matchers(FIRING, ms)
    assert [a["alertname"] for a in hidden] == ["KubeNodeUnreachable", "KubeletDown"]
    # An absent label is the empty string: != "x" holds for an alert without it.
    assert silences.labels_match({"alertname": "A"}, parse_matchers('alertname="A", team!="x"'))


@pytest.mark.parametrize("minutes", [0, 4, 10081, "two", None])
def test_duration_outside_5m_to_7d_is_refused(minutes):
    with pytest.raises(SilenceInputError):
        silences.validate_duration(minutes)


def test_comment_is_required():
    with pytest.raises(SilenceInputError, match="comment"):
        silences.validate_comment("   ")


def test_request_body_names_the_signed_in_person_and_ends_after_the_duration():
    now = datetime(2026, 11, 5, 14, 35, tzinfo=UTC)
    req = silences.build_request(
        {"matchers": 'node="worker-5"', "duration_minutes": 120, "comment": "disk swap"}, "alice@acme.com", now
    )
    body = req.to_api()
    assert body == {
        "matchers": [{"name": "node", "value": "worker-5", "isRegex": False, "isEqual": True}],
        "startsAt": "2026-11-05T14:35:00Z",
        "endsAt": "2026-11-05T16:35:00Z",
        "createdBy": "alice@acme.com",
        "comment": "disk swap",
    }


def test_no_signed_in_person_means_no_silence():
    with pytest.raises(SilenceInputError, match="who you are"):
        silences.build_request({"matchers": 'node="w"', "duration_minutes": 60, "comment": "x"}, None)


# ---- the write tools -------------------------------------------------------------


def _call(tool, **args):
    out = tool.call(args)
    return out[0] if isinstance(out, tuple) else out


def test_create_silence_posts_the_validated_body_as_the_user(as_alice):
    with patch.object(alertmanager, "create_silence", return_value=SID) as post:
        text = _call(
            silence_tools.create_silence,
            matchers='node="worker-5", alertname=~"KubeNodeUnreachable|KubeletDown"',
            duration_minutes=120,
            comment="Planned disk replacement",
        )
    body = post.call_args.args[0]
    assert body["createdBy"] == "alice@acme.com"
    assert body["comment"] == "Planned disk replacement"
    assert [m["name"] for m in body["matchers"]] == ["node", "alertname"]
    assert SID in text and "expire_silence" in text


def test_create_silence_refuses_an_all_matching_set_without_calling_alertmanager(as_alice):
    with patch.object(alertmanager, "create_silence") as post:
        text = _call(silence_tools.create_silence, matchers='alertname=~".*"', duration_minutes=60, comment="x")
    post.assert_not_called()
    assert text.startswith("Silence not created") and "every alert" in text


def test_create_silence_refuses_without_a_signed_in_person(no_user):
    with patch.object(alertmanager, "create_silence") as post:
        text = _call(silence_tools.create_silence, matchers='node="w"', duration_minutes=60, comment="x")
    post.assert_not_called()
    assert "who you are" in text


def test_create_silence_reports_a_403_with_the_role_needed(as_alice):
    with patch.object(alertmanager, "create_silence", side_effect=AlertmanagerError(403, "Forbidden")):
        text = _call(silence_tools.create_silence, matchers='node="w"', duration_minutes=60, comment="x")
    assert "not created" in text and "monitoring-alertmanager-edit" in text


def test_expire_silence_reads_then_deletes(as_alice):
    silence = {
        "id": SID,
        "status": {"state": "active"},
        "matchers": [{"name": "node", "value": "worker-5", "isRegex": False}],
        "createdBy": "dave",
        "comment": "disk",
    }
    with (
        patch.object(alertmanager, "get_silence", return_value=silence),
        patch.object(alertmanager, "expire_silence") as delete,
    ):
        text = _call(silence_tools.expire_silence, silence_id=SID)
    delete.assert_called_once_with(SID)
    assert 'node="worker-5"' in text and "notify again" in text


def test_expire_silence_already_expired_changes_nothing(as_alice):
    with (
        patch.object(
            alertmanager, "get_silence", return_value={"id": SID, "status": {"state": "expired"}, "matchers": []}
        ),
        patch.object(alertmanager, "expire_silence") as delete,
    ):
        text = _call(silence_tools.expire_silence, silence_id=SID)
    delete.assert_not_called()
    assert "already expired" in text


def test_expire_silence_rejects_a_non_uuid_and_reports_alertmanager_errors(as_alice):
    assert "UUID" in _call(silence_tools.expire_silence, silence_id="../../silences")
    with patch.object(alertmanager, "get_silence", side_effect=AlertmanagerError(404, "silence not found")):
        assert "404" in _call(silence_tools.expire_silence, silence_id=SID)


def test_write_tools_are_confirmation_gated_and_reads_are_not():
    from sre_agent.tool_registry import get_write_tools

    writes = get_write_tools()
    assert {"create_silence", "expire_silence"} <= writes
    assert not {"propose_silence", "list_silences"} & writes


# ---- the read tools --------------------------------------------------------------


def test_propose_for_a_node_names_both_alerts_and_what_stays_visible():
    with patch.object(alertmanager, "list_alerts", return_value=FIRING):
        text = _call(silence_tools.propose_silence, node="worker-5", reason="Planned disk replacement")
    assert 'node="worker-5", alertname=~"KubeNodeUnreachable|KubeletDown|NodeFilesystemSpaceFillingUp"' in text
    # Narrowed by alertname, the other alert on the node stays visible.
    with patch.object(alertmanager, "list_alerts", return_value=FIRING):
        text = _call(silence_tools.propose_silence, node="worker-5", alertname="KubeletDown")
    assert 'node="worker-5", alertname="KubeletDown"' in text
    assert "Would NOT hide" in text and "NodeFilesystemSpaceFillingUp" in text
    assert "worker-6" not in text


def test_propose_from_an_inbox_item_uses_its_resource_and_due_time():
    import time as _time

    item = {
        "id": "inb-1",
        "title": "Replace disk on worker-5",
        "resources": [{"kind": "Node", "name": "worker-5"}],
        "correlation_key": "maintenance::Node/worker-5",
        "due_date": _time.time() + 110 * 60,
        "claimed_by": "dave",
    }
    with (
        patch.object(silence_tools, "_lookup_item", return_value=item),
        patch.object(alertmanager, "list_alerts", return_value=FIRING),
    ):
        text = _call(silence_tools.propose_silence, finding_id="inb-1", reason="Planned disk replacement")
    assert 'node="worker-5"' in text
    assert "duration_minutes: 120" in text and "due time" in text
    assert "(Inbox: Replace disk on worker-5)" in text and "Inbox owner: dave" in text


def test_propose_says_when_nothing_is_firing_and_when_alerts_cannot_be_read():
    with patch.object(alertmanager, "list_alerts", return_value=FIRING):
        assert "nothing to silence" in _call(silence_tools.propose_silence, node="worker-9")
    with patch.object(alertmanager, "list_alerts", side_effect=AlertmanagerError(0, "timed out")):
        text = _call(silence_tools.propose_silence, node="worker-5")
    assert "could not be read" in text and "proposed" in text


def test_list_silences_filters_by_state_and_reports_failures():
    data = [
        {
            "id": SID,
            "status": {"state": "active"},
            "matchers": [{"name": "node", "value": "w"}],
            "endsAt": "e",
            "createdBy": "a",
            "comment": "c",
        },
        {"id": "x", "status": {"state": "expired"}, "matchers": [], "endsAt": "e", "createdBy": "b", "comment": "d"},
    ]
    with patch.object(alertmanager, "list_silences", return_value=data):
        text = _call(silence_tools.list_silences)
    assert SID in text and "expired" not in text.split("\n", 1)[1]
    with patch.object(alertmanager, "list_silences", side_effect=AlertmanagerError(403, "Forbidden")):
        assert "could not be read" in _call(silence_tools.list_silences)


# ---- confirm preview ---------------------------------------------------------------


def test_create_preview_shows_creator_end_time_and_what_it_hides(as_alice):
    with patch.object(alertmanager, "list_alerts", return_value=FIRING):
        p = silences.preview_create(
            {
                "matchers": 'node="worker-5", alertname=~"KubeNodeUnreachable|KubeletDown"',
                "duration_minutes": 120,
                "comment": "disk",
            }
        )
    assert p["valid"] is True and p["createdBy"] == "alice@acme.com" and p["duration"] == "2 hours"
    assert [m["text"] for m in p["matchers"]] == ['node="worker-5"', 'alertname=~"KubeNodeUnreachable|KubeletDown"']
    assert [a["alertname"] for a in p["hides"]] == ["KubeNodeUnreachable", "KubeletDown"]


def test_create_preview_when_firing_alerts_cannot_be_read_says_unknown_not_nothing(as_alice):
    with patch.object(alertmanager, "list_alerts", side_effect=AlertmanagerError(403, "Forbidden")):
        p = silences.preview_create({"matchers": 'node="w"', "duration_minutes": 60, "comment": "x"})
    assert p["valid"] is True and p["hides"] is None and "403" in p["hidesError"]


def test_invalid_create_preview_carries_the_refusal(as_alice):
    p = silences.preview_create({"matchers": 'alertname=~".*"', "duration_minutes": 60, "comment": "x"})
    assert p["valid"] is False and "every alert" in p["error"]


def test_expire_preview_reads_the_silence():
    silence = {
        "id": SID,
        "status": {"state": "active"},
        "matchers": [{"name": "node", "value": "w"}],
        "createdBy": "dave",
        "comment": "c",
        "endsAt": "e",
    }
    with patch.object(alertmanager, "get_silence", return_value=silence):
        p = silences.preview_expire({"silence_id": SID})
    assert p["valid"] and p["createdBy"] == "dave" and p["matchers"][0]["text"] == 'node="w"'


def test_confirm_request_carries_the_preview():
    from sre_agent.api import agent_ws

    ws = MagicMock()
    sent: list[dict] = []

    async def send_json(msg):
        sent.append(msg)

    ws.send_json = send_json

    async def run():
        await agent_ws._create_and_register_future("s-preview", "create_silence", {"a": 1}, ws, preview={"kind": "k"})
        await agent_ws._create_and_register_future("s-plain", "scale_deployment", {"b": 2}, ws)

    asyncio.run(run())
    assert sent[0]["preview"] == {"kind": "k"} and sent[0]["tool"] == "create_silence"
    assert "preview" not in sent[1]
    for sid in ("s-preview", "s-plain"):
        agent_ws._pending_confirms.pop(sid, None)
        agent_ws._pending_nonces.pop(sid, None)
        agent_ws._pending_timestamps.pop(sid, None)


def test_preview_is_built_only_for_tools_that_have_one_and_failures_are_left_out(as_alice):
    from sre_agent import confirm_preview

    assert asyncio.run(confirm_preview.build_preview_async("scale_deployment", {})) is None
    with patch.object(alertmanager, "list_alerts", return_value=[]):
        p = asyncio.run(
            confirm_preview.build_preview_async(
                "create_silence", {"matchers": 'node="w"', "duration_minutes": 60, "comment": "x"}
            )
        )
    assert p is not None and p["kind"] == "silence_create" and p["hides"] == []
    with patch.object(silences, "build_request", side_effect=RuntimeError("boom")):
        assert confirm_preview.build_preview("create_silence", {}) is None


# ---- the HTTP client ---------------------------------------------------------------


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_client_sends_the_callers_token_and_decodes_json(monkeypatch):
    from sre_agent.k8s_client import user_token_context

    monkeypatch.setenv("PULSE_AGENT_ALERTMANAGER_URL", "https://am.example:9094")
    from sre_agent.config import _reset_settings

    _reset_settings()
    seen = {}

    def fake_urlopen(req, context=None, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data) if req.data else None
        return _Resp(json.dumps({"silenceID": SID}).encode())

    with patch("urllib.request.urlopen", fake_urlopen), user_token_context("caller-token"):
        assert alertmanager.create_silence({"matchers": []}) == SID
    assert seen == {
        "url": "https://am.example:9094/api/v2/silences",
        "auth": "Bearer caller-token",
        "method": "POST",
        "body": {"matchers": []},
    }


def test_client_turns_http_and_network_errors_into_alertmanager_errors():
    def forbidden(req, context=None, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"forbidden: alertmanagers/api"))

    with patch("urllib.request.urlopen", forbidden), pytest.raises(AlertmanagerError) as e:
        alertmanager.list_silences()
    assert e.value.status == 403 and "alertmanagers/api" in e.value.detail

    def down(req, context=None, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", down), pytest.raises(AlertmanagerError) as e:
        alertmanager.list_alerts()
    assert e.value.status == 0 and "could not be reached" in e.value.describe()
