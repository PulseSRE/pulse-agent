"""Table/kind/query mutation actions."""

from __future__ import annotations

from .base import MutationResult, ViewMutation
from .registry import register_mutation


class UpdateColumns(ViewMutation):
    action = "update_columns"

    def validate(self, ctx):
        err = self._check_widget_index(ctx)
        if err:
            return err
        layout = self._get_layout(ctx)
        if layout[ctx.widget_index].get("kind") != "data_table":
            return f"Widget [{ctx.widget_index}] is not a data_table (it's a {layout[ctx.widget_index].get('kind')})."
        columns = ctx.params.get("columns", [])
        if not columns:
            return "Error: params_json must include 'columns' list."
        return None

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        widget = layout[ctx.widget_index]
        columns = ctx.params.get("columns", [])
        existing_cols = {c["id"]: c for c in widget.get("columns", [])}
        new_cols = [existing_cols[cid] for cid in columns if cid in existing_cols]
        if not new_cols:
            return MutationResult(False, f"No matching columns found. Available: {list(existing_cols.keys())}")
        widget["columns"] = new_cols
        if widget.get("rows"):
            col_ids = {c["id"] for c in new_cols}
            widget["rows"] = [
                {k: v for k, v in row.items() if k in col_ids or k.startswith("_")} for row in widget["rows"]
            ]
        self._save_layout(ctx, layout)
        return MutationResult(True, f"Updated columns on widget [{ctx.widget_index}] to {columns}.", ctx.view_id)


class SortBy(ViewMutation):
    action = "sort_by"

    def validate(self, ctx):
        err = self._check_widget_index(ctx)
        if err:
            return err
        layout = self._get_layout(ctx)
        if layout[ctx.widget_index].get("kind") != "data_table":
            return f"Widget [{ctx.widget_index}] is not a data_table."
        if not ctx.params.get("column"):
            return "Error: params_json must include 'column'."
        return None

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        widget = layout[ctx.widget_index]
        column = ctx.params["column"]
        direction = ctx.params.get("direction", "asc")
        rows = widget.get("rows", [])
        reverse = direction.lower() == "desc"
        try:
            rows.sort(key=lambda r: r.get(column, ""), reverse=reverse)
        except TypeError:
            pass
        widget["rows"] = rows
        widget["_sort"] = {"column": column, "direction": direction}
        self._save_layout(ctx, layout)
        return MutationResult(True, f"Sorted widget [{ctx.widget_index}] by {column} {direction}.", ctx.view_id)


class FilterBy(ViewMutation):
    action = "filter_by"

    def validate(self, ctx):
        err = self._check_widget_index(ctx)
        if err:
            return err
        layout = self._get_layout(ctx)
        if layout[ctx.widget_index].get("kind") != "data_table":
            return f"Widget [{ctx.widget_index}] is not a data_table."
        if not ctx.params.get("column"):
            return "Error: params_json must include 'column'."
        return None

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        widget = layout[ctx.widget_index]
        column = ctx.params["column"]
        operator = ctx.params.get("operator", "==")
        value = ctx.params.get("value", "")
        filters = widget.get("_filters", [])
        filters.append({"column": column, "operator": operator, "value": value})
        widget["_filters"] = filters
        self._save_layout(ctx, layout)
        return MutationResult(
            True, f"Added filter on widget [{ctx.widget_index}]: {column} {operator} {value}.", ctx.view_id
        )


class ChangeKind(ViewMutation):
    action = "change_kind"

    def validate(self, ctx):
        err = self._check_widget_index(ctx)
        if err:
            return err
        new_kind = ctx.params.get("new_kind", "")
        if not new_kind:
            return "Error: params_json must include 'new_kind'."
        from ..component_registry import get_valid_kinds

        if new_kind not in get_valid_kinds():
            return f"Invalid kind '{new_kind}'. Valid: {sorted(get_valid_kinds())}"
        return None

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        widget = layout[ctx.widget_index]
        old_kind = widget.get("kind", "unknown")
        new_kind = ctx.params["new_kind"]
        from ..component_transform import can_transform, transform

        if can_transform(old_kind, new_kind):
            layout[ctx.widget_index] = transform(widget, new_kind)
        else:
            widget["kind"] = new_kind
        self._save_layout(ctx, layout)
        return MutationResult(True, f"Changed widget [{ctx.widget_index}] from {old_kind} to {new_kind}.", ctx.view_id)


class UpdateQuery(ViewMutation):
    action = "update_query"

    def validate(self, ctx):
        err = self._check_widget_index(ctx)
        if err:
            return err
        if not ctx.params.get("query"):
            return "Error: params_json must include 'query'."
        return None

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        layout[ctx.widget_index]["query"] = ctx.params["query"]
        self._save_layout(ctx, layout)
        return MutationResult(True, f"Updated query on widget [{ctx.widget_index}].", ctx.view_id)


class SetRenderOverride(ViewMutation):
    action = "set_render_override"

    def validate(self, ctx):
        err = self._check_widget_index(ctx)
        if err:
            return err
        render_as = ctx.params.get("render_as", "")
        if not render_as:
            return "Error: params_json must include 'render_as'."
        from ..component_registry import get_valid_kinds

        if render_as not in get_valid_kinds():
            return f"Invalid render_as '{render_as}'. Valid: {sorted(get_valid_kinds())}"
        return None

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        widget = layout[ctx.widget_index]
        render_as = ctx.params["render_as"]
        widget["render_as"] = render_as
        widget["render_options"] = ctx.params.get("render_options", {})
        self._save_layout(ctx, layout)
        return MutationResult(True, f"Set render override on widget [{ctx.widget_index}] to {render_as}.", ctx.view_id)


register_mutation(UpdateColumns())
register_mutation(SortBy())
register_mutation(FilterBy())
register_mutation(ChangeKind())
register_mutation(UpdateQuery())
register_mutation(SetRenderOverride())
