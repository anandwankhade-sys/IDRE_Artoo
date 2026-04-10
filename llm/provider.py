"""
Multi-Provider LLM Support
==========================
Supports: AWS Bedrock, OpenAI, Google Gemini
Use: get_llm() for structured output, get_llm_chain() for text generation
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from config.settings import settings


@lru_cache(maxsize=1)
def get_llm():
    """Get the configured LLM with structured output support."""
    provider = settings.llm_provider.lower()

    if provider == "bedrock":
        from llm.bedrock_client import get_llm as get_bedrock
        return get_bedrock()

    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_model_id,
            temperature=settings.openai_temperature,
            max_tokens=settings.openai_max_tokens,
        )

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model_id,
            temperature=settings.gemini_temperature,
            max_output_tokens=settings.gemini_max_tokens,
        )

    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")


@lru_cache(maxsize=1)
def get_llm_chain():
    """Get LLM configured for simple text-to-text chains."""
    from langchain.prompts import PromptTemplate
    from langchain.chains import LLMChain

    llm = get_llm()
    prompt = PromptTemplate(input_variables=["text"], template="{text}")
    return LLMChain(llm=llm, prompt=prompt)


def get_provider_info() -> dict:
    """Return info about the currently active provider."""
    provider = settings.llm_provider.lower()

    info = {
        "provider": provider,
        "model": None,
        "status": "configured",
    }

    if provider == "bedrock":
        info["model"] = settings.bedrock_model_id
        info["has_credentials"] = bool(settings.aws_access_key_id or settings.aws_profile)

    elif provider == "openai":
        info["model"] = settings.openai_model_id
        info["has_credentials"] = bool(settings.openai_api_key.get_secret_value())

    elif provider == "gemini":
        info["model"] = settings.gemini_model_id
        info["has_credentials"] = bool(settings.gemini_api_key.get_secret_value())

    return info
