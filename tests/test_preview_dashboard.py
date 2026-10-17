"""preview_dashboard lets the view designer see the draft page before saving it.

The screenshot travels as an image attached to the tool result (tool_images),
which the agent loop sends as an image block and drops from history once a
newer one arrives.
"""

import json
import urllib.error
from unittest.mock import patch

import pytest

from sre_agent import tool_images, view_draft, view_render
from sre_agent.k8s_client import user_token_context
from sre_agent.view_tools import MAX_PREVIEWS_PER_TURN, preview_dashboard


def _chart(title):
    return {"kind": "chart", "title": title, "series": [{"name": "a", "data": [[0, 1], [1, 2]]}]}


RENDERED = {
    "image": "iVBORw0KGgo=",
    "width": 1440,
    "height": 1200,
    "cells": [
        {"title": "CPU", "text": "CPU 42%", "scrolls": False, "clipped": False},
        {"title": "Memory", "text": "Memory No data", "scrolls": False, "clipped": False},
        {"title": "Pods", "text": "Pods a b c", "scrolls": False, "clipped": True},
    ],
    "pageErrors": [],
    "error": "",
    "truncated": False,
}


@pytest.fixture
def draft():
    token = view_draft.bind([_chart("CPU"), _chart("Memory"), _chart("Pods")])
    yield view_draft.current()
    view_draft.unbind(token)


@pytest.fixture
def configured():
    with patch.object(view_render, "configured", return_value=True):
        yield


def _preview(**kw):
    return (
        preview_dashboard.call({"title": "Prod", **kw})
        if hasattr(preview_dashboard, "call")
        else preview_dashboard(title="Prod", **kw)
    )


class TestPreviewDashboard:
    def test_unavailable_without_render_service(self, draft):
        with patch.object(view_render, "configured", return_value=False):
            assert "not available" in _preview()

    def test_needs_a_draft(self, configured):
        assert "no dashboard is being built" in _preview()

    def test_needs_the_users_token(self, configured, draft):
        with patch.object(view_render, "render_draft") as render:
            out = _preview()
        assert "user's session" in out
        render.assert_not_called()

    def test_renders_the_draft_as_it_will_be_saved(self, configured, draft):
        with (
            user_token_context("alice-token"),
            patch.object(view_render, "render_draft", return_value=RENDERED) as render,
        ):
            out = _preview(rows="CPU | Memory\nPods", exclude="")
        spec, token = render.call_args.args
        assert token == "alice-token"
        assert [w["title"] for w in spec["layout"]] == ["CPU", "Memory", "Pods"]
        assert set(spec["positions"]) == {0, 1, 2}
        # CPU and Memory share the first row, as planned.
        assert spec["positions"][0]["y"] == spec["positions"][1]["y"] < spec["positions"][2]["y"]

        image, text = tool_images.split_image(out)
        assert image == {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}
        assert "'Memory' is empty" in text
        assert "'Pods' is cut off" in text
        assert "CPU" not in text.split("Found on the page:")[1].split("Look for")[0]

    def test_exclusions_leave_widgets_out(self, configured, draft):
        with user_token_context("t"), patch.object(view_render, "render_draft", return_value=RENDERED) as render:
            _preview(exclude="Memory")
        assert [w["title"] for w in render.call_args.args[0]["layout"]] == ["CPU", "Pods"]

    def test_limits_previews_per_turn(self, configured, draft):
        with user_token_context("t"), patch.object(view_render, "render_draft", return_value=RENDERED) as render:
            for _ in range(MAX_PREVIEWS_PER_TURN + 1):
                out = _preview()
        assert render.call_count == MAX_PREVIEWS_PER_TURN
        assert "limit reached" in out

    def test_render_failure_says_to_save(self, configured, draft):
        with (
            user_token_context("t"),
            patch.object(
                view_render,
                "render_draft",
                side_effect=view_render.RenderUnavailableError("render service answered 429"),
            ),
        ):
            out = _preview()
        assert "429" in out and "create_dashboard" in out
        assert tool_images.split_image(out)[0] is None


