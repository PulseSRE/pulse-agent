"""Alertmanager silences: matcher parsing, the safety rules, and confirm previews.

The tools in :mod:`sre_agent.silence_tools` and the confirmation preview the
chat shows before one runs both go through here, so what a person approves is
checked by the same rules the write enforces.

Safety rules (``validate_matchers``):

- At least one matcher, at most 20, label names in Prometheus syntax, regexes
  that compile.
- At least one *positive* matcher (``=`` or ``=~``) that is narrow: an ``=``
  with a non-empty value, or an ``=~`` that matches neither the empty string
  nor arbitrary probe strings. A silence of only negative matchers, or of
  ``=~".*"``/``=~".+"``, would mute every alert on the cluster; it is refused,
  not confirmed.
- A silence whose ``alertname`` matcher matches ``Watchdog`` is refused:
  Watchdog is the dead man's switch that tells an external pager Alertmanager
  is alive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_MATCHERS = 20
MAX_VALUE_LEN = 1024
MAX_COMMENT_LEN = 1000
MIN_DURATION_MINUTES = 5
MAX_DURATION_MINUTES = 7 * 24 * 60

_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_MATCHER_RE = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*')
_SILENCE_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Strings no narrow matcher should accept. A regex that matches all of them
# matches (for practical purposes) every value of its label.
_PROBES = ("", "x", "pulse-probe-7f3a9c", "Z9_q.r/s:t")


class SilenceInputError(ValueError):
    """The requested silence is not allowed; the message says why."""


@dataclass(frozen=True)
class Matcher:
    name: str
    value: str
    is_regex: bool = False
    is_equal: bool = True

    @property
    def op(self) -> str:
        if self.is_regex:
            return "=~" if self.is_equal else "!~"
        return "=" if self.is_equal else "!="

    def __str__(self) -> str:
        escaped = self.value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{self.name}{self.op}"{escaped}"'

    def to_api(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "isRegex": self.is_regex, "isEqual": self.is_equal}

    def matches_value(self, value: str) -> bool:
        if self.is_regex:
            try:
                hit = re.fullmatch(self.value, value) is not None
            except re.error:
                return False
        else:
            hit = value == self.value
        return hit if self.is_equal else not hit


def parse_matchers(text: str) -> list[Matcher]:
    """Parse ``name="v", name=~"re", name!="v", name!~"re"`` (braces optional)."""
    s = (text or "").strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    matchers: list[Matcher] = []
    pos = 0
    while pos < len(s):
        m = _MATCHER_RE.match(s, pos)
        if not m:
            raise SilenceInputError(
                f'Could not read the matchers at "{s[pos : pos + 40]}". Use name="value", name!="value", '
                'name=~"regex" or name!~"regex", separated by commas.'
            )
        name, op, raw = m.group(1), m.group(2), m.group(3)
        value = re.sub(r"\\(.)", r"\1", raw)
        matchers.append(Matcher(name=name, value=value, is_regex="~" in op, is_equal=not op.startswith("!")))
        pos = m.end()
        if pos < len(s):
            if s[pos] != ",":
                raise SilenceInputError(f'Expected a comma after {name}{op}"{raw}"')
            pos += 1
    return matchers


def matchers_from_api(raw: list[dict]) -> list[Matcher]:
    return [
        Matcher(
            name=str(m.get("name", "")),
            value=str(m.get("value", "")),
            is_regex=bool(m.get("isRegex", False)),
            # Alertmanager treats a missing isEqual as true.
            is_equal=m.get("isEqual", True) is not False,
        )
        for m in raw or []
    ]


def _is_narrow(m: Matcher) -> bool:
    if not m.is_equal:
        return False
    if not m.is_regex:
        return m.value != ""
    try:
        compiled = re.compile(m.value)
    except re.error:
        return False
    if compiled.fullmatch("") is not None:
        return False
    return not all(compiled.fullmatch(p) is not None for p in _PROBES if p)


def validate_matchers(matchers: list[Matcher]) -> None:
    if not matchers:
        raise SilenceInputError("A silence needs at least one matcher; an empty set would mute every alert.")
    if len(matchers) > MAX_MATCHERS:
        raise SilenceInputError(f"At most {MAX_MATCHERS} matchers per silence.")
    for m in matchers:
        if not _LABEL_NAME_RE.match(m.name):
            raise SilenceInputError(f"{m.name!r} is not a valid label name.")
        if len(m.value) > MAX_VALUE_LEN:
            raise SilenceInputError(f"The value for {m.name} is longer than {MAX_VALUE_LEN} characters.")
        if m.is_regex:
            try:
                re.compile(m.value)
            except re.error as e:
                raise SilenceInputError(f"The regex for {m.name} does not compile: {e}") from None
    if not any(_is_narrow(m) for m in matchers):
        raise SilenceInputError(
            "These matchers would match every alert: at least one must be a positive matcher (= or =~) that "
            'names something specific, such as alertname="KubeNodeUnreachable" or node="worker-5".'
        )
    for m in matchers:
        if m.name == "alertname" and m.is_equal and m.matches_value("Watchdog"):
            raise SilenceInputError(
                "Watchdog cannot be silenced: it is the always-firing alert that tells the external pager "
                "Alertmanager is alive."
            )


def validate_duration(minutes: Any) -> int:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        raise SilenceInputError("duration_minutes must be a whole number of minutes.") from None
    if value < MIN_DURATION_MINUTES or value > MAX_DURATION_MINUTES:
        raise SilenceInputError(
            f"A silence lasts between {MIN_DURATION_MINUTES} minutes and 7 days ({MAX_DURATION_MINUTES} minutes); "
            f"{value} is outside that."
        )
    return value


def validate_comment(comment: Any) -> str:
    text = str(comment or "").strip()
    if not text:
        raise SilenceInputError(
            "A silence needs a comment saying why; it is shown on the silence and in the audit log."
        )
    if len(text) > MAX_COMMENT_LEN:
        raise SilenceInputError(f"The comment is longer than {MAX_COMMENT_LEN} characters.")
    return text


def validate_silence_id(silence_id: Any) -> str:
    sid = str(silence_id or "").strip()
    if not _SILENCE_ID_RE.match(sid):
        raise SilenceInputError(f"{sid!r} is not an Alertmanager silence id (a UUID).")
    return sid


def labels_match(labels: dict[str, str], matchers: list[Matcher]) -> bool:
    """Alertmanager's rule: every matcher holds; an absent label is the empty string."""
    return all(m.matches_value(str(labels.get(m.name, ""))) for m in matchers)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SilenceRequest:
    matchers: list[Matcher]
    duration_minutes: int
    comment: str
    created_by: str
    starts_at: datetime
    ends_at: datetime

    def to_api(self) -> dict[str, Any]:
        return {
            "matchers": [m.to_api() for m in self.matchers],
            "startsAt": _iso(self.starts_at),
            "endsAt": _iso(self.ends_at),
            "createdBy": self.created_by,
            "comment": self.comment,
        }


