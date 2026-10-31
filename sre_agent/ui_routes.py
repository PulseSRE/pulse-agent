"""The pulse-ui routes the agent may link to, and a check for them.

The agent puts UI paths into what it emits: ``link`` on metric cards and
activity events, ``/custom/<id>`` for saved views, ``/logs/<ns>/<pod>`` in
pod tables. Nothing tied those strings to the UI's router, so when the UI
replaced ``/incidents`` with the Inbox the agent kept linking to a route that
no longer existed, and the UI's catch-all quietly sent every click to
``/pulse``.

``UI_ROUTES`` MIRRORS PULSE-UI'S ROUTE TABLE (``src/kubeview/engine/routeTable.ts``
once the UI's route-table PR lands; today ``src/kubeview/App.tsx`` and
``src/kubeview/routes/{resourceRoutes,domainRoutes}.tsx``). It must change
together with it: add a route here only when the UI serves it, and remove one
here in the same change that removes it from the UI. ``tests/test_ui_links.py``
scans ``sre_agent/`` for emitted links and fails on any path not listed here.

Deliberately not listed: ``/preview`` (the render service's chrome-less draft
page, never a link target) and the catch-all.
"""

from __future__ import annotations

import re

# Path patterns, React Router style. ``:name`` segments are checked by the
# validator of the same name in ``_PARAMS``. The query string and fragment are
# not part of the route and are ignored (``/r/v1~pods?ns=x`` is the pod list).
UI_ROUTES: tuple[str, ...] = (
    # Home
    "/pulse",
    # Resources. Cluster-scoped objects use "_" as the namespace segment.
    "/r/:gvr",
    "/r/:gvr/:namespace/:name",
    "/yaml/:gvr/:namespace/:name",
    "/logs/:pod_namespace/:name",
    "/node-logs/:name",
    "/metrics/:gvr/:namespace/:name",
    "/create/:gvr",
    "/deps/:gvr/:namespace/:name",
    # Saved and shared views
    "/custom/:view_id",
    "/share/:token",
    # Domain views
    "/workloads",
    "/networking",
    "/compute",
    "/storage",
    "/security",
    "/identity",
    "/admin",
    "/alerts",
    "/gitops",
    "/fleet",
    "/fleet/compare",
    "/fleet/compliance",
    "/fleet/workloads",
    "/fleet/alerts",
    "/fleet/r/:gvr",
    "/fleet/drift",
    "/inbox",
    "/readiness",
    "/about",
    "/views",
    "/agent",
    "/slo",
    "/topology",
    "/operators",
    "/project/:pod_namespace",
    "/timeline",
)

# A GroupVersionResource as the UI writes it in a path: ``group~version~plural``,
# or ``version~plural`` for the core group (``v1~pods``, never ``~v1~pods`` or
# ``core~v1~pods``).
_GROUP = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*"
_VERSION = r"v[1-9][0-9]*((alpha|beta)[1-9][0-9]*)?"
_PLURAL = r"[a-z][a-z0-9]*"
GVR_RE = re.compile(rf"^(?:(?!core~){_GROUP}~)?{_VERSION}~{_PLURAL}$")

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# Object names: DNS subdomains for most kinds, but RBAC and a few others allow
# ':' and upper case (system:auth-delegator).
_OBJECT_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._:@-]*[A-Za-z0-9])?$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

_PARAMS = {
    "gvr": lambda s: bool(GVR_RE.match(s)),
    "namespace": lambda s: s == "_" or bool(_DNS_LABEL.match(s)),
    "pod_namespace": lambda s: bool(_DNS_LABEL.match(s)),
    "name": lambda s: bool(_OBJECT_NAME.match(s)),
    "view_id": lambda s: bool(_OPAQUE_ID.match(s)),
    "token": lambda s: bool(_OPAQUE_ID.match(s)),
}

_SPLIT_ROUTES = tuple(tuple(r.split("/")[1:]) for r in UI_ROUTES)


def is_gvr(value: str) -> bool:
    """True if *value* is a GVR in the UI's path form (``apps~v1~deployments``)."""
    return bool(GVR_RE.match(value))


def is_ui_route(link: str) -> bool:
    """True if *link* is a path pulse-ui routes to a page (query/fragment ignored)."""
    if not isinstance(link, str) or not link.startswith("/"):
        return False
    path = link.split("#", 1)[0].split("?", 1)[0]
    segments = path.split("/")[1:]
    if any(not s for s in segments):  # "//", trailing "/", or bare "/"
        return False
    for route in _SPLIT_ROUTES:
        if len(route) != len(segments):
            continue
        if all(_PARAMS[r[1:]](s) if r.startswith(":") else r == s for r, s in zip(route, segments, strict=True)):
            return True
    return False