class TestRenderClient:
    def _settings(self, url="http://render:8090", token="s3cret"):
        from types import SimpleNamespace

        return SimpleNamespace(agent=SimpleNamespace(render_url=url, render_token=token))

    def test_unconfigured_raises(self):
        with patch.object(view_render, "get_settings", return_value=self._settings(url="")):
            assert not view_render.configured()
            with pytest.raises(view_render.RenderUnavailableError):
                view_render.render_draft({}, "t")

    def test_posts_with_service_token(self):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(RENDERED).encode()

        with (
            patch.object(view_render, "get_settings", return_value=self._settings()),
            patch("urllib.request.urlopen", return_value=Resp()) as urlopen,
        ):
            assert view_render.render_draft({"title": "x", "layout": []}, "alice")["width"] == 1440
        req = urlopen.call_args.args[0]
        assert req.full_url == "http://render:8090/render"
        assert req.get_header("Authorization") == "Bearer s3cret"
        assert json.loads(req.data)["userToken"] == "alice"

    def test_http_error_carries_service_message(self):
        import io

        err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error":"too many widgets"}'))
        with (
            patch.object(view_render, "get_settings", return_value=self._settings()),
            patch("urllib.request.urlopen", side_effect=err),
        ):
            with pytest.raises(view_render.RenderUnavailableError, match="400: too many widgets"):
                view_render.render_draft({}, "t")

    def test_unreachable(self):
        with (
            patch.object(view_render, "get_settings", return_value=self._settings()),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")),
        ):
            with pytest.raises(view_render.RenderUnavailableError, match="unreachable"):
                view_render.render_draft({}, "t")


class TestToolImages:
    def test_round_trip(self):
        image, text = tool_images.split_image(tool_images.attach_image("hello", "AAAA"))
        assert image["data"] == "AAAA" and text == "hello"

    def test_plain_text_untouched(self):
        assert tool_images.split_image("hello") == (None, "hello")
        assert tool_images.result_content("hello") == "hello"

    def test_rejects_unknown_media_type(self):
        with pytest.raises(ValueError):
            tool_images.attach_image("x", "AAAA", "image/svg+xml")

    def test_result_content_is_image_then_text(self):
        content = tool_images.result_content(tool_images.attach_image("seen", "AAAA"))
        assert [b["type"] for b in content] == ["image", "text"]
        assert content[1]["text"] == "seen"

    def test_only_newest_image_kept(self):
        def turn(data):
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": data,
                        "content": tool_images.result_content(tool_images.attach_image("t", data)),
                    }
                ],
            }

        messages = [turn("old"), {"role": "assistant", "content": "ok"}, turn("new")]
        assert tool_images.drop_stale_images(messages) == 1
        old = messages[0]["content"][0]["content"]
        new = messages[2]["content"][0]["content"]
        assert old[0] == {"type": "text", "text": tool_images.STALE_NOTE}
        assert new[0]["type"] == "image"
        assert tool_images.drop_stale_images(messages) == 0


class TestAgentLoopImages:
    def test_execute_tool_caps_text_but_keeps_the_image_whole(self):
        from unittest.mock import MagicMock

        from sre_agent.agent import MAX_TOOL_RESULT_LENGTH, _execute_tool

        data = "A" * (MAX_TOOL_RESULT_LENGTH + 10)  # larger than the cap on its own
        tool = MagicMock()
        tool.call.return_value = tool_images.attach_image("x" * (MAX_TOOL_RESULT_LENGTH + 5), data)
        text, _component, meta = _execute_tool("preview_dashboard", {}, {"preview_dashboard": tool})
        image, rest = tool_images.split_image(text)
        assert image is not None and image["data"] == data
        assert "truncated" in rest
        assert meta["result_bytes"] == MAX_TOOL_RESULT_LENGTH + 5
