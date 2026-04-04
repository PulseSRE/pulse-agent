"""Tests for PromQL recipe registry."""

from __future__ import annotations

import pytest

from sre_agent.promql_recipes import (
    RECIPES,
    PromQLRecipe,
    get_fallback,
    get_recipe,
    get_recipes_for_category,
)

VALID_SCOPES = {"cluster", "namespace", "pod", "node"}
VALID_CHART_TYPES = {
    "line",
    "area",
    "stacked_area",
    "bar",
    "stacked_bar",
    "metric_card",
    "status_list",
}
EXPECTED_CATEGORIES = {
    "cpu",
    "memory",
    "network",
    "storage",
    "control_plane",
    "pods",
    "alerts",
    "cluster_health",
    "ingress",
    "scheduler",
    "overcommit",
    "workload_state",
    "storage_state",
    "node_use",
    "monitoring",
    "operators",
}


class TestRecipeStructure:
    """Validate recipe data integrity."""

    def test_all_categories_exist(self) -> None:
        assert set(RECIPES.keys()) == EXPECTED_CATEGORIES

    def test_total_recipe_count(self) -> None:
        total = sum(len(v) for v in RECIPES.values())
        assert total >= 70, f"Expected >= 70 recipes, got {total}"

    @pytest.mark.parametrize("category", EXPECTED_CATEGORIES)
    def test_all_recipes_are_dataclass_instances(self, category: str) -> None:
        for recipe in RECIPES[category]:
            assert isinstance(recipe, PromQLRecipe)

    @pytest.mark.parametrize("category", EXPECTED_CATEGORIES)
    def test_required_fields(self, category: str) -> None:
        for recipe in RECIPES[category]:
            assert recipe.name, f"Missing name in {category}"
            assert recipe.query, f"Missing query in {category}"
            assert recipe.chart_type, f"Missing chart_type in {category}"
            assert recipe.metric, f"Missing metric in {category}"
            assert recipe.scope, f"Missing scope in {category}"
            assert recipe.description, f"Missing description for {recipe.name}"

    @pytest.mark.parametrize("category", EXPECTED_CATEGORIES)
    def test_valid_scope(self, category: str) -> None:
        for recipe in RECIPES[category]:
            assert recipe.scope in VALID_SCOPES, f"{recipe.name} has invalid scope: {recipe.scope}"

    @pytest.mark.parametrize("category", EXPECTED_CATEGORIES)
    def test_valid_chart_type(self, category: str) -> None:
        for recipe in RECIPES[category]:
            assert recipe.chart_type in VALID_CHART_TYPES, f"{recipe.name} has invalid chart_type: {recipe.chart_type}"

    def test_no_duplicate_queries(self) -> None:
        seen: dict[str, str] = {}
        for category, recipes in RECIPES.items():
            for recipe in recipes:
                key = recipe.query
                assert key not in seen, f"Duplicate query in {category}/{recipe.name} (first seen in {seen[key]})"
                seen[key] = f"{category}/{recipe.name}"


class TestLookupFunctions:
    """Validate lookup helpers."""

    def test_get_recipe_found(self) -> None:
        result = get_recipe("container_cpu_usage_seconds_total")
        assert result is not None
        assert result.metric == "container_cpu_usage_seconds_total"

    def test_get_recipe_not_found(self) -> None:
        assert get_recipe("totally_fake_metric") is None

    def test_get_recipes_for_category_cpu(self) -> None:
        recipes = get_recipes_for_category("cpu")
        assert len(recipes) >= 5

    def test_get_recipes_for_category_nonexistent(self) -> None:
        assert get_recipes_for_category("nonexistent") == []

    def test_get_fallback_cpu_cluster(self) -> None:
        result = get_fallback("cpu", "cluster")
        assert result is not None
        assert result.scope == "cluster"

    def test_get_fallback_memory_cluster(self) -> None:
        result = get_fallback("memory", "cluster")
        assert result is not None

    def test_get_fallback_nonexistent(self) -> None:
        assert get_fallback("nonexistent") is None
