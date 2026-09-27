"""Model gateway.

* One shared provider client per process (HTTP connection reuse) instead of a
  new client per call.
* Async API: the blocking SDK call runs in a worker thread, so the event loop
  is never blocked.
* Model routing: agents ask for a tier ("fast" or "quality"); the gateway maps
  it to a concrete model.
* Budgets: every call reserves an LLM call and output tokens from the run
  budget and records actual usage.
* Error classification with bounded, jittered retries for retryable failures.
* Optional response cache for deterministic (temperature 0) calls.
* Reproducibility (`LLM_DETERMINISTIC=true`, the default): every call uses
  temperature 0 and a fixed seed and goes through the response cache, so an
  identical prompt yields an identical answer even though providers only
  promise best-effort determinism for seeded sampling.
* Model availability: a model the provider no longer serves is reported
  clearly and, when `GROQ_FALLBACK_MODEL` is set, retried once on it.
* `LLM_PROVIDER=fake` gives a deterministic offline provider with configurable
  latency for tests and benchmarks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import threading
from dataclasses import dataclass
from typing import Any, Literal

from app.config import get_settings
from app.core.budget import Budget
from app.core.errors import ErrorType, ModelError, OrchestratorError, RateLimitError, StepTimeoutError, TransientError
from app.infra.cache import get_cache
from app.observability.telemetry import current_telemetry

logger = logging.getLogger("orchestrator.llm")

Tier = Literal["fast", "quality"]


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_CLIENTS: dict[tuple[str, float], Any] = {}
_CLIENTS_LOCK = threading.Lock()


def _groq_client(api_key: str, timeout: float) -> Any:
    key = (hashlib.sha256(api_key.encode()).hexdigest(), timeout)
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            from groq import Groq

            client = Groq(api_key=api_key, timeout=timeout, max_retries=0)
            _CLIENTS[key] = client
        return client


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _is_model_unavailable(exc: Exception) -> bool:
    """The provider does not (or no longer) serve the requested model."""
    text = str(exc).lower()
    if "model_decommissioned" in text or "model_not_found" in text:
        return True
    return getattr(exc, "status_code", None) == 404 and "model" in text


MAX_RATE_LIMIT_WAIT_SECONDS = 20.0


def _retry_after(exc: Exception) -> float | None:
    """Seconds the provider asked us to wait (Retry-After header), if any."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        value = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
    return max(0.0, value)


def _uses_reasoning_effort(model: str) -> bool:
    return model.startswith("openai/gpt-oss")


def _classify_provider_error(exc: Exception, *, model: str | None = None) -> OrchestratorError:
    try:
        import groq
    except ImportError:  # pragma: no cover
        groq = None  # type: ignore[assignment]
    name = exc.__class__.__name__
    if _is_model_unavailable(exc):
        return ModelError(
            f"Model '{model}' is not available from the provider (decommissioned or not found). "
            "Set GROQ_MODEL / GROQ_MODEL_FAST to a served model or configure GROQ_FALLBACK_MODEL.",
            retryable=False,
        )
    if groq is not None:
        if isinstance(exc, groq.RateLimitError):
            return RateLimitError("Model provider rate limit reached.", details={"retry_after": _retry_after(exc)})
        if isinstance(exc, groq.APITimeoutError):
            return StepTimeoutError("Model provider request timed out.")
        if isinstance(exc, groq.APIConnectionError):
            return TransientError("Model provider connection failed.")
        if isinstance(exc, groq.InternalServerError):
            return TransientError("Model provider returned a server error.")
        if isinstance(exc, (groq.AuthenticationError, groq.PermissionDeniedError)):
            return ModelError("Model provider rejected the credentials.", retryable=False)
        if isinstance(exc, groq.APIStatusError):
            return ModelError(f"Model provider rejected the request ({name}).", retryable=False)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return StepTimeoutError("Model provider request timed out.")
    if isinstance(exc, ConnectionError):
        return TransientError("Model provider connection failed.")
    return ModelError(f"Model call failed ({name}).", retryable=False)


