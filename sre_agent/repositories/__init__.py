"""Domain-specific repository classes for database access.

Each repository owns the SQL for its domain and returns typed results.
The ``db.py`` module retains thin wrappers for backward compatibility.
"""

from .base import BaseRepository
from .inbox_repo import InboxRepository, get_inbox_repo
from .monitor_repo import MonitorRepository, get_monitor_repo
from .view_repo import ViewRepository, get_view_repo

__all__ = [
    "BaseRepository",
    "InboxRepository",
    "MonitorRepository",
    "ViewRepository",
    "get_inbox_repo",
    "get_monitor_repo",
    "get_view_repo",
]
