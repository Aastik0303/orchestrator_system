"""Model gateway: reproducibility, single-flight, model availability, rate
limits and budget reservation."""

import _env  # noqa: F401  (must be first)

import asyncio
import os
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from app.config import get_settings
from app.core.budget import Budget, ExecutionLimits
from app.core.errors import ModelError, RateLimitError
from app.infra.cache import reset_backends
from app.llm import client as client_module
from app.llm.client import LLMClient, LLMResponse


@contextmanager
def settings(**values: str):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


class CountingProvider:
    """Returns a different answer on every call, like a sampling model."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[dict] = []
        self.delay = delay

    async def __call__(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        await asyncio.sleep(self.delay)
        return LLMResponse(text=f"answer {len(self.calls)}", model=kwargs["model"], prompt_tokens=10, completion_tokens=5)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_backends()
        self.client = LLMClient()

    def tearDown(self):
        reset_backends()

    async def test_deterministic_mode_forces_temperature_zero_seed_and_cache(self):
        provider = CountingProvider()
        with settings(LLM_DETERMINISTIC="true", LLM_SEED="7"), patch.object(self.client, "_call_provider", provider):
            first = await self.client.complete(system="s", user="u", temperature=0.9)
            second = await self.client.complete(system="s", user="u", temperature=0.9)
        self.assertEqual(provider.calls[0]["temperature"], 0.0)
        self.assertEqual(provider.calls[0]["seed"], 7)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(first.text, second.text)
        self.assertTrue(second.cached)

    async def test_non_deterministic_mode_keeps_agent_temperature(self):
        provider = CountingProvider()
        with settings(LLM_DETERMINISTIC="false"), patch.object(self.client, "_call_provider", provider):
            await self.client.complete(system="s", user="u", temperature=0.4)
            await self.client.complete(system="s", user="u", temperature=0.4)
        self.assertEqual([call["temperature"] for call in provider.calls], [0.4, 0.4])
        self.assertIsNone(provider.calls[0]["seed"])

    async def test_concurrent_identical_prompts_share_one_call(self):
        provider = CountingProvider(delay=0.05)
        with settings(LLM_DETERMINISTIC="true"), patch.object(self.client, "_call_provider", provider):
            responses = await asyncio.gather(*(self.client.complete(system="s", user="same") for _ in range(5)))
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual({response.text for response in responses}, {"answer 1"})

    async def test_followers_retry_when_the_leader_fails(self):
        attempts = {"count": 0}

        async def flaky(**kwargs):
            attempts["count"] += 1
            await asyncio.sleep(0.02)
            if attempts["count"] == 1:
                raise ModelError("bad request", retryable=False)
            return LLMResponse(text="recovered", model=kwargs["model"], prompt_tokens=1, completion_tokens=1)

        with settings(LLM_DETERMINISTIC="true"), patch.object(self.client, "_call_provider", flaky):
            results = await asyncio.gather(
                self.client.complete(system="s", user="x"), self.client.complete(system="s", user="x"), return_exceptions=True
            )
        self.assertIsInstance(results[0], ModelError)
        self.assertEqual(results[1].text, "recovered")

    async def test_budget_reservations_prevent_parallel_overshoot(self):
        budget = Budget(ExecutionLimits(max_steps=10, max_runtime_seconds=60, max_tool_calls=10, max_tokens=1000, max_retries=2, max_llm_calls=10, max_parallel_steps=4))
        first = budget.reserve_llm_call(800)
        second = budget.reserve_llm_call(800)
        self.assertEqual((first, second), (800, 200))
        budget.release_reservation(first)
        budget.record_tokens(300)
        self.assertEqual(budget.remaining_tokens, 500)


class ProviderErrorTests(unittest.IsolatedAsyncioTestCase):
    def completion(self, text: str):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
        )

    async def test_decommissioned_model_falls_back_and_is_reported_clearly(self):
        class Decommissioned(Exception):
            status_code = 400

            def __str__(self):
                return "Error code: 400 - {'error': {'code': 'model_decommissioned'}}"

        calls = []

        def create(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "old-model":
                raise Decommissioned()
            return self.completion("<think>hidden reasoning</think>Hello")

        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        client = LLMClient()
        with settings(LLM_PROVIDER="groq", GROQ_API_KEY="gsk_test", GROQ_FALLBACK_MODEL="new-model"), patch.object(client_module, "_groq_client", return_value=fake):
            response = await client._call_provider(system="s", user="u", model="old-model", max_tokens=50, temperature=0, json_mode=False)
        self.assertEqual(calls, ["old-model", "new-model"])
        self.assertEqual((response.text, response.model), ("Hello", "new-model"))

        with settings(LLM_PROVIDER="groq", GROQ_API_KEY="gsk_test", GROQ_FALLBACK_MODEL=""), patch.object(client_module, "_groq_client", return_value=fake):
            with self.assertRaises(ModelError) as raised:
                await client._call_provider(system="s", user="u", model="old-model", max_tokens=50, temperature=0, json_mode=False)
        self.assertIn("not available from the provider", str(raised.exception))
        self.assertFalse(raised.exception.is_retryable)

    async def test_rate_limit_waits_for_retry_after(self):
        client = LLMClient()
        attempts = {"count": 0}

        async def limited(**kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RateLimitError("slow down", details={"retry_after": 1.5})
            return LLMResponse(text="ok", model=kwargs["model"], prompt_tokens=1, completion_tokens=1)

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with settings(LLM_DETERMINISTIC="false"), patch.object(client, "_call_provider", limited), patch.object(client_module.asyncio, "sleep", fake_sleep):
            response = await client.complete(system="s", user="u")
        self.assertEqual(response.text, "ok")
        self.assertGreaterEqual(sleeps[0], 1.5)


if __name__ == "__main__":
    unittest.main()
