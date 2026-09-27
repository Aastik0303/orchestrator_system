import _env  # noqa: F401  (must be first)

import unittest
from unittest.mock import AsyncMock, patch

from app.agents.catalog import load_agents
from app.agents.registry import AgentRegistry, AgentSpec, RoutingHints, agent_registry
from app.llm.client import LLMResponse
from app.models import AgentName, AgentResult, ChatRequest, UploadedFile
from app.orchestrator.router import HybridRouter, choose_route, router


def llm_json(text: str) -> AsyncMock:
    return AsyncMock(return_value=LLMResponse(text=text, model="fake-fast", prompt_tokens=10, completion_tokens=10))


class RegistryTests(unittest.TestCase):
    def test_builtin_agents_are_discovered_with_specs(self):
        registry = load_agents()
        for name in ("general_chat", "deep_research", "document_rag", "youtube_rag", "code_dev", "data_analyst", "sql_agent", "python_executor", "report_generator", "evaluation", "memory"):
            spec = registry.get(name)
            self.assertTrue(spec.capabilities)
            self.assertGreater(spec.timeout_seconds, 0)
            self.assertIsNotNone(spec.retry_policy)
            self.assertIsNotNone(spec.model_policy)
            self.assertIsNotNone(spec.permission_policy)

    def test_agents_are_discoverable_by_capability(self):
        registry = load_agents()
        expected = {
            "data_analysis": "data_analyst",
            "web_research": "deep_research",
            "document_retrieval": "document_rag",
            "youtube_transcript": "youtube_rag",
            "code_generation": "code_dev",
            "python_execution": "python_executor",
        }
        for capability, agent in expected.items():
            self.assertEqual(registry.find_by_capability(capability)[0].name, agent)

    def test_new_agent_with_new_capability_routes_without_router_changes(self):
        registry = AgentRegistry()
        for spec in load_agents().all():
            registry.register(spec)

        async def translator(task, ctx):
            return AgentResult(summary="translated")

        registry.register(
            AgentSpec(
                name="translator",
                description="Translates text.",
                capabilities=["translation"],
                handler=translator,
                routing_hints={"translation": RoutingHints(description="Translate text", keywords={"translate": 2.5})},
            )
        )
        decision = HybridRouter(registry=registry).route_rules(ChatRequest(message="Please translate this paragraph to French"))
        self.assertEqual(decision.primary_agent, "translator")
        self.assertEqual(decision.required_capabilities, ["translation"])

    def test_unavailable_agent_is_not_a_candidate(self):
        registry = AgentRegistry()
        for spec in load_agents().all():
            registry.register(spec)
        registry.register(registry.get("code_dev").model_copy(update={"availability": lambda: (False, "disabled")}), replace=True)
        decision = HybridRouter(registry=registry).route_rules(ChatRequest(message="Debug this python function"))
        self.assertNotIn("code_dev", decision.candidate_agents)


