"""A proposal for a symptom should say whose symptom it is.

Measured on the reference cluster: the Inbox showed "4 fixes waiting on you",
each with an Approve button, targeting exactly the four pods the same screen
labelled "Explained by the cause above — not separate problems" under an open
``HighOverallControlPlaneMemory`` episode. Same pods, same restart counts.

Approving any of them restarts a pod whose cause is control-plane memory
pressure; it crashloops again while the memory stays high. The causal engine
knew this. The action panel did not say so, and put the most actionable-looking
control on the screen next to it.

Labelled rather than suppressed: restarting a symptom is sometimes a legitimate
stopgap, and that is the operator's call. It is only wrong to ask them to make
it blind — the same reasoning the webhook already applies when it stays silent
for findings an open episode explains.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sre_agent.monitor.episodes import explaining_cause


def _repo(index: dict, episodes: list[dict]) -> MagicMock:
    repo = MagicMock()
    repo.open_symptom_index.return_value = index
    repo.list_open.return_value = episodes
    return repo


def test_it_names_the_cause_that_explains_a_symptom():
    repo = _repo(
        {"crashloop:mce:Pod/ocm-controller": "ep-1"},
        [{"id": "ep-1", "cause_title": "HighOverallControlPlaneMemory"}],
    )
    with patch("sre_agent.monitor.episodes._repo", return_value=repo):
        assert explaining_cause("crashloop:mce:Pod/ocm-controller") == "HighOverallControlPlaneMemory"


def test_a_finding_no_episode_explains_gets_no_label():
    repo = _repo({"other:key": "ep-1"}, [{"id": "ep-1", "cause_title": "X"}])
    with patch("sre_agent.monitor.episodes._repo", return_value=repo):
        assert explaining_cause("crashloop:mce:Pod/ocm-controller") is None


def test_an_empty_key_is_not_a_lookup():
    repo = _repo({"": "ep-1"}, [{"id": "ep-1", "cause_title": "X"}])
    with patch("sre_agent.monitor.episodes._repo", return_value=repo):
        assert explaining_cause("") is None
    repo.open_symptom_index.assert_not_called()


def test_an_episode_with_no_title_does_not_produce_an_empty_label():
    """An empty string beside an Approve button reads as a rendering fault."""
    repo = _repo({"k": "ep-1"}, [{"id": "ep-1", "cause_title": ""}])
    with patch("sre_agent.monitor.episodes._repo", return_value=repo):
        assert explaining_cause("k") is None


def test_a_database_that_will_not_answer_does_not_break_the_proposal():
    """The label is an enhancement. Losing it must not lose the fix proposal."""
    repo = MagicMock()
    repo.open_symptom_index.side_effect = RuntimeError("db down")
    with patch("sre_agent.monitor.episodes._repo", return_value=repo):
        assert explaining_cause("k") is None


def test_the_proposal_path_attaches_the_label():
    """The wiring, not just the helper: auto_fix must set explainedBy."""
    import inspect

    from sre_agent.monitor import cluster_monitor

    src = inspect.getsource(cluster_monitor.ClusterMonitor.auto_fix)
    assert 'action_report["explainedBy"] = cause_title' in src
    assert "explaining_cause" in src
