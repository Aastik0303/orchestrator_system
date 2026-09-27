from __future__ import annotations

import re
from pathlib import PurePosixPath

from app.agents.common import build_user_prompt, provider_unavailable, render_evidence
from app.agents.context import AgentContext
from app.agents.prompts import system_prompt
from app.agents.registry import ModelPolicy, PermissionPolicy, RetryPolicy, RoutingHints, register_agent
from app.core.errors import OrchestratorError
from app.guardrails.retrieval_guard import validate_citations
from app.mcp.github import parse_repository_urls
from app.models import AgentResult, AgentTask, SourceReference

ROLE = (
    "You are the Code Development Agent. Help with architecture, implementation, "
    "debugging, review, and testing. Be concrete and preserve user data. You cannot "
    "execute code or shell commands; say so if asked. When repository <document> "
    "sources are provided, base statements about the repository on them and cite them "
    "as [Source N]; say what you could not see instead of guessing."
)
# Files that best explain a repository when the question names none.
KEY_FILES = (
    "pyproject.toml", "requirements.txt", "setup.py", "package.json", "go.mod", "Cargo.toml",
    "pom.xml", "build.gradle", "Dockerfile", "docker-compose.yml", "Makefile",
)
MAX_FILES_READ = 3
MAX_TREE_ENTRIES = 400


def _files_to_read(files: list[str], question: str) -> list[str]:
    """Files named in the question first, then top-level manifests."""
    words = {word.lower() for word in re.findall(r"[A-Za-z0-9_.-]{3,}", question)}
    named = [path for path in files if PurePosixPath(path).name.lower() in words or path.lower() in words]
    manifests = [path for path in files if "/" not in path and path in KEY_FILES]
    return list(dict.fromkeys([*named, *manifests]))[:MAX_FILES_READ]


async def _repository_evidence(task: AgentTask, ctx: AgentContext) -> tuple[list[dict], list[str]]:
    references = parse_repository_urls(task.goal)
    if not references:
        return [], []
    if not ctx.run.tools.is_available("github.read_repository"):
        return [], ["A GitHub repository was linked, but the GitHub tools are disabled (GITHUB_ENABLED)."]
    reference = references[0]
    repository = str(reference["repository"])
    items: list[dict] = []
    try:
        info = await ctx.call_tool("github.read_repository", {"repository": repository})
        overview = "\n".join(
            f"{label}: {info.get(key)}"
            for label, key in (
                ("Repository", "repository"), ("Description", "description"), ("Language", "language"),
                ("Default branch", "default_branch"), ("Topics", "topics"), ("License", "license"), ("Last push", "updated_at"),
            )
            if info.get(key)
        )
        items.append({"title": f"{repository} overview and README", "url": info.get("url"), "content": overview + "\n\nREADME:\n" + (info.get("readme") or "(none)")})
        paths: list[str]
        if reference["path"]:
            paths = [str(reference["path"])]
        else:
            tree = await ctx.call_tool("github.list_repository_files", {"repository": repository, "max_entries": MAX_TREE_ENTRIES})
            listing = "\n".join(tree["files"])
            if tree.get("truncated"):
                listing += f"\n... ({tree['total_files']} files in total)"
            items.append({"title": f"{repository} file tree", "url": info.get("url"), "content": listing})
            paths = _files_to_read(tree["files"], task.goal)
        for path in paths:
            try:
                file = await ctx.call_tool("github.read_file", {"repository": repository, "path": path, "ref": reference["ref"]})
            except OrchestratorError:
                continue
            url = f"{info.get('url')}/blob/{reference['ref'] or info.get('default_branch') or 'HEAD'}/{path}"
            items.append({"title": f"{repository}/{path}", "url": url, "content": file["content"]})
    except OrchestratorError as exc:
        return items, [f"Could not read the GitHub repository ({exc.error_type.value}: {str(exc)[:160]})."]
    return items, []


@register_agent(
    name="code_dev",
    description="Handles implementation, architecture, debugging, and review; reads linked GitHub repositories.",
    capabilities=["code_generation"],
    tools=["github.read_repository", "github.list_repository_files", "github.read_file", "github.search_code", "github.list_commits"],
    timeout_seconds=60,
    retry_policy=RetryPolicy(max_retries=1),
    model_policy=ModelPolicy(tier="quality", temperature=0.2, max_output_tokens=1800),
    token_budget=10000,
    permission_policy=PermissionPolicy(granted_permissions={"github:read"}),
    routing_hints={
        "code_generation": RoutingHints(
            description="Write, review, debug or explain code; understand a linked GitHub repository.",
            url_patterns=[r"github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+"],
        )
    },
)
async def code_dev(task: AgentTask, ctx: AgentContext) -> AgentResult:
    items, warnings = await _repository_evidence(task, ctx)
    evidence, kept, evidence_warnings = render_evidence(items, ctx, max_chars=12000)
    warnings.extend(evidence_warnings)
    prompt = build_user_prompt(task)
    if evidence:
        prompt += "\n\nRepository sources:\n" + evidence
    response = await ctx.llm(system=system_prompt(ROLE), user=prompt)
    sources = [SourceReference(title=item.get("title"), source_type="repository", uri=item.get("url")) for item in kept]
    if response is None:
        result = provider_unavailable("Code analysis")
        result.warnings.extend(warnings)
        result.sources = sources
        return result
    answer = response.text
    if kept:
        answer, citation_warnings = validate_citations(answer, len(kept))
        warnings.extend(citation_warnings)
    metadata = {"model": response.model, "tokens": response.total_tokens}
    if items or warnings:
        metadata.update({"repository_sources": len(kept), "tool_success": bool(kept)})
    return AgentResult(summary=answer, sources=sources, warnings=list(dict.fromkeys(warnings)), metadata=metadata)
