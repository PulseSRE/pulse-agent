"""Mask credential values in tool results before the model reads them.

The system prompt tells the model never to repeat a credential it sees in a
tool result, but a prompt is not a control: the live safety fixture
``safety_credential_in_tool_output`` showed the model occasionally quoting a
database password straight out of pod logs. Masking the value in code means
the model never has it to repeat, and the prompt rule stays as a second layer.

What is masked, conservatively, so ordinary Kubernetes output is unchanged:

- the password in URL userinfo (``postgres://user:PASSWORD@host``);
- ``Authorization: Bearer <token>`` / ``Basic <creds>`` values;
- the value of ``key=value`` / ``key: value`` pairs whose key *ends* in a
  credential word (``password``, ``passwd``, ``pwd``, ``secret``, ``token``,
  ``api_key``/``apikey``, ``access_key``, ``private_key``,
  ``client_secret``). Keys such as ``secretName`` or ``tokenExpirationSeconds``
  do not end in one and are left alone, as are booleans, ``null`` and
  references like ``${VAR}`` and ``<set to the key ...>``.

The key and where the value sat are kept, so the model can still say *which*
credential is present and where (``password=[redacted]``).
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

# scheme://user:password@host — keep scheme and user, mask the password.
_URL_USERINFO = re.compile(r"(?P<pre>\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]*:)(?P<val>[^\s@/]+)(?P<post>@)")

# A bearer token anywhere ("Bearer eyJ..."), or Basic credentials after an Authorization header.
_AUTH_HEADER = re.compile(
    r"(?P<pre>(?:\bBearer[ \t]+|\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?Basic[ \t]+))"
    r"(?P<val>[A-Za-z0-9\-._~+/]{12,}=*)",
    re.IGNORECASE,
)

_KEY_WORDS = r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
# The key ends in a credential word; the separator stays on the same line.
_KEY_VALUE = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9])[\"']?[A-Za-z0-9_.-]*?" + _KEY_WORDS + r"[\"']?[ \t]*[=:][ \t]*[\"']?)"
    r"(?!(?:true|false|null|none)\b)(?!\[redacted\])(?!\$[{(])(?!<)"
    r"(?P<val>[^\s\"',;&]{4,})",
    re.IGNORECASE,
)


def _sub(pattern: re.Pattern[str], text: str) -> str:
    return pattern.sub(lambda m: f"{m.group('pre')}{REDACTED}{m.groupdict().get('post') or ''}", text)


def redact_credentials(text: Any) -> Any:
    """Return ``text`` with credential values masked; non-strings are returned unchanged."""
    if not isinstance(text, str) or not text:
        return text
    out = _sub(_URL_USERINFO, text)
    out = _sub(_AUTH_HEADER, out)
    out = _sub(_KEY_VALUE, out)
    return out
