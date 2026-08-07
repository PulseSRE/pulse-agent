"""Tests for component registry."""

from __future__ import annotations

from sre_agent.component_registry import (
    COMPONENT_REGISTRY,
    ComponentKind,
    get_component,
    get_components_by_category,
    get_prompt_hints,
    get_valid_kinds,
    register_component,
)
from sre_agent.quality_engine import normalize_component_spec, normalize_layout, validate_layout_kinds


class TestRegistry:
    def test_has_all_existing_kinds(self):
        """Registry must include all previously hardcoded VALID_KINDS."""
        expected = {
            "metric_card",
            "chart",
            "data_table",
            "info_card_grid",
            "status_list",
            "badge_list",
            "key_value",
            "relationship_tree",
            "log_viewer",
            "yaml_viewer",
            "node_map",
            "tabs",
            "grid",
            "section",
            "bar_list",
            "progress_list",
            "stat_card",
            "timeline",
            "resource_counts",
            "confidence_badge",
            "resolution_tracker",
            "blast_radius",
            "action_button",
        }
        actual = get_valid_kinds()
        missing = expected - actual
        assert not missing, f"Missing from registry: {missing}"

    def test_get_valid_kinds_returns_frozenset(self):
        kinds = get_valid_kinds()
        assert isinstance(kinds, frozenset)
        assert len(kinds) >= 23

    def test_get_component(self):
        c = get_component("data_table")
        assert c is not None
        assert c.name == "data_table"
        assert c.category == "data"

    def test_get_component_unknown(self):
        assert get_component("nonexistent_kind_xyz") is None

    def test_all_components_have_required_fields(self):
        for name, c in COMPONENT_REGISTRY.items():
            assert c.name == name
            assert c.description, f"{name} missing description"
            assert c.category, f"{name} missing category"
            assert c.prompt_hint, f"{name} missing prompt_hint"

    def test_all_components_have_examples(self):
        for name, c in COMPONENT_REGISTRY.items():
            assert c.example, f"{name} missing example"
            assert c.example.get("kind") == name, f"{name} example has wrong kind"

    def test_categories(self):
        categories = {c.category for c in COMPONENT_REGISTRY.values()}
        assert "metrics" in categories
        assert "data" in categories
        assert "visualization" in categories
        assert "layout" in categories

    def test_get_components_by_category(self):
        metrics = get_components_by_category("metrics")
        assert len(metrics) >= 3
        names = {c.name for c in metrics}
        assert "metric_card" in names

    def test_containers_flagged(self):
        containers = [c for c in COMPONENT_REGISTRY.values() if c.is_container]
        names = {c.name for c in containers}
        assert "tabs" in names
        assert "grid" in names
        assert "section" in names
        assert "data_table" not in names

    def test_confidence_badge_schema(self):
        c = get_component("confidence_badge")
        assert c is not None
        assert c.category == "status"
        assert "score" in c.required_fields
        assert c.title_required is False

    def test_resolution_tracker_schema(self):
        c = get_component("resolution_tracker")
        assert c is not None
        assert c.category == "status"
        assert "steps" in c.required_fields
        assert c.title_required is False

    def test_blast_radius_schema(self):
        c = get_component("blast_radius")
        assert c is not None
        assert c.category == "status"
        assert "items" in c.required_fields
        assert c.title_required is True

    def test_action_button_schema(self):
        c = get_component("action_button")
        assert c is not None
        assert c.category == "action"
        assert "label" in c.required_fields
        assert "action" in c.required_fields
        assert "action_input" in c.required_fields
        assert c.title_required is False

    def test_mutation_support(self):
        table = get_component("data_table")
        assert "update_columns" in table.supports_mutations
        assert "sort_by" in table.supports_mutations

        chart = get_component("chart")
        assert "change_chart_type" in chart.supports_mutations


class TestPromptHints:
    def test_generates_hints(self):
        hints = get_prompt_hints()
        assert len(hints) > 0
        assert "data_table" in hints
        assert "metric_card" in hints

    def test_filter_by_kinds(self):
        hints = get_prompt_hints(kinds=["metric_card"])
        assert "metric_card" in hints
        assert "data_table" not in hints

    def test_empty_kinds(self):
        hints = get_prompt_hints(kinds=["nonexistent_xyz"])
        assert hints == ""


