"""View-level mutation actions (rename, update description)."""

from __future__ import annotations

from .. import db
from .base import MutationResult, ViewMutation
from .registry import register_mutation


class RenameView(ViewMutation):
    action = "rename"

    def validate(self, ctx):
        if not ctx.new_title:
            return "Error: new_title is required for rename action."
        return None

    def apply(self, ctx):
        db.update_view(ctx.view_id, ctx.owner, title=ctx.new_title)
        return MutationResult(True, f"Renamed view to '{ctx.new_title}'.", ctx.view_id)


class UpdateDescription(ViewMutation):
    action = "update_description"

    def validate(self, ctx):
        return None

    def apply(self, ctx):
        db.update_view(ctx.view_id, ctx.owner, description=ctx.new_description)
        return MutationResult(True, "Updated view description.", ctx.view_id)


register_mutation(RenameView())
register_mutation(UpdateDescription())