class LLMClient:
    MAX_ATTEMPTS = 3

    def __init__(self) -> None:
        # Single-flight: identical cacheable prompts in flight at the same time
        # share one provider call (otherwise concurrent identical requests all
        # miss the cache and may get different answers). Keyed per event loop.
        self._inflight: dict[tuple[int, str], asyncio.Future[tuple[str, str] | None]] = {}

    def available(self) -> bool:
        settings = get_settings()
        provider = settings.llm_provider.lower()
        if provider == "fake":
            return True
        if provider == "groq":
            return bool(settings.groq_api_key)
        return False

    def model_for(self, tier: Tier) -> str:
        settings = get_settings()
        if settings.llm_provider.lower() == "fake":
            return f"fake-{tier}"
        return settings.groq_model_fast if tier == "fast" else settings.groq_model

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: Tier = "quality",
        max_tokens: int | None = None,
        temperature: float = 0.3,
        name: str = "completion",
        budget: Budget | None = None,
        cacheable: bool = False,
        json_mode: bool = False,
    ) -> LLMResponse | None:
        """Return a completion, or None when no provider is configured."""
        if not self.available():
            return None
        settings = get_settings()
        model = self.model_for(tier)
        requested = max_tokens or settings.llm_max_output_tokens
        telemetry = current_telemetry.get()
        seed: int | None = None
        if settings.llm_deterministic:
            temperature = 0.0
            seed = settings.llm_seed
            cacheable = True

        cache_key = None
        flight: asyncio.Future[tuple[str, str] | None] | None = None
        flight_key: tuple[int, str] | None = None
        if cacheable and temperature == 0:
            cache_key = "llm:" + hashlib.sha256(
                json.dumps([settings.llm_provider.lower(), model, system, user, requested, json_mode, seed]).encode()
            ).hexdigest()
            loop = asyncio.get_running_loop()
            flight_key = (id(loop), cache_key)
            while True:
                cached = get_cache().get(cache_key)
                if cached is not None:
                    payload = json.loads(cached)
                    return self._shared_response(payload["text"], payload.get("model", model), name, tier, telemetry)
                leader = self._inflight.get(flight_key)
                if leader is None:
                    break
                shared = await asyncio.shield(leader)
                if shared is not None:
                    return self._shared_response(shared[0], shared[1], name, tier, telemetry)
                # The leader failed or was capped by its budget: try again.
            flight = loop.create_future()
            self._inflight[flight_key] = flight

        shared_result: tuple[str, str] | None = None
        allowed_tokens = requested
        try:
            allowed_tokens = budget.reserve_llm_call(requested) if budget else requested
            try:
                response = await self._complete_with_retries(
                    system=system,
                    user=user,
                    model=model,
                    tier=tier,
                    name=name,
                    allowed_tokens=allowed_tokens,
                    temperature=temperature,
                    seed=seed,
                    json_mode=json_mode,
                    budget=budget,
                    # A budget-capped (possibly truncated) answer must not be served
                    # later for the same prompt with a full budget.
                    cache_key=cache_key if allowed_tokens >= requested else None,
                    telemetry=telemetry,
                )
            finally:
                if budget:
                    budget.release_reservation(allowed_tokens)
            if allowed_tokens >= requested:
                shared_result = (response.text, response.model)
            return response
        finally:
            if flight is not None and flight_key is not None:
                self._inflight.pop(flight_key, None)
                if not flight.done():
                    flight.set_result(shared_result)

    @staticmethod
    def _shared_response(text: str, model: str, name: str, tier: Tier, telemetry: Any) -> LLMResponse:
        """A response served from the cache or from an identical in-flight call."""
        if telemetry:
            with telemetry.span_sync("llm", name, model=model, tier=tier, cached=True, total_tokens=0):
                pass
        return LLMResponse(text=text, model=model, prompt_tokens=0, completion_tokens=0, cached=True)

    async def _complete_with_retries(
        self,
        *,
        system: str,
        user: str,
        model: str,
        tier: Tier,
        name: str,
        allowed_tokens: int,
        temperature: float,
        seed: int | None,
        json_mode: bool,
        budget: Budget | None,
        cache_key: str | None,
        telemetry: Any,
    ) -> LLMResponse:
        settings = get_settings()
        attempt = 0
        while True:
            attempt += 1
            span_cm = telemetry.span("llm", name, model=model, tier=tier) if telemetry else _null_span()
            try:
                async with span_cm as span:
                    response = await self._call_provider(
                        system=system,
                        user=user,
                        model=model,
                        max_tokens=allowed_tokens,
                        temperature=temperature,
                        json_mode=json_mode,
                        seed=seed,
                        reasoning_effort=(
                            settings.groq_reasoning_effort_fast if tier == "fast" else settings.groq_reasoning_effort
                        ),
                    )
                    if span is not None:
                        span.set(
                            prompt_tokens=response.prompt_tokens,
                            completion_tokens=response.completion_tokens,
                            total_tokens=response.total_tokens,
                            retry_count=attempt - 1,
                        )
                if budget:
                    budget.record_tokens(response.total_tokens)
                if cache_key:
                    get_cache().set(
                        cache_key,
                        json.dumps({"text": response.text, "model": response.model}),
                        settings.llm_cache_ttl_seconds,
                    )
                return response
            except OrchestratorError as error:
                can_retry = (
                    error.is_retryable
                    and attempt < self.MAX_ATTEMPTS
                    and (budget.use_retry() if budget else True)
                    and (budget is None or budget.remaining_seconds > 1)
                )
                logger.warning(
                    "llm_call_failed",
                    extra={"error_type": error.error_type.value, "status": "retrying" if can_retry else "failed"},
                )
                if not can_retry:
                    raise
                delay = min(4.0, 0.4 * (2 ** (attempt - 1))) * random.uniform(0.5, 1.0)
                if error.error_type == ErrorType.RATE_LIMIT:
                    # Honour the provider's Retry-After; guessing too short a
                    # wait just burns the remaining attempts.
                    delay = min(MAX_RATE_LIMIT_WAIT_SECONDS, max(delay * 2, error.details.get("retry_after") or 0.0))
                if budget:
                    delay = min(delay, max(0.0, budget.remaining_seconds - 1))
                await asyncio.sleep(delay)

    async def _call_provider(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
        seed: int | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        settings = get_settings()
        if settings.llm_provider.lower() == "fake":
            return await _fake_completion(system=system, user=user, model=model, max_tokens=max_tokens)

        client = _groq_client(settings.groq_api_key or "", settings.llm_request_timeout_seconds)
        kwargs: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if seed is not None:
            kwargs["seed"] = seed
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        candidates = [model]
        if settings.groq_fallback_model and settings.groq_fallback_model != model:
            candidates.append(settings.groq_fallback_model)
        for index, candidate in enumerate(candidates):
            call_kwargs = {**kwargs, "model": candidate}
            if reasoning_effort and _uses_reasoning_effort(candidate):
                call_kwargs["reasoning_effort"] = reasoning_effort
            try:
                completion = await asyncio.to_thread(client.chat.completions.create, **call_kwargs)
            except OrchestratorError:
                raise
            except Exception as exc:
                if _is_model_unavailable(exc) and index + 1 < len(candidates):
                    logger.warning("llm_model_unavailable", extra={"status": "fallback"})
                    continue
                raise _classify_provider_error(exc, model=candidate) from None
            model = candidate
            break
        choice = completion.choices[0]
        # Reasoning models may inline their chain of thought; never return it.
        text = THINK_RE.sub("", choice.message.content or "").strip()
        usage = getattr(completion, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) or estimate_tokens(system + user)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) or estimate_tokens(text)
        if not text:
            if getattr(choice, "finish_reason", None) == "length":
                # Not retryable: the same budget would be exhausted again.
                raise ModelError(
                    "Model used its whole output token budget (reasoning) before answering.", retryable=False
                )
            raise ModelError("Model returned an empty completion.", retryable=True)
        return LLMResponse(text=text, model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)

class _null_span:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


async def _fake_completion(*, system: str, user: str, model: str, max_tokens: int) -> LLMResponse:
    """Deterministic offline completion used for tests and benchmarks."""
    latency_ms = get_settings().fake_llm_latency_ms
    if latency_ms > 0:
        await asyncio.sleep(latency_ms / 1000)
    first_line = " ".join(user.split())[:160]
    text = (
        f"Simulated response ({model}). The request was: {first_line}\n\n"
        "- This answer was produced by the offline fake provider.\n"
        "- Configure GROQ_API_KEY and LLM_PROVIDER=groq for real model output."
    )
    words = text.split()
    if len(words) > max_tokens:
        text = " ".join(words[:max_tokens])
    return LLMResponse(
        text=text,
        model=model,
        prompt_tokens=estimate_tokens(system + user),
        completion_tokens=estimate_tokens(text),
    )


llm_client = LLMClient()
