"""Chat model factory. The graph only needs `bind_tools`, so any LangChain chat model works."""

from __future__ import annotations

import os
from typing import Literal

from langchain_core.language_models import BaseChatModel

Provider = Literal["anthropic", "ollama"]

DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-opus-5-5",
    "ollama": "qwen3:8b",
}


def build_model(provider: Provider, model: str | None = None) -> BaseChatModel:
    name = model or DEFAULT_MODELS[provider]
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # Credentials come from ANTHROPIC_API_KEY. Thinking is adaptive by default on this model;
        # effort is the cost/quality dial. `fallbacks: "default"` lets the API re-route a
        # safety-classifier refusal to a fallback model inside the same call.
        return ChatAnthropic(
            model_name=name,
            max_tokens=16000,  # type: ignore[call-arg]  # pydantic alias hides it from mypy
            output_config={"effort": os.environ.get("MCP_AGENT_EFFORT", "medium")},
            betas=["server-side-fallback-2026-07-01"],
            model_kwargs={"fallbacks": "default"},
            timeout=None,
            stop=None,
        )
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        # reasoning=False keeps qwen3's <think> traces out of the transcript.
        return ChatOllama(model=name, temperature=0, reasoning=False, num_ctx=16384)
    raise ValueError(f"Unknown provider {provider!r}")
