from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import ScriptedModel, call, say
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from mcp_agent.connections import demo_connection, mcp_tools
from mcp_agent.graph import DECLINED, build_graph, is_destructive

SEED = [
    {"id": "t1", "title": "Renew domain", "priority": "high"},
    {"id": "t2", "title": "Water the plants", "priority": "low"},
]
CONFIG: dict[str, Any] = {"configurable": {"thread_id": "t"}}


async def ids(session: Any) -> set[str]:
    result = await session.call_tool("list_tasks", {})
    return {t["id"] for t in json.loads(result.content[0].text)["tasks"]}


def tool_messages(state: dict[str, Any]) -> list[ToolMessage]:
    return [m for m in state["messages"] if isinstance(m, ToolMessage)]


async def test_mcp_annotations_drive_the_destructive_flag() -> None:
    async with mcp_tools(demo_connection()) as (_, tools):
        flags = {t.name: is_destructive(t) for t in tools}
    assert flags == {
        "list_tasks": False,
        "get_task": False,
        "create_task": False,
        "update_task": False,
        "delete_task": True,
    }


def test_unannotated_and_unknown_tools_count_as_destructive() -> None:
    bare = StructuredTool.from_function(lambda: "x", name="bare", description="d")
    assert is_destructive(bare) is True
    assert is_destructive(None) is True


async def test_safe_calls_run_without_approval() -> None:
    model = ScriptedModel(script=[say("", call("list_tasks", priority="high")), say("Renew domain.")])
    async with mcp_tools(demo_connection(SEED)) as (_, tools):
        graph = build_graph(model, tools, approval="ask", checkpointer=InMemorySaver())
        state = await graph.ainvoke({"messages": [HumanMessage("high?")]}, CONFIG)
    assert "__interrupt__" not in state
    assert "Renew domain" in tool_messages(state)[0].text
    assert state["messages"][-1].text == "Renew domain."


@pytest.mark.parametrize("allow", [True, False])
async def test_destructive_calls_pause_for_a_human(allow: bool) -> None:
    model = ScriptedModel(script=[say("", call("delete_task", id="t2")), say("Done.")])
    async with mcp_tools(demo_connection(SEED)) as (session, tools):
        graph = build_graph(model, tools, approval="ask", checkpointer=InMemorySaver())
        paused = await graph.ainvoke({"messages": [HumanMessage("delete plants")]}, CONFIG)
        request = paused["__interrupt__"][0].value
        assert request["tool_calls"][0]["name"] == "delete_task"
        assert await ids(session) == {"t1", "t2"}, "nothing runs before the decision"

        state = await graph.ainvoke(Command(resume={"c1": allow}), CONFIG)
        remaining = await ids(session)
    if allow:
        assert remaining == {"t1"}
    else:
        assert remaining == {"t1", "t2"}
        assert tool_messages(state)[0].text == DECLINED


async def test_mixed_turn_runs_safe_call_and_skips_declined_one() -> None:
    model = ScriptedModel(
        script=[say("", call("list_tasks", "a"), call("delete_task", "b", id="t1")), say("ok")]
    )
    async with mcp_tools(demo_connection(SEED)) as (session, tools):
        graph = build_graph(model, tools, approval="deny")
        state = await graph.ainvoke({"messages": [HumanMessage("x")]})
        assert await ids(session) == {"t1", "t2"}
    results = {m.tool_call_id: m for m in tool_messages(state)}
    assert results["b"].text == DECLINED
    assert results["a"].status != "error"


async def test_auto_policy_executes_destructive_calls() -> None:
    model = ScriptedModel(script=[say("", call("delete_task", id="t1")), say("ok")])
    async with mcp_tools(demo_connection(SEED)) as (session, tools):
        await build_graph(model, tools, approval="auto").ainvoke({"messages": [HumanMessage("x")]})
        assert await ids(session) == {"t2"}


async def test_tool_errors_return_to_the_model() -> None:
    model = ScriptedModel(script=[say("", call("get_task", id="nope")), say("Not found.")])
    async with mcp_tools(demo_connection(SEED)) as (_, tools):
        state = await build_graph(model, tools).ainvoke({"messages": [HumanMessage("x")]})
    assert "No task with id nope" in tool_messages(state)[0].text
    assert "nope" in model.seen[-1][-1].text  # the model saw the error


async def test_unknown_tool_is_reported_not_raised() -> None:
    model = ScriptedModel(script=[say("", call("format_disk")), say("Sorry.")])
    async with mcp_tools(demo_connection(SEED)) as (_, tools):
        state = await build_graph(model, tools, approval="auto").ainvoke({"messages": [HumanMessage("x")]})
    assert tool_messages(state)[0].text == "Unknown tool format_disk."


async def test_step_budget_stops_runaway_loops() -> None:
    loop = [say("", call("list_tasks", f"c{i}")) for i in range(10)]
    model = ScriptedModel(script=loop)
    async with mcp_tools(demo_connection(SEED)) as (_, tools):
        state = await build_graph(model, tools, max_steps=3).ainvoke({"messages": [HumanMessage("x")]})
    assert "stopped after 3 steps" in state["messages"][-1].text
    assert len(model.seen) == 4
