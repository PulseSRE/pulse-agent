"""Agent Orchestrator — classifies queries and routes to the appropriate skill.

Primary routing is via skill_loader.classify_query() (skill .md files).
This module provides:
  - fix_typos(): typo correction applied before classification
  - classify_intent(): legacy keyword-based fallback classifier
  - build_orchestrated_config(): config builder (delegates to skills first, falls back to legacy)
"""

import logging
import re

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
    "dahboard": "dashboard",
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

# Canonical K8s/SRE vocabulary for edit-distance matching
_VOCABULARY: set[str] = set(_TYPO_MAP.values()) | {
    "deployment",
    "namespace",
    "configmap",
    "service",
    "statefulset",
    "daemonset",
    "replicaset",
    "ingress",
    "persistent",
    "crashloop",
    "crashlooping",
    "prometheus",
    "kubernetes",
    "openshift",
    "orchestrator",
    "rollback",
    "scaling",
    "scheduler",
    "metrics",
    "metric",
    "resource",
    "resources",
    "volume",
    "certificate",
    "endpoint",
    "container",
    "vulnerability",
    "vulnerabilities",
    "compliance",
    "privilege",
    "permission",
    "network",
    "dashboard",
    "widget",
    "pod",
    "node",
    "helm",
    "argo",
    "gitops",
    "alert",
    "monitor",
    "storage",
    "replica",
    "replicas",
    "scale",
    "image",
    "cluster",
    "operator",
    "route",
    "secret",
    "quota",
}

# Suffix pattern for stripping before edit-distance check
_SUFFIX_PATTERN = re.compile(r"(ies|es|s|ing|ed|er)$", re.IGNORECASE)


