"""Domain-specific repository classes for database access.

Each repository owns the SQL for its domain and returns typed results.
The ``db.py`` module retains thin wrappers for backward compatibility.
"""

from .analytics_repo import AnalyticsRepository, get_analytics_repo
from .base import BaseRepository
from .chat_history_repo import ChatHistoryRepository, get_chat_history_repo
from .context_bus_repo import ContextBusRepository, get_context_bus_repo
from .eval_repo import EvalRepository, get_eval_repo
from .inbox_repo import InboxRepository, get_inbox_repo
from .intelligence_repo import IntelligenceRepository, get_intelligence_repo
from .monitor_repo import MonitorRepository, get_monitor_repo
from .prompt_log_repo import PromptLogRepository, get_prompt_log_repo
from .promql_repo import PromQLRepository, get_promql_repo
from .selector_learning_repo import SelectorLearningRepository, get_selector_learning_repo
from .skill_analytics_repo import SkillAnalyticsRepository, get_skill_analytics_repo
from .slo_repo import SLORepository, get_slo_repo
from .tool_usage_repo import ToolUsageRepository, get_tool_usage_repo
from .view_repo import ViewRepository, get_view_repo

__all__ = [
    "AnalyticsRepository",
    "BaseRepository",
    "ChatHistoryRepository",
    "ContextBusRepository",
    "EvalRepository",
    "InboxRepository",
    "IntelligenceRepository",
    "MonitorRepository",
    "PromQLRepository",
    "PromptLogRepository",
    "SLORepository",
    "SelectorLearningRepository",
    "SkillAnalyticsRepository",
    "ToolUsageRepository",
    "ViewRepository",
    "get_analytics_repo",
    "get_chat_history_repo",
    "get_context_bus_repo",
    "get_eval_repo",
    "get_inbox_repo",
    "get_intelligence_repo",
    "get_monitor_repo",
    "get_prompt_log_repo",
    "get_promql_repo",
    "get_selector_learning_repo",
    "get_skill_analytics_repo",
    "get_slo_repo",
    "get_tool_usage_repo",
    "get_view_repo",
]
