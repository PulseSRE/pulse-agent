"""Agent Orchestrator — classifies queries and routes to SRE or Security agent."""

import logging
import re
from typing import Literal

logger = logging.getLogger("pulse_agent.orchestrator")

# ---------------------------------------------------------------------------
# Typo correction — applied before intent classification and tool selection.
# Maps common misspellings to the correct K8s/SRE/security term.
# Only whole-word replacements to avoid mangling valid text.
# ---------------------------------------------------------------------------
_TYPO_MAP: dict[str, str] = {
    # Kubernetes resources
    "depoyment": "deployment",
    "deploymnet": "deployment",
    "deployemnt": "deployment",
    "deplyoment": "deployment",
    "deloyment": "deployment",
    "deployement": "deployment",
    "deploment": "deployment",
    "depolyment": "deployment",
    "deploymment": "deployment",
    "namepsace": "namespace",
    "namepspace": "namespace",
    "namspace": "namespace",
    "namsepace": "namespace",
    "naemspace": "namespace",
    "namesapce": "namespace",
    "namespacce": "namespace",
    "namespcae": "namespace",
    "confimap": "configmap",
    "confgimap": "configmap",
    "cofigmap": "configmap",
    "serivce": "service",
    "servce": "service",
    "sevice": "service",
    "srevice": "service",
    "sercive": "service",
    "servcie": "service",
    "statefullset": "statefulset",
    "statefuset": "statefulset",
    "statfulset": "statefulset",
    "daemonest": "daemonset",
    "deamonset": "daemonset",
    "dameonset": "daemonset",
    "replicaest": "replicaset",
    "relicaset": "replicaset",
    "replicast": "replicaset",
    "ingerss": "ingress",
    "ingrss": "ingress",
    "igress": "ingress",
    "ingresss": "ingress",
    "persitent": "persistent",
    "persistant": "persistent",
    "perisistent": "persistent",
    # SRE terms
    "crashlopp": "crashloop",
    "crashlop": "crashloop",
    "crahsloop": "crashloop",
    "crashoping": "crashlooping",
    "crashloping": "crashlooping",
    "craslhooping": "crashlooping",
    "promethues": "prometheus",
    "promethesu": "prometheus",
    "prometheous": "prometheus",
    "pormetheus": "prometheus",
    "promethus": "prometheus",
    "kuberntes": "kubernetes",
    "kuberentes": "kubernetes",
    "kuberneets": "kubernetes",
    "kubernetse": "kubernetes",
    "kubneretes": "kubernetes",
    "openshift": "openshift",
    "openshfit": "openshift",
    "openshitf": "openshift",
    "oepnshift": "openshift",
    "opesnhift": "openshift",
    "orcehstrator": "orchestrator",
    "ochestrator": "orchestrator",
    "orchretsrator": "orchestrator",
    "orchestartor": "orchestrator",
    "rollbak": "rollback",
    "rolback": "rollback",
    "rollbcak": "rollback",
    "scael": "scale",
    "sacale": "scale",
    "sclaing": "scaling",
    "sacling": "scaling",
    "scailng": "scaling",
    "scheudler": "scheduler",
    "schdeuler": "scheduler",
    "schduler": "scheduler",
    "metrcs": "metrics",
    "metircs": "metrics",
    "metics": "metrics",
    "metirc": "metric",
    "metrc": "metric",
    "resurce": "resource",
    "resoruce": "resource",
    "resouce": "resource",
    "resrouce": "resource",
    "resouces": "resources",
    "reosurces": "resources",
    "resuorces": "resources",
    "volmue": "volume",
    "voume": "volume",
    "volumne": "volume",
    "certifiate": "certificate",
    "certifcate": "certificate",
    "certificat": "certificate",
    "ceritficate": "certificate",
    "endpont": "endpoint",
    "enpoint": "endpoint",
    "endpiont": "endpoint",
    "containre": "container",
    "contaienr": "container",
    "conatiner": "container",
    "continer": "container",
    "contanier": "container",
    # Security terms
    "vulerability": "vulnerability",
    "vulernability": "vulnerability",
    "vulnerabilty": "vulnerability",
    "vulnrability": "vulnerability",
    "vulnerablity": "vulnerability",
    "vulnerabiltiy": "vulnerability",
    "vulnerabilites": "vulnerabilities",
    "vulernabilities": "vulnerabilities",
    "vulerabilities": "vulnerabilities",
    "compliace": "compliance",
    "complianec": "compliance",
    "compliacne": "compliance",
    "privilige": "privilege",
    "privlege": "privilege",
    "privelege": "privilege",
    "previlege": "privilege",
    "permision": "permission",
    "permssion": "permission",
    "permsision": "permission",
    "netwrok": "network",
    "netowrk": "network",
    "newtork": "network",
    "nework": "network",
    # View/dashboard terms
    "dahsboard": "dashboard",
    "dashbaord": "dashboard",
    "dashbord": "dashboard",
    "dasbhoard": "dashboard",
    "dashoard": "dashboard",
    "dashborad": "dashboard",
    "widegt": "widget",
    "wiget": "widget",
    "wdiget": "widget",
}

