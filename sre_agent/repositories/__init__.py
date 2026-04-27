"""Domain-specific repository classes for database access.

Each repository owns the SQL for its domain and returns typed results.
The ``db.py`` module retains thin wrappers for backward compatibility.
"""

from .base import BaseRepository
from .view_repo import ViewRepository, get_view_repo

__all__ = ["BaseRepository", "ViewRepository", "get_view_repo"]
