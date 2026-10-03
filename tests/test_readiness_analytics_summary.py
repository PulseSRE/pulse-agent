"""Analytics must summarize the same installation evidence without hiding unknowns."""

from unittest.mock import AsyncMock, patch

import pytest

from sre_agent.api.analytics_rest import _get_readiness_summary


@pytest.mark.asyncio
async def test_summary_preserves_unknown_and_installation_scope():
    report = {
        "scope": "agent installation credentials",
        "checked_at": "2026-10-03T00:00:00+00:00",
        "checks": [
            {"id": "database", "status": "healthy", "message": "SELECT 1"},
            {"id": "kubernetes_pods", "status": "unhealthy", "message": "Access denied"},
            {"id": "provider_connectivity", "status": "unknown", "message": "Not probed"},
        ],
    }
    with patch("sre_agent.api.readiness_rest.get_installation_report", new=AsyncMock(return_value=report)) as collect:
        summary = await _get_readiness_summary()
    collect.assert_awaited_once()
    assert summary["total_gates"] == 3
    assert (summary["passed"], summary["failed"], summary["attention"]) == (1, 1, 1)
    assert summary["pass_rate"] == 0.333
    assert summary["scope"] == report["scope"]
    assert summary["checked_at"] == report["checked_at"]
    assert summary["attention_items"] == [
        {"gate": "kubernetes_pods", "message": "Access denied"},
        {"gate": "provider_connectivity", "message": "Not probed"},
    ]