# Pre-compile the regex pattern — matches base typo + optional suffix
_TYPO_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_TYPO_MAP, key=len, reverse=True)) + r")(ies|es|s|ing|ed|er)?\b",
    re.IGNORECASE,
)


def fix_typos(query: str) -> str:
    """Fix common K8s/SRE typos in user query. Case-preserving for the first char.

    Handles plural/suffixed forms automatically — if 'depoyment' is in the map,
    'depoyments' will also be corrected to 'deployments'.
    """

    def _replace(m: re.Match) -> str:
        base = m.group(1)
        suffix = m.group(2) or ""
        replacement = _TYPO_MAP[base.lower()]
        result = replacement + suffix
        if base[0].isupper():
            return result[0].upper() + result[1:]
        return result

    return _TYPO_PATTERN.sub(_replace, query)


AgentMode = Literal["sre", "security", "both", "view_designer"]

SRE_KEYWORDS = [
    "crash",
    "restart",
    "pod",
    "deploy",
    "node",
    "scale",
    "log",
    "event",
    "health",
    "capacity",
    "oom",
    "pending",
    "drain",
    "cordon",
    "prometheus",
    "alert",
    "metric",
    "resource",
    "quota",
    "memory",
    "cpu",
    "disk",
    "image",
    "pull",
    "schedule",
    "replica",
    "rollout",
    "ingress",
    "route",
    "service",
    "endpoint",
    "pvc",
    "volume",
    "operator",
    "certificate",
    "cert",
    "tls",
    "expir",
]

SECURITY_KEYWORDS = [
    "security",
    "security scan",
    "security audit",
    "security check",
    "rbac",
    "role",
    "permission",
    "scc",
    "network policy",
    "networkpolicy",
    "secret",
    "privilege",
    "root",
    "audit",
    "compliance",
    "vulnerability",
    "vulnerabilities",
    "tls",
    "certificate",
    "access control",
    "service account",
    "cluster-admin",
    "wildcard",
    "overly permissive",
    "security context",
    "capability",
]

BOTH_KEYWORDS = [
    "scan the cluster",
    "full assessment",
    "production readiness",
    "audit everything",
    "check everything",
    "full audit",
    "cluster audit",
]

VIEW_DESIGNER_KEYWORDS = [
    "dashboard",
    "create a view",
    "create me view",
    "create view",
    "build a view",
    "build me a view",
    "build view",
    "design a view",
    "make a view",
    "make me a view",
    "make view",
    "build me a dashboard",
    "create a dashboard",
    "customize the view",
    "edit the view",
    "edit view",
    "update view",
    "update the view",
    "change layout",
    "update layout",
    "edit layout",
    "change the layout",
    "add widget",
    "add a widget",
    "remove widget",
    "show me a dashboard",
    "make a view",
    "make me a view",
    "new view",
    "modify the view",
    "redesign",
    "rearrange",
    "build me an overview",
    "create an overview",
    "build an overview",
    "overview dashboard",
    "monitoring dashboard",
    "monitoring view",
    "delete dashboard",
    "delete the dashboard",
    "delete my dashboard",
    "clone dashboard",
    "clone my dashboard",
    "duplicate dashboard",
    "copy dashboard",
    "rebuild dashboard",
    "rebuild the dashboard",
    "fix the dashboard",
    "fix my dashboard",
    "improve the dashboard",
    "update the dashboard",
    "rename the chart",
    "rename the widget",
    "change the title",
    "fix the title",
    "update the title",
]


_VIEW_TRIGGER_WORDS = {
    "widget",
    "sparkline",
    "metric card",
    "overview",
    "page",
    "stat card",
    "bar list",
    "progress list",
    "bar_list",
    "progress_list",
    "stat_card",
}
# Phrases that indicate view/dashboard editing intent
_VIEW_TRIGGER_PHRASES = {
    "a view",
    "the view",
    "my view",
    "this view",
    "new view",
    "me view",
    "app view",
    "application view",
    "the chart",
    "my chart",
    "the widget",
    "my widget",
    "the title",
    "the description",
}


