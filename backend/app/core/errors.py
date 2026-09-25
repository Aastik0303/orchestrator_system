"""Structured error taxonomy.

Every failure that crosses an agent/tool/LLM boundary is classified so the
execution engine can decide whether it is worth retrying. Nothing is retried
blindly: only TRANSIENT, RATE_LIMIT and TIMEOUT (and MODEL_ERROR when the
provider says so) are retryable.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ErrorType(str, Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    SECURITY = "SECURITY"
    INVALID_INPUT = "INVALID_INPUT"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    MODEL_ERROR = "MODEL_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"


RETRYABLE_TYPES = {ErrorType.TRANSIENT, ErrorType.RATE_LIMIT, ErrorType.TIMEOUT}


class OrchestratorError(Exception):
    """Base class for classified failures."""

    error_type: ErrorType = ErrorType.PERMANENT
    retryable: bool | None = None

    def __init__(
        self,
        message: str,
        *,
        error_type: ErrorType | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if error_type is not None:
            self.error_type = error_type
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}

    @property
    def is_retryable(self) -> bool:
        if self.retryable is not None:
            return self.retryable
        return self.error_type in RETRYABLE_TYPES


class TransientError(OrchestratorError):
    error_type = ErrorType.TRANSIENT


class InvalidInputError(OrchestratorError):
    error_type = ErrorType.INVALID_INPUT


class SecurityViolation(OrchestratorError):
    error_type = ErrorType.SECURITY


class ToolError(OrchestratorError):
    error_type = ErrorType.TOOL_ERROR


class ModelError(OrchestratorError):
    error_type = ErrorType.MODEL_ERROR


class RateLimitError(OrchestratorError):
    error_type = ErrorType.RATE_LIMIT


class StepTimeoutError(OrchestratorError):
    error_type = ErrorType.TIMEOUT


class BudgetExceeded(OrchestratorError):
    error_type = ErrorType.BUDGET_EXCEEDED
    retryable = False


class RunCancelled(OrchestratorError):
    error_type = ErrorType.CANCELLED
    retryable = False


class StructuredFailure(BaseModel):
    """Serializable description of a failure, safe to persist and return."""

    agent: str | None = None
    step_id: str | None = None
    status: str = "failed"
    error_type: ErrorType
    retryable: bool
    message: str
    attempts: int = 0
    details: dict[str, Any] = Field(default_factory=dict)


def classify_exception(exc: BaseException) -> tuple[ErrorType, bool]:
    """Map any exception to (error_type, retryable)."""
    if isinstance(exc, OrchestratorError):
        return exc.error_type, exc.is_retryable
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ErrorType.TIMEOUT, True
    if isinstance(exc, asyncio.CancelledError):
        return ErrorType.CANCELLED, False
    if isinstance(exc, PermissionError):
        return ErrorType.SECURITY, False
    if isinstance(exc, (ConnectionError, OSError)) and not isinstance(exc, FileNotFoundError):
        return ErrorType.TRANSIENT, True
    if isinstance(exc, (ValueError, KeyError, TypeError, FileNotFoundError)):
        return ErrorType.INVALID_INPUT, False
    return ErrorType.PERMANENT, False
