"""Read-only GitHub tools over the REST API.

Public repositories work without a token (GitHub allows 60 unauthenticated
requests per hour per IP); `GITHUB_TOKEN` raises the limit, gives access to
private repositories the token can read, and is required for code search.
Only api.github.com (or `GITHUB_API_URL`) is contacted, never a
user-supplied host. Write operations are declared separately and are not
implemented.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.core.errors import InvalidInputError, RateLimitError, ToolError, TransientError
from app.mcp.schemas import ToolContext

REPOSITORY_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$"
# github.com/<owner>/<repo>[/blob|tree/<ref>/<path>]
REPO_URL_RE = re.compile(
    r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})(?:/(?:blob|tree)/([^/\s]+)/([^\s?#]+))?",
    re.IGNORECASE,
)
MAX_FILE_BYTES = 200_000
# Tests inject an httpx.MockTransport here.
TRANSPORT: httpx.BaseTransport | None = None


class RepositoryInput(BaseModel):
    repository: str = Field(pattern=REPOSITORY_PATTERN, description="owner/name")


class RepositoryTreeInput(RepositoryInput):
    ref: str | None = Field(default=None, max_length=200)
    max_entries: int = Field(default=300, ge=1, le=2000)


class RepositoryFileInput(RepositoryInput):
    path: str = Field(min_length=1, max_length=500)
    ref: str | None = Field(default=None, max_length=200)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        value = value.strip().lstrip("/")
        if not value or ".." in value.split("/") or "\\" in value:
            raise ValueError("Invalid repository path.")
        return value


class CodeSearchInput(RepositoryInput):
    query: str = Field(min_length=1, max_length=200)


class CommitsInput(RepositoryInput):
    limit: int = Field(default=10, ge=1, le=50)


def read_available() -> str:
    return "available" if get_settings().github_enabled else "not_configured"


def search_available() -> str:
    settings = get_settings()
    return "available" if settings.github_enabled and settings.github_token else "not_configured"


def parse_repository_urls(text: str) -> list[dict[str, str | None]]:
    """GitHub repository (and optional file) references found in text."""
    found: list[dict[str, str | None]] = []
    for owner, name, ref, path in REPO_URL_RE.findall(text):
        name = re.sub(r"\.git$", "", name.rstrip(".,;:!?)"))
        item = {"repository": f"{owner}/{name}", "ref": ref or None, "path": (path or "").rstrip(".,;:!?)") or None}
        if item not in found:
            found.append(item)
    return found[:3]


def _client() -> httpx.Client:
    settings = get_settings()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "agent-orchestrator",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return httpx.Client(base_url=settings.github_api_url, headers=headers, timeout=settings.web_timeout_seconds, transport=TRANSPORT)


def _get(path: str, *, params: dict[str, Any] | None = None, raw: bool = False) -> Any:
    try:
        with _client() as client:
            response = client.get(path, params=params, headers={"Accept": "application/vnd.github.raw"} if raw else None)
    except httpx.TimeoutException:
        raise TransientError("GitHub did not respond in time.") from None
    except httpx.TransportError:
        raise TransientError("GitHub could not be reached.") from None
    if response.status_code == 404:
        raise InvalidInputError("GitHub repository or path was not found (or is private).")
    remaining = response.headers.get("x-ratelimit-remaining")
    if response.status_code == 429 or (response.status_code == 403 and remaining == "0"):
        reset = response.headers.get("retry-after") or response.headers.get("x-ratelimit-reset")
        retry_after = None
        if reset and reset.isdigit():
            value = int(reset)
            retry_after = max(0, value - int(time.time())) if value > 1_000_000_000 else value
        raise RateLimitError(
            "GitHub API rate limit reached; set GITHUB_TOKEN for a higher limit.",
            details={"retry_after": retry_after},
            retryable=bool(retry_after is not None and retry_after <= 20),
        )
    if response.status_code in {401, 403}:
        raise ToolError("GitHub rejected the request (check GITHUB_TOKEN permissions).", retryable=False)
    if response.status_code >= 500:
        raise TransientError(f"GitHub returned HTTP {response.status_code}.")
    if response.status_code >= 400:
        raise ToolError(f"GitHub request failed (HTTP {response.status_code}).", retryable=False)
    if raw:
        return response.content
    return response.json()


def _decode(content: bytes) -> str:
    if b"\x00" in content[:8192]:
        raise InvalidInputError("The file is binary.")
    text = content[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    return text + ("\n[truncated]" if len(content) > MAX_FILE_BYTES else "")


def read_repository(arguments: RepositoryInput, context: ToolContext) -> dict[str, Any]:
    repo = _get(f"/repos/{arguments.repository}")
    readme = ""
    try:
        readme = _decode(_get(f"/repos/{arguments.repository}/readme", raw=True))[:12_000]
    except InvalidInputError:
        pass
    return {
        "repository": repo.get("full_name"),
        "url": repo.get("html_url"),
        "description": repo.get("description"),
        "default_branch": repo.get("default_branch"),
        "language": repo.get("language"),
        "topics": repo.get("topics", [])[:20],
        "stars": repo.get("stargazers_count"),
        "license": (repo.get("license") or {}).get("spdx_id"),
        "updated_at": repo.get("pushed_at"),
        "readme": readme,
    }


def list_repository_files(arguments: RepositoryTreeInput, context: ToolContext) -> dict[str, Any]:
    ref = arguments.ref or _get(f"/repos/{arguments.repository}").get("default_branch") or "HEAD"
    tree = _get(f"/repos/{arguments.repository}/git/trees/{ref}", params={"recursive": "1"})
    entries = sorted(item["path"] for item in tree.get("tree", []) if item.get("type") == "blob")
    return {
        "repository": arguments.repository,
        "ref": ref,
        "files": entries[: arguments.max_entries],
        "total_files": len(entries),
        "truncated": bool(tree.get("truncated")) or len(entries) > arguments.max_entries,
    }


def read_file(arguments: RepositoryFileInput, context: ToolContext) -> dict[str, Any]:
    params = {"ref": arguments.ref} if arguments.ref else None
    content = _get(f"/repos/{arguments.repository}/contents/{arguments.path}", params=params, raw=True)
    if isinstance(content, (dict, list)):
        raise InvalidInputError("The path is a directory, not a file.")
    return {"repository": arguments.repository, "path": arguments.path, "ref": arguments.ref, "content": _decode(content)}


def search_code(arguments: CodeSearchInput, context: ToolContext) -> list[dict[str, Any]]:
    if search_available() != "available":
        raise ToolError("GitHub code search requires GITHUB_TOKEN.", retryable=False)
    query = re.sub(r"\brepo:\S+", "", arguments.query).strip()
    data = _get("/search/code", params={"q": f"{query} repo:{arguments.repository}", "per_page": 10})
    return [{"path": item.get("path"), "url": item.get("html_url")} for item in data.get("items", [])[:10]]


def list_commits(arguments: CommitsInput, context: ToolContext) -> list[dict[str, Any]]:
    data = _get(f"/repos/{arguments.repository}/commits", params={"per_page": arguments.limit})
    commits = []
    for item in data[: arguments.limit]:
        commit = item.get("commit") or {}
        author = commit.get("author") or {}
        commits.append(
            {
                "sha": item.get("sha", "")[:12],
                "message": ((commit.get("message") or "").splitlines() or [""])[0][:200],
                "author": author.get("name"),
                "date": author.get("date"),
            }
        )
    return commits
