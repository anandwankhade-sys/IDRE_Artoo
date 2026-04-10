# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
llm/bedrock_client.py — Multi-provider LLM factory with rate limiting
=====================================================================

Supports three providers: Bedrock (Claude), OpenAI, Gemini.
Provider is selected via the LLM_PROVIDER env variable.

Rate limiting uses a simple token-bucket approach: tracks the last call
timestamp and enforces a minimum inter-call delay to stay under provider
rate limits.
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from config.settings import settings

# ── Rate limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Simple token-bucket rate limiter.

    Enforces a minimum delay between LLM calls to avoid 429 errors.
    Thread-safe.
    """

    def __init__(self, min_delay_seconds: float = 1.0):
        self._min_delay = min_delay_seconds
        self._last_call: float = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until enough time has passed since the last call."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_delay:
                sleep_time = self._min_delay - elapsed
                time.sleep(sleep_time)
            self._last_call = time.monotonic()


# Per-provider rate limiters with conservative defaults
# Bedrock: ~50 RPM for Sonnet → 1.2s between calls
# OpenAI: ~60 RPM for GPT-4o+ → 1.0s between calls
# Gemini: ~15 RPM for Pro → 4.0s between calls
_RATE_LIMITERS: dict[str, RateLimiter] = {
    "bedrock": RateLimiter(min_delay_seconds=1.2),
    "openai": RateLimiter(min_delay_seconds=1.0),
    "gemini": RateLimiter(min_delay_seconds=4.0),
}


def get_rate_limiter(provider: str | None = None) -> RateLimiter:
    """Get the rate limiter for the current or specified provider."""
    provider = (provider or settings.llm_provider).lower().strip()
    return _RATE_LIMITERS.get(provider, _RATE_LIMITERS["bedrock"])


# ── LLM factory ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_llm() -> BaseChatModel:
    """
    Singleton LLM instance.

    Provider is selected via the LLM_PROVIDER env variable:
      - "bedrock"  — AWS Bedrock / Claude via boto3
      - "openai"   — OpenAI API
      - "gemini"   — Google Gemini API

    Streaming is disabled for compatibility with with_structured_output().
    """
    provider = settings.llm_provider.lower().strip()

    if provider == "openai":
        return _build_openai()
    elif provider == "gemini":
        return _build_gemini()
    else:
        return _build_bedrock()


def get_active_model_id() -> str:
    """Return the model ID currently configured for the active provider."""
    provider = settings.llm_provider.lower().strip()
    if provider == "openai":
        return settings.openai_model_id
    elif provider == "gemini":
        return settings.gemini_model_id
    else:
        return settings.bedrock_model_id


def _build_bedrock() -> BaseChatModel:
    from langchain_aws import ChatBedrock
    import boto3

    kwargs: dict = {
        "model_id": settings.bedrock_model_id,
        "region_name": settings.aws_default_region,
        "model_kwargs": {
            "temperature": settings.bedrock_temperature,
            "max_tokens": settings.bedrock_max_tokens,
            "anthropic_version": "bedrock-2023-05-31",
        },
        "streaming": False,
    }

    # Build an explicit boto3 session so .env credentials take priority over
    # any cached SSO sessions in ~/.aws/
    if settings.aws_profile:
        session = boto3.Session(profile_name=settings.aws_profile)
    elif settings.aws_access_key_id and settings.aws_secret_access_key.get_secret_value():
        session = boto3.Session(
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key.get_secret_value(),
            region_name=settings.aws_default_region,
        )
    else:
        session = boto3.Session()

    from botocore.config import Config
    bedrock_config = Config(read_timeout=180, connect_timeout=10, retries={"max_attempts": 2})
    kwargs["client"] = session.client("bedrock-runtime", region_name=settings.aws_default_region, config=bedrock_config)
    return ChatBedrock(**kwargs)


def _build_openai() -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    if not settings.openai_api_key.get_secret_value():
        raise ValueError(
            "LLM_PROVIDER=openai but OPENAI_API_KEY is not set. "
            "Add it to your .env file."
        )

    return ChatOpenAI(
        model=settings.openai_model_id,
        api_key=settings.openai_api_key.get_secret_value(),
        temperature=settings.openai_temperature,
        max_tokens=settings.openai_max_tokens,
        streaming=False,
    )


def _build_gemini() -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    if not settings.gemini_api_key.get_secret_value():
        raise ValueError(
            "LLM_PROVIDER=gemini but GEMINI_API_KEY is not set. "
            "Add it to your .env file."
        )

    return ChatGoogleGenerativeAI(
        model=settings.gemini_model_id,
        google_api_key=settings.gemini_api_key.get_secret_value(),
        temperature=settings.gemini_temperature,
        max_output_tokens=settings.gemini_max_tokens,
    )