def _edit_distance(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(a) < len(b):
        return _edit_distance(b, a)
    if len(b) == 0:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[len(b)]


def _fuzzy_match(word: str, max_distance: int = 2) -> str | None:
    """Find the closest vocabulary word within max_distance edits.

    Only considers words of similar length (±max_distance chars) to avoid
    matching short words to long ones. Returns None if no close match.
    """
    word_lower = word.lower()
    if word_lower in _VOCABULARY:
        return None  # Already correct

    # Check if the word is a vocab word + common suffix (e.g., "services" = "service" + "s")
    for suffix in ("s", "es", "ies", "ing", "ed", "er"):
        if word_lower.endswith(suffix):
            stem = word_lower[: -len(suffix)]
            if stem in _VOCABULARY:
                return None  # Already correct: known word + suffix

    # Strip suffix for matching, then reattach
    suffix_match = _SUFFIX_PATTERN.search(word_lower)
    base = _SUFFIX_PATTERN.sub("", word_lower) if suffix_match else word_lower

    best_match = None
    best_dist = max_distance + 1

    # Try matching the full word first (handles "kubernets" → "kubernetes")
    for vocab in _VOCABULARY:
        if abs(len(word_lower) - len(vocab)) > max_distance:
            continue
        if len(word_lower) < 6:
            continue
        dist = _edit_distance(word_lower, vocab)
        if dist <= max_distance and dist < best_dist:
            best_dist = dist
            best_match = vocab

    # If no full-word match, try base (suffix stripped) — handles "contaner" → "container"
    if not best_match and base != word_lower:
        for vocab in _VOCABULARY:
            if abs(len(base) - len(vocab)) > max_distance:
                continue
            if len(base) < 6:
                continue
            dist = _edit_distance(base, vocab)
            if dist <= max_distance and dist < best_dist:
                best_dist = dist
                best_match = vocab

    return best_match


def fix_typos(query: str) -> str:
    """Fix K8s/SRE typos in user query using two strategies:

    1. Fast path: exact match against _TYPO_MAP (130+ known misspellings)
    2. Fallback: edit-distance matching against vocabulary (catches novel typos)

    Case-preserving for the first char. Handles plural/suffix forms.
    """

    # Strategy 1: known typo map (fast, exact)
    def _replace_known(m: re.Match) -> str:
        base = m.group(1)
        suffix = m.group(2) or ""
        replacement = _TYPO_MAP[base.lower()]
        result = replacement + suffix
        if base[0].isupper():
            return result[0].upper() + result[1:]
        return result

    result = _TYPO_PATTERN.sub(_replace_known, query)

    # Strategy 2: edit-distance fallback (catches novel typos)
    words = result.split()
    changed = False
    for i, word in enumerate(words):
        # Skip short words, URLs, flags
        if len(word) < 5 or word.startswith("-") or "/" in word or ":" in word:
            continue
        match = _fuzzy_match(word)
        if match:
            corrected = match
            if word[0].isupper():
                corrected = corrected[0].upper() + corrected[1:]
            words[i] = corrected
            changed = True

    return " ".join(words) if changed else result


AgentMode = str  # Any skill name, plus "both" for merged mode

# "both" is a special merged mode (SRE + security tools) — not a skill
BOTH_KEYWORDS = [
    "scan the cluster",
    "full assessment",
    "production readiness",
    "audit everything",
    "check everything",
    "full audit",
    "cluster audit",
]


_SPLIT_PATTERN = re.compile(
    r"""
    (?:,\s*(?:and\s+)?also\s+)     |  # ", also" or ", and also"
    (?:\.\s+(?:Also|Then|Plus)\s+) |  # ". Also" / ". Then" / ". Plus"
    (?:,\s*(?:then|plus)\s+)       |  # ", then" / ", plus"
    (?:\s+and\s+(?=(?:check|scan|run|review|investigate|list|show|get|describe|verify|audit|analyze)\s))
    """,
    re.VERBOSE | re.IGNORECASE,
)


def split_compound_intent(query: str) -> list[str]:
    """Split a compound query into independent sub-intents.

    Only splits on conjunctions followed by action verbs to avoid splitting
    lists like "pods and services".
    """
    parts = _SPLIT_PATTERN.split(query)
    parts = [p.strip() for p in parts if p and p.strip()]
    return parts if parts else [query]


def classify_intent(query: str) -> tuple[AgentMode, bool]:
    """Route a user query to the best skill via ORCA multi-signal selector.

    Returns:
        (mode, is_strong) — mode is any skill name or "both".
        is_strong is True for confident routing, False for default fallback.
    """
    q = fix_typos(query).lower()

    # 0. Empty or trivially short queries — default to SRE (no signal for ORCA)
    if len(q.strip()) < 4:
        return "sre", False

    # 1. "both" = merge SRE + security tools (not a skill, check first)
    if any(kw in q for kw in BOTH_KEYWORDS):
        return "both", True

    # 2. Dashboard typo fuzzy matching (ORCA keywords can't catch creative typos)
    for word in q.split():
        if len(word) >= 7 and word.startswith("dash") and word not in ("dashing",):
            return "view_designer", True
        if len(word) >= 7 and word.startswith("das") and ("board" in word or "bord" in word or "baord" in word):
            return "view_designer", True

    # 3. ORCA multi-signal routing — single authority for all skills
    try:
        from .skill_loader import classify_query as _classify
        from .skill_selector import _last_selection_result_var

        skill = _classify(query)
        if skill:
            sel = _last_selection_result_var.get(None)
            if sel and sel.source == "fallback":
                top_score = max(sel.fused_scores.values()) if sel.fused_scores else 0
                # No real signal — default to SRE
                if top_score < 0.1:
                    return "sre", False
            is_strong = skill.name != "sre"  # non-default = strong signal
            return skill.name, is_strong
    except Exception:
        logger.debug("ORCA routing failed, falling back to sre", exc_info=True)

    return "sre", False


def build_orchestrated_config(mode: str, query: str = "") -> dict:
    """Return tool_defs, tool_map, system_prompt, write_tools for the given mode.

    Tries skill-based config first (from skill .md files). Falls back to
    hardcoded config for backward compatibility.

    Args:
        mode: Agent mode (sre, security, both, view_designer, or custom skill name).
        query: User query text for adaptive tool selection. When provided,
               uses TF-IDF / LLM prediction instead of static category matching.
    """
    # Try skill-based config first
    try:
        from .skill_loader import build_config_from_skill, get_skill

        skill = get_skill(mode)
        if skill:
            return build_config_from_skill(skill, query=query)
    except Exception:
        logger.debug("Skill-based config failed for mode=%s, using legacy", mode)

    # Legacy fallback
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
            "write_tools": set(),
        }
    elif mode == "security":
        return {
            "system_prompt": SECURITY_SYSTEM_PROMPT,
            "tool_defs": SEC_TOOL_DEFS,
            "tool_map": SEC_TOOL_MAP,
            "write_tools": set(),
        }
    elif mode == "both":
        merged_map = {**SRE_TOOL_MAP, **SEC_TOOL_MAP}
        # The static maps predate TOOL_REGISTRY, so tools that only auto-register
        # (skill_search, skill_load) were absent from 'both' while present in
        # 'sre' — the same registry-versus-module-map split fixed in
        # build_config_from_skill.
        from .tool_categories import ALWAYS_INCLUDE
        from .tool_registry import TOOL_REGISTRY

        for _name in ALWAYS_INCLUDE:
            if _name not in merged_map and _name in TOOL_REGISTRY:
                merged_map[_name] = TOOL_REGISTRY[_name]
        merged_defs = [t.to_dict() for t in merged_map.values()]
        combined_prompt = (
            SRE_PROMPT + "\n\nYou also have security scanning tools available. "
            "After diagnosing operational issues, check for related security concerns."
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
