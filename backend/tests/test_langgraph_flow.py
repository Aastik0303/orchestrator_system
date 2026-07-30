import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import AgentName, AgentResponse, ChatRequest, UploadedFile
from app.orchestrator.langgraph_flow import build_graph
from app.orchestrator.supervisor import AGENTS


def _request(
    message: str,
    *,
    deep_research: bool = False,
    agent_override: AgentName = AgentName.AUTO,
    files: list[UploadedFile] | None = None,
) -> ChatRequest:
    return ChatRequest(
        message=message,
        deep_research=deep_research,
        agent_override=agent_override,
        session_id="test-thread",
        files=files or [],
    )


def _handler(agent_name: AgentName, artifacts: list[dict] | None = None):
    def inner(request: ChatRequest) -> AgentResponse:
        return AgentResponse(
            active_agent=agent_name,
            response=f"{agent_name.value} ok",
            artifacts=artifacts or [],
        )

    return inner


class LangGraphFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _invoke(
        self,
        request: ChatRequest,
        *,
        thread_id: str = "test-thread",
        graph=None,
    ) -> AgentResponse:
        graph = graph or build_graph(checkpointer=InMemorySaver())
        result = await graph.ainvoke(
            {
                "request": request.model_copy(update={"session_id": thread_id}),
                "active_agent": None,
                "response": None,
                "is_valid": False,
                "retry_count": 0,
                "error": None,
            },
            config={"configurable": {"thread_id": thread_id}},
        )
        return result["response"]

    async def test_general_chat_routing(self):
        with patch.dict(AGENTS, {AgentName.GENERAL_CHAT: _handler(AgentName.GENERAL_CHAT)}):
            response = await self._invoke(_request("hello there"))

        self.assertEqual(response.active_agent, AgentName.GENERAL_CHAT)

    async def test_code_agent_routing(self):
        with patch.dict(AGENTS, {AgentName.CODE_DEV: _handler(AgentName.CODE_DEV)}):
            response = await self._invoke(_request("debug this python api"))

        self.assertEqual(response.active_agent, AgentName.CODE_DEV)

    async def test_csv_routing(self):
        request = _request(
            "summarize this",
            files=[UploadedFile(name="data.csv", storage_path="data.csv")],
        )
        with patch.dict(AGENTS, {AgentName.DATA_ANALYST: _handler(AgentName.DATA_ANALYST)}):
            response = await self._invoke(request)

        self.assertEqual(response.active_agent, AgentName.DATA_ANALYST)

    async def test_document_routing(self):
        request = _request(
            "answer from this document",
            files=[UploadedFile(name="notes.pdf", storage_path="notes.pdf")],
        )
        with patch.dict(AGENTS, {AgentName.DOCUMENT_RAG: _handler(AgentName.DOCUMENT_RAG)}):
            response = await self._invoke(request)

        self.assertEqual(response.active_agent, AgentName.DOCUMENT_RAG)

    async def test_youtube_url_routing(self):
        with patch.dict(AGENTS, {AgentName.YOUTUBE_RAG: _handler(AgentName.YOUTUBE_RAG)}):
            response = await self._invoke(_request("https://youtu.be/example"))

        self.assertEqual(response.active_agent, AgentName.YOUTUBE_RAG)

    async def test_manual_agent_override(self):
        request = _request("plain message", agent_override=AgentName.DATA_ANALYST)
        with patch.dict(AGENTS, {AgentName.DATA_ANALYST: _handler(AgentName.DATA_ANALYST)}):
            response = await self._invoke(request)

        self.assertEqual(response.active_agent, AgentName.DATA_ANALYST)

    async def test_deep_research_routing(self):
        request = _request("explain the topic carefully", deep_research=True)
        with patch.dict(AGENTS, {AgentName.DEEP_RESEARCH: _handler(AgentName.DEEP_RESEARCH)}):
            response = await self._invoke(request)

        self.assertEqual(response.active_agent, AgentName.DEEP_RESEARCH)

    async def test_successful_validation(self):
        with patch.dict(AGENTS, {AgentName.GENERAL_CHAT: _handler(AgentName.GENERAL_CHAT)}):
            response = await self._invoke(_request("hello"))

        self.assertEqual(response.response, "general_chat ok")

    async def test_retry_behavior(self):
        calls = 0

        def flaky(request: ChatRequest) -> AgentResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return AgentResponse(active_agent=AgentName.GENERAL_CHAT, response="")
            return AgentResponse(active_agent=AgentName.GENERAL_CHAT, response="ok after retry")

        with patch.dict(AGENTS, {AgentName.GENERAL_CHAT: flaky}):
            response = await self._invoke(_request("hello"))

        self.assertEqual(calls, 2)
        self.assertEqual(response.response, "ok after retry")

    async def test_fallback_after_maximum_retries(self):
        calls = 0

        def empty(request: ChatRequest) -> AgentResponse:
            nonlocal calls
            calls += 1
            return AgentResponse(active_agent=AgentName.GENERAL_CHAT, response="")

        with patch.dict(AGENTS, {AgentName.GENERAL_CHAT: empty}):
            response = await self._invoke(_request("hello"))

        self.assertEqual(calls, 3)
        self.assertIn("could not complete", response.response)

    async def test_artifact_preservation(self):
        artifacts = [{"name": "processed.csv", "path": "storage/outputs/processed.csv"}]
        with patch.dict(
            AGENTS,
            {AgentName.DATA_ANALYST: _handler(AgentName.DATA_ANALYST, artifacts=artifacts)},
        ):
            response = await self._invoke(
                _request(
                    "summarize this",
                    files=[UploadedFile(name="data.csv", storage_path="data.csv")],
                )
            )

        self.assertEqual(response.artifacts, artifacts)

    async def test_separate_thread_session_state(self):
        graph = build_graph(checkpointer=InMemorySaver())
        with patch.dict(
            AGENTS,
            {
                AgentName.GENERAL_CHAT: _handler(AgentName.GENERAL_CHAT),
                AgentName.CODE_DEV: _handler(AgentName.CODE_DEV),
            },
        ):
            await self._invoke(_request("debug python"), thread_id="thread-a", graph=graph)
            await self._invoke(_request("hello"), thread_id="thread-b", graph=graph)

        state_a = await graph.aget_state({"configurable": {"thread_id": "thread-a"}})
        state_b = await graph.aget_state({"configurable": {"thread_id": "thread-b"}})

        self.assertEqual(state_a.values["active_agent"], AgentName.CODE_DEV)
        self.assertEqual(state_b.values["active_agent"], AgentName.GENERAL_CHAT)


if __name__ == "__main__":
    unittest.main()
