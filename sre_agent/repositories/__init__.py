"""Domain-specific repository classes for database access.

Each repository owns the SQL for its domain and returns typed results.
The ``db.py`` module retains thin wrappers for backward compatibility.
"""

from .base import BaseRepository
from .chat_history_repo import ChatHistoryRepository, get_chat_history_repo
from .inbox_repo import InboxRepository, get_inbox_repo
from .intelligence_repo import IntelligenceRepository, get_intelligence_repo
from .monitor_repo import MonitorRepository, get_monitor_repo
from .prompt_log_repo import PromptLogRepository, get_prompt_log_repo
from .selector_learning_repo import SelectorLearningRepository, get_selector_learning_repo
from .tool_usage_repo import ToolUsageRepository, get_tool_usage_repo
from .view_repo import ViewRepository, get_view_repo

__all__ = [
    "BaseRepository",
    "ChatHistoryRepository",
    "InboxRepository",
    "IntelligenceRepository",
    "MonitorRepository",
    "PromptLogRepository",
    "SelectorLearningRepository",
    "ToolUsageRepository",
    "ViewRepository",
    "get_chat_history_repo",
    "get_inbox_repo",
    "get_intelligence_repo",
    "get_monitor_repo",
    "get_prompt_log_repo",
    "get_selector_learning_repo",
    "get_tool_usage_repo",
    "get_view_repo",
]
