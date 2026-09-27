"""Python execution agent.

Runs user-supplied (or, when a model is configured, model-written) Python in
the isolated sandbox tool. The tool is HIGH risk and requires the user to
approve `sandbox.python_exec` for the request; without approval the step is
BLOCKED and nothing executes.
"""

from __future__ import annotations

import re

from app.agents.context import AgentContext
from app.agents.prompts import system_prompt
from app.agents.registry import ModelPolicy, PermissionPolicy, RetryPolicy, RoutingHints, register_agent
from app.models import AgentResult, AgentTask

CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
WRITER_ROLE = (
    "You write short, self-contained Python 3 snippets that print their result. "
    "Use only the standard library modules math, statistics, json, re, datetime, "
    "itertools, functools, collections, random, decimal, fractions. No file, network "
    "or OS access. Reply with a single ```python code block and nothing else."
)


def extract_code(text: str) -> str | None:
    match = CODE_BLOCK_RE.search(text)
    return match.group(1).strip() if match else None


@register_agent(
    name="python_executor",
    description="Executes Python snippets in an isolated, resource-limited sandbox (requires approval).",
    capabilities=["python_execution"],
    tools=["sandbox.python_exec"],
    timeout_seconds=45,
    retry_policy=RetryPolicy(max_retries=0),
    model_policy=ModelPolicy(tier="fast", temperature=0.0, max_output_tokens=800),
    token_budget=2000,
    permission_policy=PermissionPolicy(granted_permissions={"code:execute"}),
    routing_hints={
        "python_execution": RoutingHints(
            description="Run/execute a Python snippet and return its output.",
            keywords={
                "run this code": 2.5,
                "run this python": 2.5,
                "execute this python": 2.5,
                "execute the python": 2.5,
                "execute this code": 2.5,
                "run the following code": 2.5,
                "run the following python": 2.5,
                "compute with python": 2.0,
                "sandbox": 1.0,
            },
            suppresses={"code_generation"},
        )
    },
)
async def python_executor(task: AgentTask, ctx: AgentContext) -> AgentResult:
    code = extract_code(task.goal)
    generated = False
    if code is None:
        response = await ctx.llm(system=system_prompt(WRITER_ROLE, output_rules=False), user=task.goal, cacheable=True)
        code = extract_code(response.text) if response else None
        generated = code is not None
    if not code:
        return AgentResult(
            summary="No Python code block was provided and no model is available to write one.",
            warnings=["Include the code in a ```python fenced block."],
            metadata={"tool_success": False},
        )
    result = await ctx.call_tool("sandbox.python_exec", {"code": code})
    status = result.get("status")
    # Wall-clock timing lives in metadata, not in the answer text, so the same
    # code always produces the same answer.
    lines = [f"Sandbox status: **{status}**."]
    if result.get("stdout"):
        lines.append("```text\n" + str(result["stdout"])[:4000] + "\n```")
    if result.get("stderr"):
        lines.append("Errors:\n```text\n" + str(result["stderr"])[:2000] + "\n```")
    warnings = [f"Blocked by sandbox policy: {violation}" for violation in result.get("violations", [])]
    if generated:
        warnings.append("The executed code was written by the model; review it before relying on the output.")
    return AgentResult(
        summary="\n\n".join(lines),
        artifacts=[{"type": "sandbox_run", "status": status, "code": code[:4000]}],
        warnings=warnings,
        metadata={
            "tool_success": status == "ok",
            "sandbox": result.get("limits", {}),
            "sandbox_duration_ms": result.get("duration_ms", 0),
            "generated_code": generated,
        },
    )
