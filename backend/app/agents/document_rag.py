from __future__ import annotations

from app.agents.context import AgentContext
from app.agents.registry import ModelPolicy, PermissionPolicy, RetryPolicy, RoutingHints, register_agent
from app.models import AgentResult, AgentTask, SourceReference
from app.rag.workflow import rag_graph_metadata, run_document_rag


@register_agent(
    name="document_rag",
    description="Runs a LangGraph retrieval workflow over the caller's indexed documents with citations.",
    capabilities=["document_retrieval"],
    tools=["vector.search_chunks"],
    timeout_seconds=60,
    retry_policy=RetryPolicy(max_retries=1),
    model_policy=ModelPolicy(tier="quality", temperature=0.1, max_output_tokens=1200),
    token_budget=8000,
    permission_policy=PermissionPolicy(granted_permissions={"vector:read"}),
    routing_hints={
        "document_retrieval": RoutingHints(
            description="Answer questions from the user's uploaded documents.",
            file_extensions={".pdf", ".docx", ".md", ".txt"},
        )
    },
)
async def document_rag(task: AgentTask, ctx: AgentContext) -> AgentResult:
    document_ids = [file.document_id for file in task.files if file.document_id]
    state = await run_document_rag(task.goal, document_ids, ctx)
    context_chunks = state.get("context_chunks", [])
    sources = [
        SourceReference(
            title=chunk["document_name"],
            source_type="document",
            document_id=chunk["document_id"],
            page=chunk.get("page_number"),
            chunk_id=chunk["id"],
        )
        for chunk in context_chunks
    ]
    findings = []
    if context_chunks:
        similarities = [chunk.get("similarity", 0.0) for chunk in context_chunks]
        findings.append(
            f"Retrieved {len(context_chunks)} chunks with similarity scores from "
            f"{min(similarities):.2f} to {max(similarities):.2f}."
        )
    return AgentResult(
        summary=state["answer"],
        findings=findings,
        sources=sources,
        warnings=state.get("warnings", []),
        metadata={
            **rag_graph_metadata(),
            "retrieved_chunks": len(context_chunks),
            "quarantined_or_filtered": len(state.get("retrieved_chunks", [])) - len(state.get("safe_chunks", [])),
            "generation_mode": state.get("generation_mode", "unknown"),
            "faithfulness": state.get("faithfulness"),
            "tool_success": True,
        },
    )