class RuleAndCapabilityRoutingTests(unittest.TestCase):
    def test_decision_is_structured(self):
        decision = choose_route(ChatRequest(message="Show sales trends and KPI outliers"))
        payload = decision.model_dump()
        for key in ("intent", "required_capabilities", "candidate_agents", "confidence"):
            self.assertIn(key, payload)
        self.assertEqual(decision.primary_agent, AgentName.DATA_ANALYST)
        self.assertGreaterEqual(decision.confidence, 0.7)

    def test_repository_request_routes_to_code_agent_with_planning(self):
        decision = choose_route(ChatRequest(message="Analyze my GitHub repository architecture"))
        self.assertEqual(decision.primary_agent, AgentName.CODE_DEV)
        self.assertTrue(decision.requires_planning)
        self.assertGreaterEqual(decision.confidence, 0.65)

    def test_research_and_code_route_to_multiple_agents(self):
        decision = choose_route(ChatRequest(message="Research current patterns and refactor this React repository"))
        self.assertEqual(decision.primary_agent, AgentName.CODE_DEV)
        self.assertIn(AgentName.DEEP_RESEARCH, decision.secondary_agents)
        self.assertTrue(decision.requires_planning)

    def test_file_type_rules_precede_message_hints(self):
        decision = choose_route(ChatRequest(message="debug this", files=[UploadedFile(name="metrics.csv", storage_path="metrics.csv")]))
        self.assertEqual(decision.primary_agent, AgentName.DATA_ANALYST)
        self.assertEqual(decision.strategy, "rule")

    def test_document_attachment_routes_to_rag(self):
        decision = choose_route(ChatRequest(message="summarize", files=[UploadedFile(name="policy.pdf")]))
        self.assertEqual(decision.primary_agent, AgentName.DOCUMENT_RAG)

    def test_youtube_url_rule(self):
        decision = choose_route(ChatRequest(message="Summarize https://www.youtube.com/watch?v=abc123"))
        self.assertEqual(decision.primary_agent, AgentName.YOUTUBE_RAG)

    def test_uploaded_file_data_intent_routes_to_data_agent(self):
        self.assertEqual(choose_route(ChatRequest(message="Analyze the uploaded file for data quality")).primary_agent, AgentName.DATA_ANALYST)

    def test_sql_and_dataset_request_plans_companion_data_agent(self):
        decision = choose_route(ChatRequest(message="Write a SQL query and analyze data quality in the results"))
        self.assertEqual(decision.primary_agent, AgentName.SQL_AGENT)
        self.assertIn(AgentName.DATA_ANALYST, decision.secondary_agents)
        self.assertTrue(decision.requires_planning)

    def test_csv_research_report_request_is_multi_capability(self):
        decision = choose_route(ChatRequest(message="Analyze this CSV and research the market, then create a report"))
        self.assertEqual(set(decision.required_capabilities), {"data_analysis", "web_research", "report_synthesis"})
        self.assertTrue(decision.requires_planning)

    def test_code_execution_suppresses_code_generation(self):
        decision = choose_route(ChatRequest(message="Run this python code:\n```python\nprint(1)\n```"))
        self.assertEqual(decision.required_capabilities, ["python_execution"])

    def test_manual_override(self):
        decision = choose_route(ChatRequest(message="anything", agent_override="code_dev"))
        self.assertEqual((decision.primary_agent, decision.strategy, decision.confidence), ("code_dev", "override", 1.0))

    def test_system_agent_manual_override_is_rejected(self):
        for name in ("supervisor", "memory", "evaluation", "report_generator", "does_not_exist"):
            with self.assertRaises(ValueError):
                choose_route(ChatRequest(message="run it", agent_override=name))

    def test_chit_chat_routes_to_conversation_without_llm(self):
        decision = choose_route(ChatRequest(message="hello there"))
        self.assertEqual((decision.primary_agent, decision.strategy), ("general_chat", "capability"))
        self.assertGreaterEqual(decision.confidence, 0.65)

    def test_resume_questions_route_to_rag(self):
        for message in ("summarize my resume", "what skills are listed in this CV", "what does the attached document say"):
            self.assertEqual(choose_route(ChatRequest(message=message)).primary_agent, AgentName.DOCUMENT_RAG, message)

    def test_follow_up_in_chat_with_attached_document_routes_to_rag(self):
        resume = [UploadedFile(name="resume.pdf", document_id="doc_1")]
        for message in ("what are my skills?", "who is Aastik Mishra", "hey, what projects are mentioned in there"):
            decision = choose_route(ChatRequest(message=message, session_files=resume))
            self.assertEqual((decision.primary_agent, decision.strategy), (AgentName.DOCUMENT_RAG, "rule"), message)
        # Short chit-chat and clear specialist requests are unaffected.
        self.assertEqual(choose_route(ChatRequest(message="thanks!", session_files=resume)).primary_agent, "general_chat")
        self.assertEqual(
            choose_route(ChatRequest(message="Write a python function that parses ISO dates", session_files=resume)).primary_agent,
            AgentName.CODE_DEV,
        )

    def test_no_signal_falls_back_to_conversation(self):
        decision = choose_route(ChatRequest(message="could you look into what our rivals shipped lately"))
        self.assertEqual((decision.primary_agent, decision.strategy), ("general_chat", "fallback"))
        self.assertLess(decision.confidence, 0.65)


class LlmRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_routing_is_used_only_for_low_confidence_and_validated(self):
        request = ChatRequest(message="could you look into what our rivals shipped lately")
        with patch.object(router._llm, "complete", llm_json('{"intent":"research","required_capabilities":["web_research"],"confidence":0.99}')):
            decision = await router.route(request)
        self.assertEqual(decision.strategy, "llm")
        self.assertEqual(decision.primary_agent, "deep_research")
        self.assertLessEqual(decision.confidence, 0.9, "LLM confidence must be capped")

    async def test_llm_cannot_select_unknown_or_system_capabilities(self):
        request = ChatRequest(message="could you look into what our rivals shipped lately")
        for payload in (
            '{"required_capabilities":["shell_access"],"confidence":0.95}',
            '{"required_capabilities":["memory_retrieval","validation"],"confidence":0.95}',
            "not json at all",
        ):
            with patch.object(router._llm, "complete", llm_json(payload)):
                decision = await router.route(request)
            self.assertEqual(decision.strategy, "fallback", payload)
            self.assertEqual(decision.primary_agent, "general_chat")

    async def test_low_llm_confidence_is_ignored(self):
        with patch.object(router._llm, "complete", llm_json('{"required_capabilities":["code_generation"],"confidence":0.2}')):
            decision = await router.route(ChatRequest(message="could you look into what our rivals shipped lately"))
        self.assertEqual(decision.strategy, "fallback")

    async def test_llm_router_knows_about_session_documents_and_may_pick_conversation(self):
        request = ChatRequest(message="what is the capital of France", session_files=[UploadedFile(name="resume.pdf")])
        mock = llm_json('{"intent":"chat","required_capabilities":["conversation"],"confidence":0.9}')
        with patch.object(router._llm, "complete", mock):
            decision = await router.route(request)
        self.assertEqual(decision.primary_agent, "general_chat")
        self.assertIn("resume.pdf", mock.call_args.kwargs["system"])
        # Without a usable LLM answer the session documents are used.
        with patch.object(router._llm, "complete", llm_json("not json")):
            decision = await router.route(ChatRequest(message="what are his main skills", session_files=[UploadedFile(name="resume.pdf")]))
        self.assertEqual(decision.primary_agent, "document_rag")

    async def test_confident_rule_decisions_skip_the_llm(self):
        mock = llm_json("{}")
        with patch.object(router._llm, "complete", mock):
            await router.route(ChatRequest(message="Show sales trends and KPI outliers"))
        mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
