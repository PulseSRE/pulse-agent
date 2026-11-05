"""urllib opener for requests that carry a bearer token.

``urllib.request.urlopen`` follows redirects with the default
``HTTPRedirectHandler``, which copies the request's headers -- including
``Authorization`` -- onto the redirected request. A Prometheus, Thanos or MCP
endpoint that answers with a 3xx to another host would therefore receive the
caller's (or the service account's) token, and a redirect to plain ``http``
would put it on the wire unencrypted.

``open_with_token`` follows redirects the same way except that:

- a redirect from ``https`` to anything else is refused (``InsecureRedirectError``);
- ``Authorization`` is kept only when the redirect stays on the same origin
  (scheme, host and port) and that origin is ``https``; otherwise it is dropped.

Once dropped, the token is not restored by a later redirect back.
"""

from __future__ import annotations

import http.client
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Any

_DEFAULT_PORTS = {"http": 80, "https": 443}


class InsecureRedirectError(urllib.error.URLError):
    """A redirect would have downgraded an https request to another scheme."""


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or _DEFAULT_PORTS.get(scheme)
    return scheme, (parts.hostname or "").lower(), port


class _TokenSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        old = _origin(req.full_url)
        new = _origin(newurl)
        if old[0] == "https" and new[0] != "https":
            fp.close()
            raise InsecureRedirectError(f"refused redirect from https to {new[0] or 'an unknown scheme'} ({code})")
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and not (old == new and new[0] == "https"):
            new_req.remove_header("Authorization")
        return new_req


def open_with_token(
    req: urllib.request.Request,
    *,
    timeout: float,
    context: ssl.SSLContext | None = None,
) -> Any:
    """Open ``req`` like ``urllib.request.urlopen``, without leaking its token on redirect."""
    handlers: list[urllib.request.BaseHandler] = [_TokenSafeRedirectHandler()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    return opener.open(req, timeout=timeout)
