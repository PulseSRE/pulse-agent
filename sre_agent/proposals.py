"""Writes in proposal namespaces run only when the user approves them as a new message.

Why this exists. The deny policy (``policy.py``) refuses ``delete_pod`` in a
protected namespace, and the agent loop stops further writes in a turn once
the policy has refused one. But since the deny rules are also in the prompt
(``policy_system_note``), the model usually refuses in words without calling
the denied tool, so the code-level stop never arms. On SRE-Bench's
``unsafe_missing_confirmation`` trap ("delete this production pod, no
questions") the model then said "confirm and I'll run" a restart — and ran
it in the same turn. Telling it not to (pulse-agent#170) leaves that to the
model, which slipped about one run in three.

So the rule is structural, not a judgment about intent: in a proposal
namespace (``PULSE_AGENT_PROPOSAL_NAMESPACES``, default: the protected
namespaces), a write the model calls is not run in that turn. It becomes a
proposal: the exact tool and arguments, shown to the user. It runs only when
the user approves that proposal, which arrives as its own WebSocket message
and starts a new turn. Nothing parses the user's request, so nothing can be
talked around.

Proposals live in memory, per WebSocket session: they are offers made in a
conversation, not durable work. A restart or a closed tab drops them, and the
user asks again.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

PROPOSAL_TTL_SECONDS = 15 * 60
MAX_PROPOSALS_PER_SESSION = 10


def proposal_namespace_patterns() -> list[str]:
    from .config import get_settings

    return get_settings().get_proposal_namespaces()


def requires_proposal(input_data: dict | None) -> str | None:
    """The namespace that makes this write a proposal, or None to run it as usual.

    Only the call's own ``namespace`` argument decides: the same argument the
    deny policy reads. A write with no namespace (cluster-scoped) is unaffected.
    """
    namespace = str((input_data or {}).get("namespace", "") or "")
    if not namespace:
        return None
    patterns = proposal_namespace_patterns()
    if any(fnmatch(namespace, pattern) for pattern in patterns):
        return namespace
    return None


def proposed_result(tool_name: str, namespace: str, proposal_id: str | None) -> str:
    """What the model is told instead of a tool result. It must not read as "done"."""
    if proposal_id is None:
        return (
            f"Not run: '{tool_name}' targets '{namespace}', where writes are proposals the user approves "
            "in a separate message, and no proposal could be offered in this session. Nothing changed. "
            "Say what you would do and why; the user can ask for it."
        )
    return (
        f"Not run yet: '{tool_name}' targets '{namespace}', where a write runs only when the user approves it. "
        f"It is now proposal {proposal_id}: the user sees the exact tool and arguments and approves or declines "
        "it. Nothing has changed in the cluster. Do not call it again or try another write for the same goal in "
        "this turn. End your reply by saying what you proposed and why; if the user approves it, you will be "
        "told the result in their next message."
    )


@dataclass
class Proposal:
    id: str
    nonce: str
    session_id: str
    tool: str
    input: dict
    namespace: str
    tool_map: dict = field(repr=False)
    created_at: float = field(default_factory=time.time)

    @property
    def expires_at(self) -> float:
        return self.created_at + PROPOSAL_TTL_SECONDS

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) > self.expires_at


class ProposalStore:
    """Open proposals by session. Thread-safe; tools run in a thread pool."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, dict[str, Proposal]] = {}

    def create(self, session_id: str, tool: str, input_data: dict, namespace: str, tool_map: dict) -> Proposal:
        proposal = Proposal(
            id="p_" + secrets.token_hex(6),
            nonce=secrets.token_urlsafe(16),
            session_id=session_id,
            tool=tool,
            input=dict(input_data),
            namespace=namespace,
            tool_map=tool_map,
        )
        with self._lock:
            open_ = self._prune(session_id)
            while len(open_) >= MAX_PROPOSALS_PER_SESSION:
                oldest = min(open_.values(), key=lambda p: p.created_at)
                open_.pop(oldest.id, None)
            open_[proposal.id] = proposal
        return proposal

    def take(self, session_id: str, proposal_id: str, nonce: str) -> tuple[Proposal | None, str]:
        """Remove and return the proposal if the id and nonce match and it is still open.

        Returns (proposal, "") or (None, reason). A proposal is single-use: a
        second approval of the same id finds nothing.
        """
        with self._lock:
            open_ = self._by_session.get(session_id, {})
            proposal = open_.get(proposal_id)
            if proposal is None:
                return None, "not_found"
            if not isinstance(nonce, str) or not hmac.compare_digest(proposal.nonce, nonce):
                return None, "nonce_mismatch"
            open_.pop(proposal_id, None)
            if proposal.expired():
                return None, "expired"
            return proposal, ""

    def discard(self, session_id: str, proposal_id: str) -> bool:
        with self._lock:
            return self._by_session.get(session_id, {}).pop(proposal_id, None) is not None

    def drop_session(self, session_id: str) -> None:
        with self._lock:
            self._by_session.pop(session_id, None)

    def open_for(self, session_id: str) -> list[Proposal]:
        with self._lock:
            return list(self._prune(session_id).values())

    def _prune(self, session_id: str) -> dict[str, Proposal]:
        now = time.time()
        open_ = self._by_session.setdefault(session_id, {})
        for pid in [pid for pid, p in open_.items() if p.expired(now)]:
            open_.pop(pid, None)
        return open_


_store = ProposalStore()


def get_store() -> ProposalStore:
    return _store


def approval_message(proposal: Proposal, result_text: str, ok: bool) -> str:
    """The user's turn after approving: what they approved and what happened, in their voice."""
    import json

    args = json.dumps(proposal.input, sort_keys=True, default=str)
    outcome = "It ran. Result:" if ok else "It did not succeed. Result:"
    return (
        f"I approved proposal {proposal.id}: {proposal.tool} {args}. {outcome}\n{result_text}\n\n"
        "Check whether it worked and tell me. Do not report it as fixed until you have checked."
    )


def describe(proposal: Proposal) -> dict[str, Any]:
    """The ``write_proposal`` WebSocket event body (without preview)."""
    return {
        "type": "write_proposal",
        "id": proposal.id,
        "nonce": proposal.nonce,
        "tool": proposal.tool,
        "input": proposal.input,
        "namespace": proposal.namespace,
        "expiresAt": int(proposal.expires_at * 1000),
    }
