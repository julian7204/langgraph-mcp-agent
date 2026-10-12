from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedModel(BaseChatModel):
    """Replays a fixed list of AI messages and records what it was shown."""

    script: list[AIMessage]
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedModel:  # type: ignore[override]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        if not self.script:
            raise AssertionError("ScriptedModel ran out of replies")
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def call(name: str, call_id: str = "c1", **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def say(text: str = "", *calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content=text, tool_calls=list(calls))
