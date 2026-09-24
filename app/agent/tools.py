"""Tool registry shared by builtin tools (music, TTS) and dynamically loaded plugins.

A Tool is deliberately provider-agnostic: `parameters` is a JSON Schema object (the format
both the OpenAI-style function-calling convention and Ollama's tool-calling API expect), and
`func` is a plain Python callable taking keyword arguments and returning a string that gets fed
back to the LLM as the tool result.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., str]
    source: str = "builtin"  # "builtin" | "plugin"
    # If true, this tool's result is fetched automatically once per agent loop iteration and
    # injected directly into the input context - the agent does not need to call it itself.
    # It stays additionally callable as a regular tool, e.g. to re-run it with different args.
    context: bool = False
    context_args: dict[str, Any] = field(default_factory=dict)
    # Plugins that *do* something (lights) rather than look something up: the dispatch desk runs
    # them as direct actions shown in the call, and never caches their result.
    action: bool = False

    def to_schema(self) -> dict[str, Any]:
        """OpenAI/Ollama-compatible function schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            logger.warning("Overwriting tool '%s' (source=%s)", tool.name, tool.source)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def to_schemas(self) -> list[dict[str, Any]]:
        return [t.to_schema() for t in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'"
        try:
            result = tool.func(**arguments)
        except Exception as exc:  # tool errors must not crash the agent loop
            logger.exception("Tool '%s' raised an exception", name)
            return f"Error running tool '{name}': {exc}"
        return result if isinstance(result, str) else str(result)
