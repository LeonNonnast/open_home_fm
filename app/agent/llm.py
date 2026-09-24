"""LLM provider abstraction so the agent loop can swap Ollama (local or Ollama Cloud) for
Anthropic (or anything else) purely via `llm.provider` in config.yaml.

The agent loop only ever talks to `LLMProvider.chat(messages, tools) -> LLMMessage`, using the
uniform, OpenAI/Ollama-shaped `LLMMessage`/`ToolCall` types below - each concrete provider is
responsible for translating to/from its own wire format.
"""
from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool" messages
    name: str | None = None  # tool name, set on role="tool" messages


class LLMProvider(ABC):
    @abstractmethod
    def chat(self, messages: list[LLMMessage], tools: list[dict[str, Any]]) -> LLMMessage:
        ...


class OllamaProvider(LLMProvider):
    def __init__(self, model: str, host: str | None = None):
        import ollama

        headers = {}
        api_key = os.environ.get("OLLAMA_API_KEY")
        if host and "ollama.com" in host and api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self.model = model
        self.client = ollama.Client(host=host or None, headers=headers or None)

    def chat(self, messages: list[LLMMessage], tools: list[dict[str, Any]]) -> LLMMessage:
        wire_messages = []
        for m in messages:
            entry: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": tc.name, "arguments": tc.arguments}} for tc in m.tool_calls
                ]
            wire_messages.append(entry)

        response = self._chat_with_retry(wire_messages, tools)
        message = response["message"]

        tool_calls = [
            ToolCall(
                id=tc.get("id", f"call_{i}"),
                name=tc["function"]["name"],
                arguments=tc["function"].get("arguments", {}) or {},
            )
            for i, tc in enumerate(message.get("tool_calls") or [])
        ]
        return LLMMessage(role="assistant", content=message.get("content", "") or "", tool_calls=tool_calls)


    # Ollama Cloud occasionally answers with a transient 5xx mid-run; without a retry that one
    # hiccup throws away the whole generation run.
    RETRY_DELAYS_SECONDS = (2, 5, 10)

    def _chat_with_retry(self, wire_messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        import ollama

        for attempt, delay in enumerate((*self.RETRY_DELAYS_SECONDS, None), start=1):
            try:
                return self.client.chat(model=self.model, messages=wire_messages, tools=tools or None)
            except (ollama.ResponseError, ConnectionError) as exc:
                status = getattr(exc, "status_code", None)
                retryable = status is None or status >= 500 or status == 429
                if delay is None or not retryable:
                    raise
                logger.warning("Ollama request failed (attempt %d, %s), retrying in %ds", attempt, exc, delay)
                time.sleep(delay)


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str):
        import anthropic

        self.model = model
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    @staticmethod
    def _to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

    def chat(self, messages: list[LLMMessage], tools: list[dict[str, Any]]) -> LLMMessage:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        anthropic_messages: list[dict[str, Any]] = []

        i = 0
        non_system = [m for m in messages if m.role != "system"]
        while i < len(non_system):
            m = non_system[i]
            if m.role == "assistant" and m.tool_calls:
                content: list[dict[str, Any]] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                content.extend(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                    for tc in m.tool_calls
                )
                anthropic_messages.append({"role": "assistant", "content": content})
                i += 1
            elif m.role == "tool":
                # Bundle consecutive tool results into one user message, as Anthropic expects.
                tool_results = []
                while i < len(non_system) and non_system[i].role == "tool":
                    tr = non_system[i]
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": tr.tool_call_id, "content": tr.content}
                    )
                    i += 1
                anthropic_messages.append({"role": "user", "content": tool_results})
            else:
                anthropic_messages.append({"role": m.role, "content": m.content})
                i += 1

        response = self.client.messages.create(
            model=self.model,
            system=system,
            messages=anthropic_messages,
            tools=self._to_anthropic_tools(tools) if tools else [],
            max_tokens=1024,
        )

        text_parts = [b.text for b in response.content if b.type == "text"]
        tool_calls = [
            ToolCall(id=b.id, name=b.name, arguments=b.input)
            for b in response.content
            if b.type == "tool_use"
        ]
        return LLMMessage(role="assistant", content="".join(text_parts), tool_calls=tool_calls)


def create_llm_provider(config: dict[str, Any]) -> LLMProvider:
    llm_cfg = config.get("llm", {})
    provider = llm_cfg.get("provider", "ollama")

    if provider == "ollama":
        ollama_cfg = llm_cfg.get("ollama", {})
        return OllamaProvider(model=ollama_cfg.get("model", "llama3.1"), host=ollama_cfg.get("host"))
    if provider == "anthropic":
        anthropic_cfg = llm_cfg.get("anthropic", {})
        return AnthropicProvider(model=anthropic_cfg.get("model", "claude-sonnet-5"))

    raise ValueError(f"Unknown LLM provider: {provider}")
