"""Git integration tools — auto-PR creation for cluster changes.

Pillar 3: The "Ghost in the Machine" — turns manual cluster edits
into documented, versioned Git changes via automatic PR creation.
"""

from __future__ import annotations

import base64
import json
import os
import posixpath
import threading
import urllib.error
import urllib.parse
import urllib.request

from kubernetes.client.rest import ApiException

from .decorators import beta_tool
from .k8s_client import get_custom_client

# Per-thread PR counter to prevent runaway PR creation (thread-safe)
_pr_local = threading.local()
_MAX_PRS_PER_SESSION = int(os.environ.get("PULSE_AGENT_MAX_PRS", "5"))


def _get_pr_count() -> int:
    return getattr(_pr_local, "count", 0)


def _increment_pr_count():
    _pr_local.count = _get_pr_count() + 1


def _get_allowed_repos() -> set[str]:
    """Get the set of allowed repos from PULSE_ALLOWED_REPOS env var."""
    raw = os.environ.get("PULSE_ALLOWED_REPOS", "")
    if not raw:
        return set()
    return {r.strip() for r in raw.split(",") if r.strip()}


def _validate_file_path(path: str) -> str | None:
    """Validate file_path for path traversal. Returns error message or None."""
    if ".." in path.split("/"):
        return "Error: file_path contains '..' — path traversal is not allowed."
    normalized = posixpath.normpath(path)
    if normalized.startswith("/") or normalized.startswith(".."):
        return "Error: file_path must be a relative path within the repository."
    return None


def _github_api(method: str, path: str, body: dict | None = None, token: str = "") -> dict:
    """Make a GitHub API call."""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"GitHub API {e.code}: {error_body[:200]}") from e


@beta_tool
def propose_git_change(
    repo: str,
    file_path: str,
    new_content: str,
    commit_message: str,
    pr_title: str,
    pr_body: str = "",
    branch_name: str = "",
) -> str:
    """Create a Pull Request on GitHub with a YAML/config change. Turns a cluster edit into a documented Git change. REQUIRES USER CONFIRMATION.

    This is the "Commit to Git" flow: instead of just applying a change to the
    cluster, this creates a PR so the change is versioned, reviewed, and permanent.

    Args:
        repo: GitHub repository in 'owner/repo' format (e.g. 'myorg/k8s-manifests').
        file_path: Path to the file in the repo (e.g. 'apps/nginx/deployment.yaml').
        new_content: The full new content of the file.
        commit_message: Git commit message.
        pr_title: Pull request title.
        pr_body: Pull request description (optional).
        branch_name: Branch name for the PR (auto-generated if empty).
    """
    # Per-thread PR rate limiting

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return "Error: GITHUB_TOKEN environment variable is not set. Cannot create PRs."

    if "/" not in repo or len(repo.split("/")) != 2:
        return f"Error: Invalid repo format '{repo}'. Use 'owner/repo'."

    # Repo allow-list check
    allowed = _get_allowed_repos()
    if allowed and repo not in allowed:
        return f"Error: Repository '{repo}' is not in the allowed list. Allowed: {', '.join(sorted(allowed))}"

    # Path traversal check
    path_err = _validate_file_path(file_path)
    if path_err:
        return path_err

    # Per-session rate limit
    if _get_pr_count() >= _MAX_PRS_PER_SESSION:
        return (
            f"Error: PR rate limit reached ({_MAX_PRS_PER_SESSION} per session). Set PULSE_AGENT_MAX_PRS to increase."
        )

    try:
        # 1. Get the default branch and its SHA
        repo_info = _github_api("GET", f"/repos/{repo}", token=token)
        default_branch = repo_info["default_branch"]
        ref = _github_api("GET", f"/repos/{repo}/git/ref/heads/{default_branch}", token=token)
        base_sha = ref["object"]["sha"]

        # 2. Create a branch
        if not branch_name:
            import time

            branch_name = f"pulse-agent/{int(time.time())}"

        _github_api(
            "POST",
            f"/repos/{repo}/git/refs",
            token=token,
            body={
                "ref": f"refs/heads/{branch_name}",
                "sha": base_sha,
            },
        )

        # 3. Get the current file (if it exists) for the SHA
        file_sha = None
        try:
            existing = _github_api("GET", f"/repos/{repo}/contents/{file_path}?ref={branch_name}", token=token)
            file_sha = existing.get("sha")
        except RuntimeError:
            pass  # New file

        # 4. Create/update the file
        encoded = base64.b64encode(new_content.encode()).decode()
        file_body: dict = {
            "message": commit_message,
            "content": encoded,
            "branch": branch_name,
        }
        if file_sha:
            file_body["sha"] = file_sha

        _github_api("PUT", f"/repos/{repo}/contents/{file_path}", token=token, body=file_body)

        # 5. Create the PR
        if not pr_body:
            pr_body = (
                f"## Proposed by Pulse Agent\n\n"
                f"This change was generated by the Pulse Agent SRE assistant.\n\n"
                f"**File:** `{file_path}`\n"
                f"**Commit:** {commit_message}\n\n"
                f"Please review before merging."
            )

        pr = _github_api(
            "POST",
            f"/repos/{repo}/pulls",
            token=token,
            body={
                "title": pr_title,
                "body": pr_body,
                "head": branch_name,
                "base": default_branch,
            },
        )

        _increment_pr_count()

        return (
            f"Pull Request created successfully!\n"
            f"  PR: {pr['html_url']}\n"
            f"  Branch: {branch_name}\n"
            f"  File: {file_path}\n"
            f"  Status: Open — awaiting review\n"
            f"  PRs this session: {_get_pr_count()}/{_MAX_PRS_PER_SESSION}"
        )

    except RuntimeError as e:
        return f"Error creating PR: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


@beta_tool
def get_argo_app_source(name: str, namespace: str = "openshift-gitops") -> str:
    """Get the Git source details for an ArgoCD Application — repo URL, path, and target revision. Use this to find the right repo/path for propose_git_change.

    Args:
        name: Name of the ArgoCD Application.
        namespace: Namespace of the Application.
    """
    try:
        app = get_custom_client().get_namespaced_custom_object(
            "argoproj.io", "v1alpha1", namespace, "applications", name
        )
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}"

    source = app.get("spec", {}).get("source", {})
    repo = source.get("repoURL", "")
    path = source.get("path", "")
    revision = source.get("targetRevision", "HEAD")

    # Convert Git URL to owner/repo format for GitHub
    github_repo = ""
    if "github.com" in repo:
        clean = repo.rstrip("/")
        if clean.endswith(".git"):
            clean = clean[:-4]
        github_repo = clean.split("github.com")[-1].strip("/:")

    return json.dumps(
        {
            "repo_url": repo,
            "github_repo": github_repo,
            "path": path,
            "target_revision": revision,
            "app_name": name,
            "destination_namespace": app.get("spec", {}).get("destination", {}).get("namespace", ""),
        },
        indent=2,
    )


GIT_TOOLS = [propose_git_change, get_argo_app_source]

# Register git tools in the central registry
from .tool_registry import register_tool

register_tool(propose_git_change, is_write=True)
register_tool(get_argo_app_source, is_write=False)
