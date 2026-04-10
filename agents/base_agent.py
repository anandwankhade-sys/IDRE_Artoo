# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from llm.bedrock_client import get_llm, get_rate_limiter, get_active_model_id
from llm.llm_logger import LLMCallRecord, llm_logger
from app_logging.activity_logger import ActivityLogger
from schemas.workflow_state import WorkflowState
from utils.retry import circuit_breaker

# Maximum number of LLM invocations per structured-output call when Bedrock
# returns a None parsed result (parse failure without raising an exception).
_MAX_PARSE_RETRIES = 3


def attempt_json_repair(raw_text: str) -> Optional[dict]:
    """
    Attempt to repair malformed JSON from LLM responses.

    Common issues:
    - Truncated JSON (missing closing braces)
    - Extra text before/after JSON
    - Markdown code blocks wrapping JSON

    Returns repaired dict or None if unrepairable.
    """
    import json
    import re

    if not raw_text or not raw_text.strip():
        return None

    # Try direct parse first
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    # Strip markdown code blocks
    text = raw_text.strip()
    if text.startswith("```"):
        # Extract content between ```json and ``` or ``` and ```
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass

    # Try to find JSON object/array in the text
    for pattern in [r'\{.*\}', r'\[.*\]']:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    # Try adding missing closing braces (common truncation issue)
    if text.startswith("{"):
        open_count = text.count("{") - text.count("}")
        if open_count > 0:
            repaired = text + ("}" * open_count)
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    return None


class BaseAgent(ABC):
    """
    Abstract base class for all SDLC workflow agents.

    Provides:
    - Standardised LLM invocation via invoke_llm_structured()
    - Full LLM call logging (every call captured via llm_logger)
    - Activity event logging
    - Async-to-sync bridge for MCP calls inside synchronous LangGraph nodes
    """

    def __init__(self) -> None:
        self.agent_name = self.__class__.__name__
        self.logger = ActivityLogger(self.agent_name)
        self._llm: Optional[BaseChatModel] = None

    # ── LLM ──────────────────────────────────────────────────────────────────

    @property
    def llm(self) -> BaseChatModel:
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def invoke_llm(
        self,
        system_prompt: str,
        human_prompt: str,
        run_id: str,
        ticket_id: str,
        prompt_template_name: str,
    ) -> tuple[str, str]:
        """
        Invoke LLM with regular text output (no structured parsing).
        Returns (response_text, call_id).

        Every invocation is logged to logs/llm_calls.jsonl and SQLite.
        """
        # Check circuit breaker before any LLM call
        circuit_breaker.check()

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prompt),
        ]

        # Apply rate limiting to avoid 429 errors
        rate_limiter = get_rate_limiter()
        rate_limiter.wait()

        response, record = llm_logger.invoke_and_log(
            llm=self.llm,
            messages=messages,
            run_id=run_id,
            ticket_id=ticket_id,
            agent_name=self.agent_name,
            prompt_template_name=prompt_template_name,
            output_schema_name=None,  # No schema for regular text output
        )

        self.logger.info(
            "llm_call_completed",
            ticket_id=ticket_id,
            run_id=run_id,
            call_id=record.call_id,
            latency_ms=round(record.latency_ms, 1),
            tokens=record.total_token_count,
            parsed_ok=True,  # Text output always "parses" successfully
        )

        # Extract text content from response
        # Gemini returns content as a list of dicts: [{"type": "text", "text": "..."}]
        # Other providers return a plain string
        response_text = ""
        if response and hasattr(response, "content"):
            content = response.content
            if isinstance(content, list):
                response_text = " ".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            else:
                response_text = content or ""
        elif isinstance(response, str):
            response_text = response

        return response_text, record.call_id

    def invoke_llm_structured(
        self,
        system_prompt: str,
        human_prompt: str,
        output_schema: type,
        run_id: str,
        ticket_id: str,
        prompt_template_name: str,
    ) -> tuple[Any, str]:
        """
        Invoke LLM with structured output (Pydantic schema via with_structured_output).
        Returns (parsed_result, call_id).

        Retries up to _MAX_PARSE_RETRIES times when Bedrock returns a None
        parsed result (parse failure without raising an exception).

        Every invocation is logged to logs/llm_calls.jsonl and SQLite.
        """
        # Check circuit breaker before any LLM call
        circuit_breaker.check()

        # Use json_schema method for OpenAI (handles large outputs better);
        # function_calling for Bedrock (Claude tool_use);
        # function_calling for Gemini (json_mode had 50% parse failures - switching to function_calling).
        from config.settings import settings
        provider = settings.llm_provider.lower()
        if provider == "openai":
            so_method = "json_schema"
        elif provider == "gemini":
            so_method = "function_calling"  # Changed from json_mode to fix parse failures
        else:
            so_method = "function_calling"
        llm_structured = self.llm.with_structured_output(
            output_schema, include_raw=True, method=so_method
        )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prompt),
        ]

        parsed_output = None
        last_call_id = None

        # Apply rate limiting to avoid 429 errors
        rate_limiter = get_rate_limiter()

        for attempt in range(1, _MAX_PARSE_RETRIES + 1):
            rate_limiter.wait()
            parsed_output, record = llm_logger.invoke_and_log(
                llm=llm_structured,
                messages=messages,
                run_id=run_id,
                ticket_id=ticket_id,
                agent_name=self.agent_name,
                prompt_template_name=prompt_template_name,
                output_schema_name=output_schema.__name__,
            )
            last_call_id = record.call_id

            self.logger.info(
                "llm_call_completed",
                ticket_id=ticket_id,
                run_id=run_id,
                call_id=record.call_id,
                latency_ms=round(record.latency_ms, 1),
                tokens=record.total_token_count,
                parsed_ok=record.parsed_successfully,
                attempt=attempt,
            )

            if parsed_output is not None:
                break

            # If parsing failed, attempt JSON repair before retrying
            if parsed_output is None and record.raw_response:
                repaired_dict = attempt_json_repair(record.raw_response)
                if repaired_dict:
                    try:
                        parsed_output = output_schema(**repaired_dict)
                        self.logger.info(
                            "llm_json_repair_success",
                            ticket_id=ticket_id,
                            run_id=run_id,
                            schema=output_schema.__name__,
                            attempt=attempt,
                        )
                        break
                    except Exception as repair_err:
                        self.logger.warning(
                            "llm_json_repair_failed",
                            ticket_id=ticket_id,
                            run_id=run_id,
                            schema=output_schema.__name__,
                            attempt=attempt,
                            repair_error=str(repair_err),
                        )

            if attempt < _MAX_PARSE_RETRIES:
                self.logger.warning(
                    "llm_parse_failed_retrying",
                    ticket_id=ticket_id,
                    run_id=run_id,
                    schema=output_schema.__name__,
                    attempt=attempt,
                    parse_error=record.parse_error,
                )
                time.sleep(2 ** attempt)  # 2 s, 4 s back-off

        return parsed_output, last_call_id

    # ── Async bridge ──────────────────────────────────────────────────────────

    def run_async(self, coro) -> Any:
        """
        Run an async coroutine from a synchronous LangGraph node.
        Handles nested event loops (e.g. Jupyter / some test runners).
        """
        try:
            asyncio.get_running_loop()
            # Already inside a running event loop — delegate to a new thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        except RuntimeError:
            # No running event loop — safe to use asyncio.run()
            return asyncio.run(coro)

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def run(self, state: WorkflowState) -> dict:
        """
        Execute agent logic.
        Returns a partial WorkflowState dict to be merged by LangGraph.
        """
        ...
