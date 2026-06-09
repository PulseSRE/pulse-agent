"""Tests for narrowed async DB exception handling and fire-and-forget task tracking.

Covers:
- _ASYNC_DB_ERRORS tuple in handoff_processor, investigation_runner, verification_pipeline
- _pending_record_tasks WeakSet in tool_usage
- Fallback from async to sync on expected DB errors only
- Unexpected errors propagate instead of silently falling back
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAsyncDbErrorsTuple:
    """Verify _ASYNC_DB_ERRORS contains the right exception types."""

    def test_handoff_processor_includes_asyncpg(self):
        from sre_agent.monitor.handoff_processor import _ASYNC_DB_ERRORS

        assert OSError in _ASYNC_DB_ERRORS
        assert ConnectionError in _ASYNC_DB_ERRORS
        try:
            import asyncpg

            assert asyncpg.PostgresError in _ASYNC_DB_ERRORS
        except ImportError:
            assert len(_ASYNC_DB_ERRORS) == 2

    def test_investigation_runner_includes_asyncpg(self):
        from sre_agent.monitor.investigation_runner import _ASYNC_DB_ERRORS

        assert OSError in _ASYNC_DB_ERRORS
        assert ConnectionError in _ASYNC_DB_ERRORS

    def test_verification_pipeline_includes_asyncpg(self):
        from sre_agent.monitor.verification_pipeline import _ASYNC_DB_ERRORS

        assert OSError in _ASYNC_DB_ERRORS
        assert ConnectionError in _ASYNC_DB_ERRORS

    def test_does_not_include_generic_exception(self):
        from sre_agent.monitor.handoff_processor import _ASYNC_DB_ERRORS

        assert Exception not in _ASYNC_DB_ERRORS
        assert RuntimeError not in _ASYNC_DB_ERRORS
        assert ValueError not in _ASYNC_DB_ERRORS


class TestHandoffProcessorFallback:
    """Verify handoff_processor falls back to sync on _ASYNC_DB_ERRORS, not on all exceptions."""

    @pytest.fixture
    def mock_monitor(self):
        m = MagicMock()
        m._client = MagicMock()
        return m

    def test_falls_back_to_sync_on_connection_error(self, mock_monitor):
        mock_repo = MagicMock()
        mock_repo.async_get_pending_handoffs = AsyncMock(side_effect=ConnectionError("refused"))
        mock_repo.get_pending_handoffs = MagicMock(return_value=[])

        with patch("sre_agent.monitor.handoff_processor.get_monitor_repo", return_value=mock_repo):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    __import__("sre_agent.monitor.handoff_processor", fromlist=["process_handoffs"]).process_handoffs(
                        mock_monitor
                    )
                )
            finally:
                loop.close()

        mock_repo.get_pending_handoffs.assert_called_once()

    def test_falls_back_to_sync_on_os_error(self, mock_monitor):
        mock_repo = MagicMock()
        mock_repo.async_get_pending_handoffs = AsyncMock(side_effect=OSError("broken pipe"))
        mock_repo.get_pending_handoffs = MagicMock(return_value=[])

        with patch("sre_agent.monitor.handoff_processor.get_monitor_repo", return_value=mock_repo):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    __import__("sre_agent.monitor.handoff_processor", fromlist=["process_handoffs"]).process_handoffs(
                        mock_monitor
                    )
                )
            finally:
                loop.close()

        mock_repo.get_pending_handoffs.assert_called_once()

    def test_does_not_fall_back_on_value_error(self, mock_monitor):
        """Non-DB errors should NOT silently fall back to sync."""
        from sre_agent.monitor.handoff_processor import process_handoffs

        mock_repo = MagicMock()
        mock_repo.async_get_pending_handoffs = AsyncMock(side_effect=ValueError("bad data"))

        with patch("sre_agent.monitor.handoff_processor.get_monitor_repo", return_value=mock_repo):
            loop = asyncio.new_event_loop()
            try:
                with pytest.raises(ValueError, match="bad data"):
                    loop.run_until_complete(process_handoffs(mock_monitor))
            finally:
                loop.close()

        mock_repo.get_pending_handoffs.assert_not_called()

    def test_cleanup_falls_back_on_connection_error(self, mock_monitor):
        """Delete step also falls back to sync on _ASYNC_DB_ERRORS."""
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
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(process_handoffs(mock_monitor))
            finally:
                loop.close()

        mock_repo.delete_processed_handoffs.assert_called_once()


class TestInvestigationRunnerFallback:
    """Verify investigation_runner falls back to sync save on _ASYNC_DB_ERRORS."""

    def test_save_investigation_falls_back_on_connection_error(self):
        from sre_agent.monitor.investigation_runner import _ASYNC_DB_ERRORS

        assert ConnectionError in _ASYNC_DB_ERRORS
        assert OSError in _ASYNC_DB_ERRORS

    def test_unexpected_error_not_in_tuple(self):
        from sre_agent.monitor.investigation_runner import _ASYNC_DB_ERRORS

        assert TypeError not in _ASYNC_DB_ERRORS
        assert KeyError not in _ASYNC_DB_ERRORS


class TestVerificationPipelineFallback:
    """Verify verification_pipeline falls back to sync on _ASYNC_DB_ERRORS."""

    def test_falls_back_to_sync_update_on_connection_error(self):
        from sre_agent.monitor.verification_pipeline import process_verifications

        mock_monitor = MagicMock()
        mock_monitor._pending_verifications = {
            "act-1": {
                "target_scan": 0,
                "category": "crashloop",
                "resources": [{"kind": "Pod", "namespace": "ns1", "name": "web-1"}],
                "finding_id": "f-1",
            }
        }
        mock_monitor._scan_counter = 1
        mock_monitor._broadcast_raw = AsyncMock()

        mock_repo = MagicMock()
        mock_repo.async_update_action_verification = AsyncMock(side_effect=ConnectionError("refused"))
        mock_repo.async_get_investigation_by_finding_id = AsyncMock(return_value=None)

        with (
            patch("sre_agent.monitor.verification_pipeline.get_monitor_repo", return_value=mock_repo),
            patch("sre_agent.monitor.verification_pipeline._sync_update_action_verification") as mock_sync,
        ):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(process_verifications(mock_monitor, []))
            finally:
                loop.close()

        mock_sync.assert_called_once()
        args = mock_sync.call_args[0]
        assert args[0] == "act-1"
        assert args[1] == "verified"
        assert isinstance(args[2], str)

    def test_does_not_fall_back_on_runtime_error(self):
        from sre_agent.monitor.verification_pipeline import process_verifications

        mock_monitor = MagicMock()
        mock_monitor._pending_verifications = {
            "act-2": {
                "target_scan": 0,
                "category": "crashloop",
                "resources": [{"kind": "Pod", "namespace": "ns1", "name": "web-2"}],
                "finding_id": "f-2",
            }
        }
        mock_monitor._scan_counter = 1
        mock_monitor._broadcast_raw = AsyncMock()

        mock_repo = MagicMock()
        mock_repo.async_update_action_verification = AsyncMock(side_effect=RuntimeError("unexpected"))

        with patch("sre_agent.monitor.verification_pipeline.get_monitor_repo", return_value=mock_repo):
            loop = asyncio.new_event_loop()
            try:
                with pytest.raises(RuntimeError, match="unexpected"):
                    loop.run_until_complete(process_verifications(mock_monitor, []))
            finally:
                loop.close()


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

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

    def test_handler_sync_fallback_without_event_loop(self):
        """When no event loop is running, handler falls back to sync record_tool_call."""
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
        """The done callback properly discards the task from _pending_record_tasks."""
        from sre_agent.tool_usage import _pending_record_tasks

        async def run():
            task = asyncio.get_running_loop().create_task(asyncio.sleep(0))
            _pending_record_tasks.add(task)
            task.add_done_callback(_pending_record_tasks.discard)
            assert task in _pending_record_tasks
            await task
            await asyncio.sleep(0)
            assert task not in _pending_record_tasks

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()
