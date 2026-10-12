"""The agent graph.

    START ─► agent ─┬─► (no tool calls) ─────────────► END
                    ├─► (step budget spent) ─────────► END
                    ├─► (any destructive call) ─► approve ─► tools ─► agent
                    └─► (only safe calls) ──────────────────► tools ─► agent

`approve` pauses the run with `interrupt()` and resumes with the human's decision, so a
destructive action never happens without consent. "Destructive" is read from the MCP tool
annotations the server publishes, not from a hard-coded list in the agent.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

ApprovalPolicy = Literal["ask", "auto", "deny"]

# Prompt v2. Each rule fixes a failure seen in evals/reports/qwen3-8b_prompt-v1.md. A v3 that told the
# model to always list without filters fixed one case but made it complete the wrong task in another,
# so it was reverted; see evals/reports/qwen3-8b_prompt-v3.md and the README.
SYSTEM_PROMPT = """You manage the user's task list through the tools you have.

Finding the right task
- Task ids are opaque strings such as "t1" or "5b9e408e" that only list_tasks returns. Never pass a
  title or a word from the user's message as an id.
- When you need an id, call list_tasks with no filters (or with `query` set to one keyword from the
  user's message) and pick the task whose title matches what the user described.
- If a tool says a task does not exist, call list_tasks and retry once with the real id. If no task
  matches, tell the user you could not find it and change nothing.

Changing tasks
- To complete or finish a task, set its status to "done" with update_task. Only call delete_task
  when the user explicitly asks to delete or remove.
- Make the smallest set of changes that does what the user asked, and only to the tasks they named.
- Act by calling tools. Never end your turn saying you will do something; do it, then report.
- If the user declines an action, say it was not done; do not retry it.

Dates: today is {today}. Use YYYY-MM-DD. The next seven days are {week}.

Finish with one or two short sentences saying what you did."""

DECLINED = "The user declined this action. It was not executed."


def system_prompt(today: dt.date) -> str:
    # Small models cannot reliably do weekday arithmetic, so hand them the calendar.
    week = ", ".join(f"{d:%A} {d.isoformat()}" for d in (today + dt.timedelta(days=i) for i in range(1, 8)))
    return SYSTEM_PROMPT.format(today=f"{today:%A} {today.isoformat()}", week=week)


def is_destructive(tool: BaseTool | None) -> bool:
    """MCP annotation semantics: destructiveHint defaults to true unless the tool is read-only.
    Unknown tools are treated as destructive."""
    if tool is None:
        return True
    meta = tool.metadata or {}
    if meta.get("readOnlyHint") is True:
        return False
    return bool(meta.get("destructiveHint", True))


def _last_ai(messages: Sequence[AnyMessage]) -> AIMessage | None:
    last = messages[-1] if messages else None
    return last if isinstance(last, AIMessage) else None


def _steps_since_user(messages: Sequence[AnyMessage]) -> int:
    steps = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage):
            steps += 1
    return steps


def build_graph(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    *,
    approval: ApprovalPolicy = "ask",
    max_steps: int = 8,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    today: dt.date | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    by_name = {tool.name: tool for tool in tools}
    bound = model.bind_tools(list(tools))
    system = SystemMessage(system_prompt(today or dt.date.today()))

    async def agent(state: MessagesState) -> dict[str, list[AnyMessage]]:
        reply = await bound.ainvoke([system, *state["messages"]])
        return {"messages": [reply]}

    def route(state: MessagesState) -> str:
        last = _last_ai(state["messages"])
        if last is None or not last.tool_calls:
            return END
        if _steps_since_user(state["messages"]) > max_steps:
            return "budget"
        if any(is_destructive(by_name.get(call["name"])) for call in last.tool_calls):
            return "approve"
        return "tools"

    def budget(state: MessagesState) -> dict[str, list[AnyMessage]]:
        last = _last_ai(state["messages"])
        assert last is not None
        stopped: list[AnyMessage] = [
            ToolMessage("Step budget exhausted; not executed.", tool_call_id=call["id"] or "", status="error")
            for call in last.tool_calls
        ]
        stopped.append(
            AIMessage(f"I stopped after {max_steps} steps without finishing. Try a narrower request.")
        )
        return {"messages": stopped}

    def approve(state: MessagesState) -> dict[str, list[AnyMessage]]:
        last = _last_ai(state["messages"])
        assert last is not None
        risky = [c for c in last.tool_calls if is_destructive(by_name.get(c["name"]))]
        if approval == "auto":
            return {"messages": []}
        if approval == "deny":
            decisions = {c["id"]: False for c in risky}
        else:
            # Pauses the graph. The caller resumes with Command(resume={tool_call_id: bool}).
            answer = interrupt({"type": "approval", "tool_calls": [dict(c) for c in risky]})
            decisions = {c["id"]: bool(answer.get(c["id"], False)) for c in risky}
        denied: list[AnyMessage] = [
            ToolMessage(DECLINED, tool_call_id=call_id or "", status="error")
            for call_id, ok in decisions.items()
            if not ok
        ]
        return {"messages": denied}

    async def run_tools(state: MessagesState) -> dict[str, list[AnyMessage]]:
        messages = state["messages"]
        ai = next(m for m in reversed(messages) if isinstance(m, AIMessage))
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        pending = [c for c in ai.tool_calls if c["id"] not in answered]

        async def run(call: Any) -> ToolMessage:
            tool = by_name.get(call["name"])
            if tool is None:
                return ToolMessage(f"Unknown tool {call['name']}.", tool_call_id=call["id"], status="error")
            try:
                result = await tool.ainvoke({**call, "type": "tool_call"})
            except Exception as error:  # a broken tool must not kill the conversation
                return ToolMessage(f"Tool failed: {error}", tool_call_id=call["id"], status="error")
            assert isinstance(result, ToolMessage)
            return result

        # Calls in one turn are independent by construction, so run them concurrently.
        results = await asyncio.gather(*(run(call) for call in pending))
        return {"messages": list(results)}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("approve", approve)
    graph.add_node("tools", run_tools)
    graph.add_node("budget", budget)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["approve", "tools", "budget", END])
    graph.add_edge("approve", "tools")
    graph.add_edge("tools", "agent")
    graph.add_edge("budget", END)
    return graph.compile(checkpointer=checkpointer)