def build_request(args: dict, created_by: str | None, now: datetime | None = None) -> SilenceRequest:
    """Validate a create_silence call and build what would be sent."""
    matchers = parse_matchers(str(args.get("matchers", "")))
    validate_matchers(matchers)
    minutes = validate_duration(args.get("duration_minutes"))
    comment = validate_comment(args.get("comment"))
    if not created_by:
        raise SilenceInputError(
            "Pulse could not tell who you are, and a silence must name the person who created it. Sign in again."
        )
    start = now or datetime.now(UTC)
    return SilenceRequest(
        matchers=matchers,
        duration_minutes=minutes,
        comment=comment,
        created_by=created_by,
        starts_at=start,
        ends_at=start + timedelta(minutes=minutes),
    )


def _alert_summary(alert: dict) -> dict[str, Any]:
    labels = alert.get("labels") or {}
    return {
        "alertname": labels.get("alertname", ""),
        "severity": labels.get("severity", ""),
        "namespace": labels.get("namespace", ""),
        "labels": labels,
        "startsAt": alert.get("startsAt", ""),
    }


def split_by_matchers(alerts: list[dict], matchers: list[Matcher]) -> list[dict]:
    """The alerts a silence with these matchers would hide."""
    return [_alert_summary(a) for a in alerts if labels_match(a.get("labels") or {}, matchers)]


def describe_duration(minutes: int) -> str:
    if minutes % (24 * 60) == 0:
        days = minutes // (24 * 60)
        return f"{days} day{'s' if days != 1 else ''}"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minutes"


def preview_create(args: dict) -> dict[str, Any]:
    """What the chat shows before create_silence runs: the exact silence, who it names, what it hides."""
    from .request_context import get_current_user

    preview: dict[str, Any] = {"kind": "silence_create", "valid": False}
    try:
        req = build_request(args, get_current_user())
    except SilenceInputError as e:
        preview["error"] = str(e)
        preview["createdBy"] = get_current_user()
        return preview
    preview.update(
        {
            "valid": True,
            "matchers": [{**m.to_api(), "text": str(m)} for m in req.matchers],
            "durationMinutes": req.duration_minutes,
            "duration": describe_duration(req.duration_minutes),
            "startsAt": _iso(req.starts_at),
            "endsAt": _iso(req.ends_at),
            "createdBy": req.created_by,
            "comment": req.comment,
            "undo": "Expire it any time in Alerts > Silences, or ask Pulse to expire it.",
        }
    )
    from . import alertmanager

    try:
        firing = alertmanager.list_alerts(active=True, silenced=False, inhibited=False)
        preview["hides"] = split_by_matchers(firing, req.matchers)
    except alertmanager.AlertmanagerError as e:
        # The silence can still be reviewed from its matchers; what it would
        # hide right now is unknown, and says so rather than "nothing".
        preview["hides"] = None
        preview["hidesError"] = e.describe()
    return preview


def preview_expire(args: dict) -> dict[str, Any]:
    from . import alertmanager

    preview: dict[str, Any] = {"kind": "silence_expire", "valid": False}
    try:
        sid = validate_silence_id(args.get("silence_id"))
    except SilenceInputError as e:
        preview["error"] = str(e)
        return preview
    preview["silenceId"] = sid
    try:
        silence = alertmanager.get_silence(sid)
    except alertmanager.AlertmanagerError as e:
        preview["error"] = e.describe()
        return preview
    preview.update(
        {
            "valid": True,
            "state": (silence.get("status") or {}).get("state", ""),
            "matchers": [{**m.to_api(), "text": str(m)} for m in matchers_from_api(silence.get("matchers") or [])],
            "createdBy": silence.get("createdBy", ""),
            "comment": silence.get("comment", ""),
            "endsAt": silence.get("endsAt", ""),
        }
    )
    return preview
