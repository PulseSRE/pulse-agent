"""Shared execution-gate facts, independent of models and transport.

Consent and tool classification determine whether execution reaches the
configured namespace/node deny policy. They never override that deny policy.
Administrator-configured MCP servers may explicitly attest a read-only tool;
unknown annotations require confirmation. Investigation plans use the same
chat execution gate; durable approved phases additionally require server trust.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


def is_explicit_approval(value: object) -> bool:
    """Only boolean True grants execution authority, never truthy input."""
    return value is True


def effective_write_tools(configured: Iterable[str], registered: Iterable[str], offered: Iterable[str]) -> set[str]:
    """Stale caller configuration cannot demote a currently offered write."""
    return set(configured) | (set(registered) & set(offered))


def mcp_requires_confirmation(annotations: object) -> bool:
    """Unknown/mutating tools fail closed; explicit trusted reads stay usable."""
    if not isinstance(annotations, Mapping):
        return True
    return not (annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is not True)


def approved_phase_can_write(approved: object, server_trust: int) -> bool:
    """Durable phase consent is necessary alongside configured write trust."""
    return is_explicit_approval(approved) and server_trust >= 2


# --- Plan-only turns -------------------------------------------------------
#
# Confirmation is consent to a step someone asked for. When the person asked
# for a proposal ("diagnose the quota issue and propose a recovery plan"), a
# write in that turn was never requested, and a confirmation dialog for it
# invites a reflexive click. Under the release replay the model sometimes
# deleted pods on exactly that prompt and sometimes stopped at the plan; this
# makes "propose" mean propose whatever the model does with it.
#
# Deliberately narrow: only explicit requests for a plan, proposal,
# recommendation or options count, and any imperative to act anywhere in the
# message ("…and fix it", "roll it back", "go ahead") keeps writes available.
# "Propose a fix" is a request for a proposal: the action word is a noun there,
# so an action counts only at the start of a clause or after a request lead-in.

_PLAN_REQUEST = re.compile(
    r"\b(?:propos(?:e|al|ed)|recommend(?:ation)?s?|suggest(?:ion)?s?|(?:recovery|remediation|action|fix|rollback|"
    r"migration|upgrade|mitigation)\s+plan|(?:make|draft|write|give\s+me|outline|come\s+up\s+with)\s+(?:an?\s+|the\s+)?plan|"
    r"what\s+(?:should|would|could)\s+(?:i|we)\b|how\s+(?:should|would)\s+(?:i|we)\b|should\s+(?:i|we)\b|"
    r"(?:my|our|the)\s+options|what\s+are\s+(?:my|our|the)\s+options|dry[\s-]?run\s+only|don'?t\s+(?:change|touch|apply|make\s+changes))",
    re.IGNORECASE,
)

_ACTION_VERBS = (
    r"(?:fix|apply|delete|remove|restart|scale|roll\s*(?:it\s+)?back|rollback|drain|cordon|uncordon|patch|"
    r"clean\s*(?:it\s+)?up|resolve|remediate|evict|kill|execute|run|proceed|go\s+ahead|do\s+(?:it|that|this)|"
    r"implement|carry\s+(?:it\s+)?out|make\s+(?:the|that|this|those)\s+change)"
)
_ACTION_REQUEST = re.compile(
    r"(?:^|[.!?;:\n]\s*|,\s*(?:then\s+|and\s+)?|\b(?:and|then|please|now|also|just)\s+|"
    r"\b(?:can|could|would|will)\s+you\s+(?:please\s+)?|\b(?:i\s+want|we\s+want|i\s+need|we\s+need)\s+(?:you\s+)?to\s+)"
    + _ACTION_VERBS
    + r"\b",
    re.IGNORECASE,
)
_APPROVAL = re.compile(
    r"^\s*(?:yes|yep|ok(?:ay)?|sure|approved?|confirm(?:ed)?|lgtm|do\s+it|go\s+ahead)\b", re.IGNORECASE
)


def requests_plan_only(text: str) -> bool:
    """Whether the turn asks for a plan or advice and for no change at all."""
    if not text or _APPROVAL.search(text):
        return False
    return bool(_PLAN_REQUEST.search(text)) and not _ACTION_REQUEST.search(text)


# The UI prefixes chat messages with what the person is looking at
# (api/context.py): dashboard and widget titles, not words they wrote. A
# dashboard named "Upgrade plan" must not make "add a chart" a plan request.
_UI_CONTEXT = re.compile(r"\A\s*\[UI Context:.*?(?:\n\s*\n|\Z)", re.DOTALL)


def latest_user_text(messages: Iterable[Mapping]) -> str:
    """The person's latest message, without tool-result turns or the UI context header."""
    for message in reversed(list(messages)):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "text"
            )
        if text:
            return _UI_CONTEXT.sub("", text, count=1)
    return ""


PLAN_ONLY_REFUSAL = (
    "Not run: the user asked for a plan, not for changes, so writes are not offered for this turn. "
    "Include this step in the plan you present (what it changes, why, and how to undo it) and let the "
    "user approve it in their next message."
)
