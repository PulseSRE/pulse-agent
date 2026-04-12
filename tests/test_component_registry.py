"""Tests for component registry."""

from __future__ import annotations

from sre_agent.component_registry import (
    COMPONENT_REGISTRY,
    ComponentKind,
    generate_prompt_hints,
    get_component,
    get_components_by_category,
    get_valid_kinds,
    register_component,
)


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
        }
        actual = get_valid_kinds()
        missing = expected - actual
        assert not missing, f"Missing from registry: {missing}"

    def test_get_valid_kinds_returns_frozenset(self):
        kinds = get_valid_kinds()
        assert isinstance(kinds, frozenset)
        assert len(kinds) >= 19

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

    def test_mutation_support(self):
        table = get_component("data_table")
        assert "update_columns" in table.supports_mutations
        assert "sort_by" in table.supports_mutations

        chart = get_component("chart")
        assert "change_chart_type" in chart.supports_mutations


class TestPromptHints:
    def test_generates_hints(self):
        hints = generate_prompt_hints()
        assert len(hints) > 0
        assert "data_table" in hints
        assert "metric_card" in hints

    def test_filter_by_category(self):
        hints = generate_prompt_hints(categories=["metrics"])
        assert "metric_card" in hints
        assert "data_table" not in hints

    def test_empty_category(self):
        hints = generate_prompt_hints(categories=["nonexistent"])
        assert hints == ""


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
