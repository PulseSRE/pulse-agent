"""The inbox must not list an item that an open episode already explains.

Without this, an episode *adds* rows: the panel shows a cause with its
fourteen symptoms folded underneath, and the queue below still lists the same
fourteen. The complaint episodes exist to answer was volume, so duplicating
the list is worse than not having them.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sre_agent.inbox import _collapse_episode_symptoms

INDEX = "sre_agent.monitor.episodes.symptom_keys_by_episode"


def _item(key: str, title: str = "x"):
    return {"correlation_key": key, "title": title}


def test_a_symptom_of_an_open_episode_is_taken_out():
    items = [_item("workloads:mce:Deployment/ocm-controller"), _item("crashloop:demo:Pod/web")]
    with patch(INDEX, return_value={"workloads:mce:Deployment/ocm-controller": "ep-1"}):
        kept, collapsed = _collapse_episode_symptoms(items)
    assert collapsed == 1
    assert [i["correlation_key"] for i in kept] == ["crashloop:demo:Pod/web"]


def test_the_count_comes_back_so_the_ui_can_say_what_it_hid():
    """Items vanishing from a work queue with no explanation loses people."""
    items = [_item(f"crashloop:ns{i}:Pod/p") for i in range(5)]
    index = {i["correlation_key"]: "ep-1" for i in items[:3]}
    with patch(INDEX, return_value=index):
        kept, collapsed = _collapse_episode_symptoms(items)
    assert collapsed == 3
    assert len(kept) == 2


def test_nothing_is_hidden_when_no_episode_is_open():
    items = [_item("a"), _item("b")]
    with patch(INDEX, return_value={}):
        kept, collapsed = _collapse_episode_symptoms(items)
    assert kept == items
    assert collapsed == 0


def test_an_item_with_no_correlation_key_is_always_kept():
    """Hand-created tasks have no key and are nobody's symptom."""
    items = [{"title": "look at the thing"}, _item("a")]
    with patch(INDEX, return_value={"a": "ep-1"}):
        kept, collapsed = _collapse_episode_symptoms(items)
    assert collapsed == 1
    assert kept == [{"title": "look at the thing"}]


def test_the_cause_itself_is_never_collapsed():
    """The episode index holds symptoms only, so the cause stays in the queue."""
    cause = _item("control_plane::Etcd/cluster")
    symptom = _item("workloads:mce:Deployment/ocm-controller")
    with patch(INDEX, return_value={symptom["correlation_key"]: "ep-1"}):
        kept, _ = _collapse_episode_symptoms([cause, symptom])
    assert kept == [cause]


def test_a_broken_episode_lookup_shows_everything_rather_than_nothing():
    """Failing open is the only safe direction: never hide work by accident."""
    items = [_item("a"), _item("b")]
    with patch(INDEX, side_effect=RuntimeError("db down")):
        kept, collapsed = _collapse_episode_symptoms(items)
    assert kept == items
    assert collapsed == 0


@pytest.mark.parametrize("bad", [None, {}])
def test_an_empty_index_is_not_treated_as_hiding_everything(bad):
    items = [_item("a")]
    with patch(INDEX, return_value=bad or {}):
        kept, collapsed = _collapse_episode_symptoms(items)
    assert kept == items
    assert collapsed == 0