def _keyword_score(query_lower: str, keywords: list[str]) -> float:
    """Score query against keywords, weighting longer (more specific) keywords higher."""
    return sum(len(kw) for kw in keywords if kw in query_lower)


def classify_intent(query: str) -> tuple[AgentMode, bool]:
    """Classify a user query as sre, security, both, or view_designer.

    Returns:
        (mode, is_strong) — is_strong is True when the classification is based
        on explicit keyword matches, False when it's the default sre fallback.
        Callers should only switch session mode on strong signals.
    """
    q = query.lower()

    # Fuzzy match for "dashboard" — catch common typos
    # Check if any word is within edit distance 2 of "dashboard"
    for word in q.split():
        if len(word) >= 7 and word.startswith("dash") and word not in ("dashing",):
            return "view_designer", True
        if len(word) >= 7 and word.startswith("das") and ("board" in word or "bord" in word or "baord" in word):
            return "view_designer", True

    # Check "both" first (explicit full-audit requests)
    if any(kw in q for kw in BOTH_KEYWORDS):
        return "both", True

    # Check view designer (dashboard/view creation requests)
    if (
        any(kw in q for kw in VIEW_DESIGNER_KEYWORDS)
        or any(w in q for w in _VIEW_TRIGGER_WORDS)
        or any(p in q for p in _VIEW_TRIGGER_PHRASES)
    ):
        return "view_designer", True

    sre_score = _keyword_score(q, SRE_KEYWORDS)
    sec_score = _keyword_score(q, SECURITY_KEYWORDS)

    # If both domains match and scores are close, route to both
    if sre_score > 0 and sec_score > 0:
        ratio = min(sre_score, sec_score) / max(sre_score, sec_score)
        if ratio >= 0.7:
            return "both", True

    if sec_score > sre_score and sec_score > 0:
        return "security", True

    # SRE is the default — only strong if there were actual keyword matches
    return "sre", sre_score > 0


def build_orchestrated_config(mode: AgentMode) -> dict:
    """Return tool_defs, tool_map, system_prompt, write_tools for the given mode."""
    from .agent import SYSTEM_PROMPT as SRE_PROMPT
    from .agent import TOOL_DEFS as SRE_TOOL_DEFS
    from .agent import TOOL_MAP as SRE_TOOL_MAP
    from .agent import WRITE_TOOLS as SRE_WRITE_TOOLS
    from .security_agent import SECURITY_SYSTEM_PROMPT
    from .security_agent import TOOL_DEFS as SEC_TOOL_DEFS
    from .security_agent import TOOL_MAP as SEC_TOOL_MAP

    if mode == "view_designer":
        from .view_designer import TOOL_DEFS as VD_TOOL_DEFS
        from .view_designer import TOOL_MAP as VD_TOOL_MAP
        from .view_designer import VIEW_DESIGNER_SYSTEM_PROMPT

        return {
            "system_prompt": VIEW_DESIGNER_SYSTEM_PROMPT,
            "tool_defs": VD_TOOL_DEFS,
            "tool_map": VD_TOOL_MAP,
            "write_tools": set(),  # View designer never modifies cluster state
        }
    elif mode == "security":
        return {
            "system_prompt": SECURITY_SYSTEM_PROMPT,
            "tool_defs": SEC_TOOL_DEFS,
            "tool_map": SEC_TOOL_MAP,
            "write_tools": set(),
        }
    elif mode == "both":
        # Merge both tool sets, use SRE prompt with security addendum
        merged_map = {**SRE_TOOL_MAP, **SEC_TOOL_MAP}
        merged_defs = SRE_TOOL_DEFS + [d for d in SEC_TOOL_DEFS if d.get("name") not in SRE_TOOL_MAP]
        combined_prompt = (
            SRE_PROMPT
            + "\n\n"
            + (
                "You also have security scanning tools available. "
                "After diagnosing operational issues, check for related security concerns."
            )
        )
        return {
            "system_prompt": combined_prompt,
            "tool_defs": merged_defs,
            "tool_map": merged_map,
            "write_tools": SRE_WRITE_TOOLS,
        }
    else:  # sre
        return {
            "system_prompt": SRE_PROMPT,
            "tool_defs": SRE_TOOL_DEFS,
            "tool_map": SRE_TOOL_MAP,
            "write_tools": SRE_WRITE_TOOLS,
        }
