# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import threading
from tenacity import retry, stop_after_attempt, wait_exponential


# ── Rate-limit circuit breaker ───────────────────────────────────────────────
# When a 429 (rate limit) error is detected, the breaker trips globally.
# All subsequent LLM calls will immediately raise RateLimitBreaker instead
# of continuing to accumulate 429 errors across every ticket.

class RateLimitBreaker(Exception):
    """Raised when the circuit breaker has tripped due to a rate-limit error."""
    def __init__(self, provider: str, original_error: str):
        self.provider = provider
        self.original_error = original_error
        super().__init__(
            f"RATE LIMIT CIRCUIT BREAKER TRIPPED for {provider}. "
            f"All processing stopped. Original error: {original_error}"
        )


class _CircuitBreaker:
    """Global circuit breaker — trips on first rate-limit error, blocks all further calls."""

    def __init__(self):
        self._tripped = False
        self._lock = threading.Lock()
        self._provider: str = ""
        self._error: str = ""

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    def trip(self, provider: str, error: str) -> None:
        with self._lock:
            if not self._tripped:
                self._tripped = True
                self._provider = provider
                self._error = error

    def check(self) -> None:
        """Raise RateLimitBreaker if the breaker has tripped."""
        if self._tripped:
            raise RateLimitBreaker(self._provider, self._error)

    def reset(self) -> None:
        """Reset the breaker (e.g. between model runs if desired)."""
        with self._lock:
            self._tripped = False
            self._provider = ""
            self._error = ""


# Global singleton
circuit_breaker = _CircuitBreaker()


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Check if an exception is a rate-limit (429) error."""
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__.lower()

    # Check common patterns across providers
    if "429" in exc_str or "rate" in exc_str and "limit" in exc_str:
        return True
    if "ratelimit" in exc_type or "toomanyrequests" in exc_type:
        return True
    if "quota" in exc_str and ("exceeded" in exc_str or "exhausted" in exc_str):
        return True
    if "resource_exhausted" in exc_str:
        return True

    # Check HTTP status code attributes
    for attr in ("status_code", "status", "code", "http_status"):
        code = getattr(exc, attr, None)
        if code == 429:
            return True

    return False


# ── LLM retry config ──────────────────────────────────────────────────────────
# Retries up to 3 times with exponential back-off (2 s → 4 s → 8 s … max 30 s).
# On 429 errors, trips the circuit breaker before retrying.
_llm_retry = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
)


def with_llm_retry(fn):
    """Decorator: wrap a callable with LLM retry policy + circuit breaker."""
    retried_fn = _llm_retry(fn)

    def wrapper(*args, **kwargs):
        # Check breaker before attempting
        circuit_breaker.check()
        try:
            return retried_fn(*args, **kwargs)
        except Exception as exc:
            if _is_rate_limit_error(exc):
                # Determine provider from settings
                try:
                    from config.settings import settings
                    provider = settings.llm_provider
                except Exception:
                    provider = "unknown"
                circuit_breaker.trip(provider, str(exc)[:300])
                raise RateLimitBreaker(provider, str(exc)[:300]) from exc
            raise

    return wrapper


# ── MCP / tool ainvoke retry helper ──────────────────────────────────────────

_mcp_retry = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
)


@_mcp_retry
async def ainvoke_with_retry(tool, params: dict):
    """
    Async wrapper that calls ``tool.ainvoke(params)`` with up to 3 retry
    attempts and exponential back-off.  Re-raises the last exception if all
    attempts fail.
    """
    return await tool.ainvoke(params)
