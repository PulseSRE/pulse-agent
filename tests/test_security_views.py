"""Security regression tests for view access control.

Tests that verify the IDOR (Insecure Direct Object Reference) vulnerability
in view tools has been fixed. These tests ensure user B cannot read, modify,
or delete user A's views via ownerless database fallback queries.
"""

from unittest.mock import MagicMock, patch

from sre_agent.view_mutations import (
    optimize_view,
    remove_widget_from_view,
    undo_view_change,
    update_view_widgets,
)
from sre_agent.view_tools import delete_dashboard, get_view_details, list_saved_views


class TestViewOwnershipBypass:
    """Verify user B cannot read/modify/delete user A's views via IDOR.

    These tests verify that the CURRENT CODE (before the fix) has the vulnerability.
    After the fix, these tests should PASS, meaning the vulnerability is closed.
    """

    def test_get_view_details_rejects_other_users_view(self):
        """User B should not be able to read user A's view via ownerless fallback.

        BEFORE FIX: db.get_view is called TWICE - once with owner, once without.
                    The second call returns user-A's view, exposing it to user-B.
        AFTER FIX: db.get_view is called ONCE with owner='user-B', returns None, function returns 'not found'.
        """
        with patch("sre_agent.view_tools.get_current_user", return_value="user-B"):
            with patch("sre_agent.db.get_view") as mock_get_view:
                # Sequence: first call returns None, second call returns user-A's view
                mock_get_view.side_effect = [
                    None,  # db.get_view("cv-123", "user-B")
                    {
                        "id": "cv-123",
                        "owner": "user-A",
                        "title": "Secret Dashboard",
                        "layout": [],
                    },  # db.get_view("cv-123") - IDOR!
                ]

                result = get_view_details("cv-123")

                # BEFORE FIX: returns "View: Secret Dashboard" (user-A's view leaked!)
                # AFTER FIX: returns "View 'cv-123' not found."
                assert "not found" in result.lower(), f"IDOR vulnerability! User B accessed user A's view: {result}"

    def test_list_views_does_not_leak_other_users(self):
        """User B should only see their own views, not all views via fallback query."""
        with (
            patch("sre_agent.view_tools.get_current_user", return_value="user-B"),
            patch("sre_agent.db.list_views", return_value=[]),  # User-scoped query returns empty
            patch("sre_agent.db.get_database") as mock_get_db,
            patch("sre_agent.repositories.view_repo._deserialize_view_row") as mock_deserialize,
        ):
            # Mock database for ownerless fallback attempt
            mock_db_instance = MagicMock()
            mock_db_instance.fetchall.return_value = [
                {
                    "id": "cv-user-a",
                    "owner": "user-A",
                    "title": "User A Dashboard",
                    "description": "",
                    "icon": "",
                    "layout": "[]",
                    "positions": "{}",
                    "created_at": "2024-01-01",
                    "updated_at": "2024-01-01",
                }
            ]
            mock_get_db.return_value = mock_db_instance
            mock_deserialize.return_value = {
                "id": "cv-user-a",
                "owner": "user-A",
                "title": "User A Dashboard",
                "layout": [],
            }

            result = list_saved_views()

            # BEFORE FIX: returns user-A's dashboard in the list
            # AFTER FIX: returns "No saved views found"
            assert "User A Dashboard" not in result, f"IDOR vulnerability! Leaked other user's view: {result}"

    def test_update_view_widgets_rejects_other_users_view(self):
        """User B should not be able to update user A's view via ownerless fallback."""
        with patch("sre_agent.view_mutations.get_current_user", return_value="user-B"):
            with patch("sre_agent.db.get_view") as mock_get_view:
                with patch("sre_agent.db.update_view") as mock_update:
                    mock_get_view.side_effect = [
                        None,  # db.get_view("cv-123", "user-B")
                        {  # db.get_view("cv-123") - IDOR!
                            "id": "cv-123",
                            "owner": "user-A",
                            "title": "User A Dashboard",
                            "layout": [{"kind": "chart", "title": "Widget"}],
                        },
                    ]

                    result = update_view_widgets("cv-123", action="rename", new_title="Hacked")

                    # BEFORE FIX: function proceeds and updates user-A's view (identity drift too!)
                    # AFTER FIX: returns "View 'cv-123' not found."
                    assert "not found" in result.lower(), f"IDOR vulnerability! User B modified user A's view: {result}"
                    assert not mock_update.called, "User B should not be able to update user A's view"

    def test_remove_widget_rejects_other_users_view(self):
        """User B should not be able to remove widgets from user A's view."""
        with patch("sre_agent.view_mutations.get_current_user", return_value="user-B"):
            with patch("sre_agent.db.get_view") as mock_get_view:
                with patch("sre_agent.db.update_view") as mock_update:
                    mock_get_view.side_effect = [
                        None,
                        {
                            "id": "cv-123",
                            "owner": "user-A",
                            "title": "User A Dashboard",
                            "layout": [{"kind": "chart", "title": "Widget"}],
                        },
                    ]

                    result = remove_widget_from_view("cv-123", "Widget")
                    assert "not found" in result.lower(), f"IDOR vulnerability! {result}"
                    assert not mock_update.called

    def test_undo_view_change_rejects_other_users_view(self):
        """User B should not be able to undo changes to user A's view."""
        with patch("sre_agent.view_mutations.get_current_user", return_value="user-B"):
            with patch("sre_agent.db.list_view_versions", return_value=[{"version": 1, "action": "create"}]):
                with patch("sre_agent.db.restore_view_version", return_value=False):
                    with patch("sre_agent.db.get_view") as mock_get_view:
                        mock_get_view.side_effect = [
                            None,
                            {"id": "cv-123", "owner": "user-A", "title": "User A Dashboard", "layout": []},
                        ]

                        result = undo_view_change("cv-123", version=1)
                        assert "not found" in result.lower() or "could not restore" in result.lower(), (
                            f"IDOR vulnerability! {result}"
                        )

    def test_delete_dashboard_rejects_other_users_view(self):
        """User B should not be able to delete user A's view."""
        with (
            patch("sre_agent.view_tools.get_current_user", return_value="user-B"),
            patch("sre_agent.db.delete_view", return_value=False),
            patch("sre_agent.db.get_view") as mock_get_view,
        ):
            mock_get_view.return_value = None  # Ownerless fallback should not happen after fix

            result = delete_dashboard("cv-123")
            assert "not found" in result.lower() or "don't have permission" in result.lower(), (
                f"IDOR vulnerability! {result}"
            )

    def test_optimize_view_rejects_other_users_view(self):
        """User B should not be able to optimize user A's view."""
        with patch("sre_agent.view_mutations.get_current_user", return_value="user-B"):
            with patch("sre_agent.db.get_view") as mock_get_view:
                with patch("sre_agent.db.update_view") as mock_update:
                    mock_get_view.side_effect = [
                        None,
                        {
                            "id": "cv-123",
                            "owner": "user-A",
                            "title": "User A Dashboard",
                            "layout": [{"kind": "chart", "title": "Widget"}],
                        },
                    ]

                    result = optimize_view("cv-123", strategy="group")
                    assert "not found" in result.lower(), f"IDOR vulnerability! {result}"
                    assert not mock_update.called


class TestReDoSProtection:
    """Verify regex pattern validation prevents ReDoS attacks."""

    def test_rejects_long_pattern(self):
        from sre_agent.api.views import _validate_regex_pattern

        assert _validate_regex_pattern("a" * 101) is not None

    def test_rejects_nested_quantifiers(self):
        from sre_agent.api.views import _validate_regex_pattern

        assert _validate_regex_pattern("(a+)+$") is not None

    def test_allows_normal_pattern(self):
        from sre_agent.api.views import _validate_regex_pattern

        assert _validate_regex_pattern("error|Error|ERROR") is None