class TestNormalization:
    def test_status_list_label_to_name(self):
        spec = {"kind": "status_list", "items": [{"label": "Alert", "status": "warning"}]}
        normalize_component_spec(spec)
        assert spec["items"][0]["name"] == "Alert"
        assert "label" not in spec["items"][0]

    def test_status_list_preserves_name(self):
        spec = {"kind": "status_list", "items": [{"name": "Alert", "status": "warning"}]}
        normalize_component_spec(spec)
        assert spec["items"][0]["name"] == "Alert"

    def test_status_list_info_to_unknown(self):
        spec = {"kind": "status_list", "items": [{"name": "x", "status": "info"}]}
        normalize_component_spec(spec)
        assert spec["items"][0]["status"] == "unknown"

    def test_badge_list_label_to_text(self):
        spec = {"kind": "badge_list", "badges": [{"label": "v1", "variant": "info"}]}
        normalize_component_spec(spec)
        assert spec["badges"][0]["text"] == "v1"
        assert "label" not in spec["badges"][0]

    def test_log_viewer_warning_to_warn(self):
        spec = {"kind": "log_viewer", "lines": [{"message": "oops", "level": "warning"}]}
        normalize_component_spec(spec)
        assert spec["lines"][0]["level"] == "warn"

    def test_chart_values_to_data(self):
        spec = {"kind": "chart", "series": [{"label": "cpu", "values": [[1, 2]]}]}
        normalize_component_spec(spec)
        assert spec["series"][0]["data"] == [[1, 2]]
        assert "values" not in spec["series"][0]

    def test_yaml_viewer_yaml_to_content(self):
        spec = {"kind": "yaml_viewer", "yaml": "key: val"}
        normalize_component_spec(spec)
        assert spec["content"] == "key: val"
        assert "yaml" not in spec

    def test_stat_card_label_to_title(self):
        spec = {"kind": "stat_card", "label": "Errors", "value": "3"}
        normalize_component_spec(spec)
        assert spec["title"] == "Errors"

    def test_info_card_grid_title_to_label(self):
        spec = {"kind": "info_card_grid", "cards": [{"title": "Nodes", "text": "5"}]}
        normalize_component_spec(spec)
        assert spec["cards"][0]["label"] == "Nodes"
        assert spec["cards"][0]["value"] == "5"

    def test_props_flattened(self):
        spec = {"kind": "metric_card", "title": "CPU", "props": {"value": "72%", "status": "warning"}}
        normalize_component_spec(spec)
        assert spec["value"] == "72%"
        assert spec["status"] == "warning"
        assert "props" not in spec

    def test_blast_radius_info_to_healthy(self):
        spec = {"kind": "blast_radius", "items": [{"kind_abbrev": "Svc", "name": "api", "status": "info"}]}
        normalize_component_spec(spec)
        assert spec["items"][0]["status"] == "healthy"

    def test_normalize_layout_recurses_into_grid(self):
        layout = [{"kind": "grid", "items": [{"kind": "status_list", "items": [{"label": "A", "status": "info"}]}]}]
        normalize_layout(layout)
        assert layout[0]["items"][0]["items"][0]["name"] == "A"
        assert layout[0]["items"][0]["items"][0]["status"] == "unknown"


class TestValidateLayoutKinds:
    def test_valid_kinds(self):
        layout = [{"kind": "metric_card"}, {"kind": "chart"}]
        assert validate_layout_kinds(layout) == []

    def test_invalid_kind(self):
        layout = [{"kind": "metric_card"}, {"kind": "bogus_widget"}]
        errors = validate_layout_kinds(layout)
        assert len(errors) == 1
        assert "bogus_widget" in errors[0]

    def test_missing_kind(self):
        layout = [{"title": "oops"}]
        errors = validate_layout_kinds(layout)
        assert len(errors) == 1
        assert "missing" in errors[0].lower()


class TestFrontendContract:
    """Verify the backend registry matches what the frontend expects."""

    _FRONTEND_KNOWN_KINDS: frozenset[str] = frozenset(
        {
            "data_table",
            "info_card_grid",
            "badge_list",
            "status_list",
            "key_value",
            "chart",
            "tabs",
            "grid",
            "section",
            "relationship_tree",
            "log_viewer",
            "yaml_viewer",
            "metric_card",
            "node_map",
            "bar_list",
            "progress_list",
            "stat_card",
            "timeline",
            "resource_counts",
            "topology",
            "action_button",
            "confidence_badge",
            "resolution_tracker",
            "blast_radius",
            "status_pipeline",
        }
    )

    def test_backend_covers_frontend_kinds(self):
        backend_kinds = get_valid_kinds()
        missing = self._FRONTEND_KNOWN_KINDS - backend_kinds
        assert not missing, f"Frontend expects kinds not in backend registry: {missing}"

    def test_frontend_covers_backend_kinds(self):
        backend_kinds = get_valid_kinds()
        extra = backend_kinds - self._FRONTEND_KNOWN_KINDS
        assert not extra, f"Backend has kinds not in frontend getKnownKinds(): {extra}"

    def test_all_examples_use_correct_field_names(self):
        """Verify registry examples use the canonical field names (not aliases)."""
        for name, comp in COMPONENT_REGISTRY.items():
            ex = comp.example
            if name == "status_list":
                for item in ex.get("items", []):
                    assert "name" in item, "status_list example uses 'label' instead of 'name'"
                    assert "label" not in item
            elif name == "badge_list":
                for badge in ex.get("badges", []):
                    assert "text" in badge, "badge_list example uses 'label' instead of 'text'"
                    assert "label" not in badge
            elif name == "chart":
                for s in ex.get("series", []):
                    assert "values" not in s, "chart example uses 'values' instead of 'data'"


class TestRegisterComponent:
    def test_register_custom(self):
        custom = ComponentKind(
            name="_test_custom",
            description="Test component",
            category="test",
            required_fields=["value"],
            example={"kind": "_test_custom", "value": 42},
            prompt_hint="_test_custom — Test.",
        )
        register_component(custom)
        assert get_component("_test_custom") is not None
        # Cleanup
        del COMPONENT_REGISTRY["_test_custom"]
