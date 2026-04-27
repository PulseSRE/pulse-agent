"""Widget-level mutation actions."""

from __future__ import annotations

from .base import MutationResult, ViewMutation
from .registry import register_mutation


class RemoveWidget(ViewMutation):
    action = "remove_widget"

    def validate(self, ctx):
        return self._check_widget_index(ctx)

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        removed = layout[ctx.widget_index]
        removed_title = removed.get("title", removed.get("kind", "widget"))
        new_layout = [w for i, w in enumerate(layout) if i != ctx.widget_index]
        self._save_layout(ctx, new_layout)
        return MutationResult(
            True,
            f"Removed widget [{ctx.widget_index}]: {removed_title}. View now has {len(new_layout)} widgets.",
            ctx.view_id,
        )


class MoveWidget(ViewMutation):
    action = "move_widget"

    def validate(self, ctx):
        err = self._check_widget_index(ctx)
        if err:
            return err
        try:
            int(ctx.new_title)
        except (ValueError, TypeError):
            return "Error: provide target position as new_title (e.g. '0' for top)."
        return None

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        new_pos = max(0, min(int(ctx.new_title), len(layout) - 1))
        widget = layout.pop(ctx.widget_index)
        layout.insert(new_pos, widget)
        self._save_layout(ctx, layout)
        moved_title = widget.get("title", widget.get("kind", "widget"))
        return MutationResult(
            True,
            f"Moved widget '{moved_title}' from position {ctx.widget_index} to {new_pos}.",
            ctx.view_id,
        )


class RenameWidget(ViewMutation):
    action = "rename_widget"

    def validate(self, ctx):
        err = self._check_widget_index(ctx)
        if err:
            return err
        if not ctx.new_title:
            return "Error: new_title is required."
        return None

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        layout[ctx.widget_index]["title"] = ctx.new_title
        self._save_layout(ctx, layout)
        return MutationResult(True, f"Renamed widget [{ctx.widget_index}] to '{ctx.new_title}'.", ctx.view_id)


class UpdateWidgetDescription(ViewMutation):
    action = "update_widget_description"

    def validate(self, ctx):
        return self._check_widget_index(ctx)

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        layout[ctx.widget_index]["description"] = ctx.new_description
        self._save_layout(ctx, layout)
        return MutationResult(True, f"Updated widget [{ctx.widget_index}] description.", ctx.view_id)


class ChangeChartType(ViewMutation):
    action = "change_chart_type"

    def validate(self, ctx):
        err = self._check_widget_index(ctx)
        if err:
            return err
        layout = self._get_layout(ctx)
        widget = layout[ctx.widget_index]
        if widget.get("kind") != "chart":
            return f"Widget [{ctx.widget_index}] is not a chart (it's a {widget.get('kind')})."
        chart_type = ctx.new_title
        if chart_type not in ("line", "bar", "area"):
            return f"Invalid chart type '{chart_type}'. Use: line, bar, area."
        return None

    def apply(self, ctx):
        layout = self._get_layout(ctx)
        layout[ctx.widget_index]["chartType"] = ctx.new_title
        self._save_layout(ctx, layout)
        return MutationResult(True, f"Changed widget [{ctx.widget_index}] to {ctx.new_title} chart.", ctx.view_id)


register_mutation(RemoveWidget())
register_mutation(MoveWidget())
register_mutation(RenameWidget())
register_mutation(UpdateWidgetDescription())
register_mutation(ChangeChartType())
