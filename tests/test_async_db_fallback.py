"""Tests for narrowed async DB exception handling and fire-and-forget task tracking.

Covers:
- ASYNC_DB_ERRORS canonical tuple in async_db (re-exported by monitor modules)
- _pending_record_tasks WeakSet in tool_usage
- Fallback from async to sync on expected DB errors only
- Unexpected errors propagate instead of silently falling back
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestAsyncDbErrorsTuple:
    """Verify ASYNC_DB_ERRORS is canonical and re-exported consistently."""

    def test_canonical_tuple_in_async_db(self):
        from sre_agent.async_db import ASYNC_DB_ERRORS

        assert OSError in ASYNC_DB_ERRORS
        # ConnectionError is a subclass of OSError — caught implicitly
        assert issubclass(ConnectionError, OSError)
        try:
            import asyncpg

            assert asyncpg.PostgresError in ASYNC_DB_ERRORS
        except ImportError:
            assert len(ASYNC_DB_ERRORS) == 1

    def test_all_modules_share_same_tuple(self):
        from sre_agent.async_db import ASYNC_DB_ERRORS
        from sre_agent.monitor.handoff_processor import _ASYNC_DB_ERRORS as HP_ERRORS
        from sre_agent.monitor.investigation_runner import _ASYNC_DB_ERRORS as IR_ERRORS
        from sre_agent.monitor.verification_pipeline import _ASYNC_DB_ERRORS as VP_ERRORS

        assert HP_ERRORS is ASYNC_DB_ERRORS
        assert IR_ERRORS is ASYNC_DB_ERRORS
        assert VP_ERRORS is ASYNC_DB_ERRORS

    def test_does_not_include_generic_exception(self):
        from sre_agent.async_db import ASYNC_DB_ERRORS

        assert Exception not in ASYNC_DB_ERRORS
        assert RuntimeError not in ASYNC_DB_ERRORS
        assert ValueError not in ASYNC_DB_ERRORS

    def test_catches_connection_error_via_oserror(self):
        from sre_agent.async_db import ASYNC_DB_ERRORS

        assert issubclass(ConnectionError, tuple(ASYNC_DB_ERRORS))


class TestHandoffProcessorFallback:
    """Verify handoff_processor falls back to sync on _ASYNC_DB_ERRORS, not on all exceptions."""

    @pytest.fixture
    def mock_monitor(self):
        m = MagicMock()
        m._client = MagicMock()
        return m

    def test_falls_back_to_sync_on_connection_error(self, mock_monitor):
        from sre_agent.monitor.handoff_processor import process_handoffs

        mock_repo = MagicMock()
        mock_repo.async_get_pending_handoffs = AsyncMock(side_effect=ConnectionError("refused"))
        mock_repo.get_pending_handoffs = MagicMock(return_value=[])

        with patch("sre_agent.monitor.handoff_processor.get_monitor_repo", return_value=mock_repo):
            _run(process_handoffs(mock_monitor))

        mock_repo.get_pending_handoffs.assert_called_once()

    def test_falls_back_to_sync_on_os_error(self, mock_monitor):
        from sre_agent.monitor.handoff_processor import process_handoffs

        mock_repo = MagicMock()
        mock_repo.async_get_pending_handoffs = AsyncMock(side_effect=OSError("broken pipe"))
        mock_repo.get_pending_handoffs = MagicMock(return_value=[])

        with patch("sre_agent.monitor.handoff_processor.get_monitor_repo", return_value=mock_repo):
            _run(process_handoffs(mock_monitor))

        mock_repo.get_pending_handoffs.assert_called_once()

    def test_does_not_fall_back_on_value_error(self, mock_monitor):
        from sre_agent.monitor.handoff_processor import process_handoffs

        mock_repo = MagicMock()
        mock_repo.async_get_pending_handoffs = AsyncMock(side_effect=ValueError("bad data"))

        with patch("sre_agent.monitor.handoff_processor.get_monitor_repo", return_value=mock_repo):
            with pytest.raises(ValueError, match="bad data"):
                _run(process_handoffs(mock_monitor))

        mock_repo.get_pending_handoffs.assert_not_called()

    def test_cleanup_falls_back_on_connection_error(self, mock_monitor):
        from sre_agent.monitor.handoff_processor import process_handoffs

        mock_repo = MagicMock()
        mock_repo.async_get_pending_handoffs = AsyncMock(
            return_value=[
                {
                    "details": '{"target": "security_agent", "namespace": "ns1", "context": "test"}',
                    "namespace": "ns1",
                }
            ]
        )
        mock_repo.async_delete_processed_handoffs = AsyncMock(side_effect=ConnectionError("gone"))
        mock_repo.delete_processed_handoffs = MagicMock()

        with (
            patch("sre_agent.monitor.handoff_processor.get_monitor_repo", return_value=mock_repo),
            patch(
                "sre_agent.monitor.handoff_processor._run_security_followup",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            _run(process_handoffs(mock_monitor))

        mock_repo.delete_processed_handoffs.assert_called_once()


class TestInvestigationRunnerFallback:
    """Verify investigation_runner falls back to sync save on _ASYNC_DB_ERRORS."""

    def test_catches_db_errors(self):
        from sre_agent.monitor.investigation_runner import _ASYNC_DB_ERRORS

        assert issubclass(ConnectionError, tuple(_ASYNC_DB_ERRORS))
        assert OSError in _ASYNC_DB_ERRORS

    def test_unexpected_error_not_in_tuple(self):
        from sre_agent.monitor.investigation_runner import _ASYNC_DB_ERRORS

        assert TypeError not in _ASYNC_DB_ERRORS
        assert KeyError not in _ASYNC_DB_ERRORS


class TestVerificationPipelineFallback:
    """Verify verification_pipeline falls back to sync on _ASYNC_DB_ERRORS."""

    def _make_monitor(self, action_id, finding_id):
        m = MagicMock()
        m._pending_verifications = {
            action_id: {
                "target_scan": 0,
                "category": "crashloop",
                "resources": [{"kind": "Pod", "namespace": "ns1", "name": "web-1"}],
                "finding_id": finding_id,
            }
        }
        m._scan_counter = 1
        m._broadcast_raw = AsyncMock()
        return m

    def test_falls_back_to_sync_update_on_connection_error(self):
        from sre_agent.monitor.verification_pipeline import process_verifications

        mock_monitor = self._make_monitor("act-1", "f-1")
        mock_repo = MagicMock()
        mock_repo.async_update_action_verification = AsyncMock(side_effect=ConnectionError("refused"))
        mock_repo.async_get_investigation_by_finding_id = AsyncMock(return_value=None)

        # This test is about the DB fallback, not about cluster health. Without
        # a stub the health gate correctly reports "unverifiable" (there is no
        # cluster in CI), so pin the gate to a pass and let the test assert the
        # thing it is named for.
        with (
            patch("sre_agent.monitor.verification_pipeline.get_monitor_repo", return_value=mock_repo),
            patch("sre_agent.monitor.verification_pipeline._sync_update_action_verification") as mock_sync,
            patch(
                "sre_agent.monitor.health_gate.check_resources",
                return_value=("pass", "Deployment prod/web has 1/1 replicas ready"),
            ),
        ):
            _run(process_verifications(mock_monitor, []))

        mock_sync.assert_called_once()
        args = mock_sync.call_args[0]
        assert args[0] == "act-1"
        assert args[1] == "verified"
        assert isinstance(args[2], str)

    def test_does_not_fall_back_on_runtime_error(self):
        from sre_agent.monitor.verification_pipeline import process_verifications

        mock_monitor = self._make_monitor("act-2", "f-2")
        mock_repo = MagicMock()
        mock_repo.async_update_action_verification = AsyncMock(side_effect=RuntimeError("unexpected"))

        with patch("sre_agent.monitor.verification_pipeline.get_monitor_repo", return_value=mock_repo):
            with pytest.raises(RuntimeError, match="unexpected"):
                _run(process_verifications(mock_monitor, []))


class TestPendingRecordTasks:
    """Verify _pending_record_tasks WeakSet prevents GC of fire-and-forget tasks."""

    def test_weakset_exists(self):
        import weakref

        from sre_agent.tool_usage import _pending_record_tasks

        assert isinstance(_pending_record_tasks, weakref.WeakSet)

    def test_task_tracked_during_execution(self):
        from sre_agent.tool_usage import _pending_record_tasks, build_tool_result_handler

        handler = build_tool_result_handler("test-sess", "sre", set())

        async def run():
            original_create_task = asyncio.get_running_loop().create_task
            tasks_created = []

            def tracking_create_task(coro, **kwargs):
                task = original_create_task(coro, **kwargs)
                tasks_created.append(task)
                return task

            with (
                patch("sre_agent.tool_usage.asyncio.get_running_loop") as mock_loop_fn,
                patch("sre_agent.tool_usage.record_tool_call_async", new_callable=AsyncMock),
            ):
                mock_loop = MagicMock()
                mock_loop.create_task = tracking_create_task
                mock_loop_fn.return_value = mock_loop

                handler(
                    {
                        "tool_name": "list_pods",
                        "status": "success",
                        "turn_number": 1,
                        "duration_ms": 100,
                        "result_bytes": 500,
                    }
                )

                if tasks_created:
                    assert tasks_created[0] in _pending_record_tasks
                    await tasks_created[0]
                    assert tasks_created[0] not in _pending_record_tasks

        _run(run())

    def test_handler_sync_fallback_without_event_loop(self):
        from sre_agent.tool_usage import build_tool_result_handler

        handler = build_tool_result_handler("test-sess", "sre", set())

        with patch("sre_agent.tool_usage.record_tool_call") as mock_sync:
            handler(
                {
                    "tool_name": "list_pods",
                    "status": "success",
                    "turn_number": 1,
                    "duration_ms": 100,
                    "result_bytes": 500,
                }
            )
            mock_sync.assert_called_once()

    def test_done_callback_removes_from_weakset(self):
        from sre_agent.tool_usage import _pending_record_tasks

        async def run():
            task = asyncio.get_running_loop().create_task(asyncio.sleep(0))
            _pending_record_tasks.add(task)
            task.add_done_callback(_pending_record_tasks.discard)
            assert task in _pending_record_tasks
            await task
            await asyncio.sleep(0)
            assert task not in _pending_record_tasks

        _run(run())
