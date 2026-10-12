"""Command line: `mcp-agent chat`, `mcp-agent login`, `mcp-agent eval`."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from rich.console import Console

from mcp_agent.connections import demo_connection, http_connection, mcp_tools
from mcp_agent.graph import ApprovalPolicy, build_graph
from mcp_agent.llm import DEFAULT_MODELS, Provider, build_model

app = typer.Typer(add_completion=False, help="A LangGraph agent for MCP servers.")
console = Console()

DEMO_SEED = [
    {"id": "t1", "title": "Renew domain", "priority": "high", "dueDate": "2026-10-16"},
    {"id": "t2", "title": "Send invoice to ACME", "priority": "medium", "notes": "October hours"},
    {"id": "t3", "title": "Water the plants", "priority": "low"},
]

ServerOpt = Annotated[str, typer.Option(help='"demo" for the local stdio server, or an https://…/mcp URL.')]
ProviderOpt = Annotated[str, typer.Option(help="anthropic | ollama")]
ModelOpt = Annotated[str | None, typer.Option(help="Model name; defaults per provider.")]


def _connection(server: str) -> dict[str, Any]:
    return demo_connection(DEMO_SEED) if server == "demo" else http_connection(server)


def _provider(value: str) -> Provider:
    if value not in DEFAULT_MODELS:
        raise typer.BadParameter(f"provider must be one of {list(DEFAULT_MODELS)}")
    return value  # type: ignore[return-value]


@app.command()
def chat(
    server: ServerOpt = "demo",
    provider: ProviderOpt = "ollama",
    model: ModelOpt = None,
    approval: Annotated[str, typer.Option(help="ask | auto | deny")] = "ask",
) -> None:
    """Talk to the agent. Destructive tool calls ask for your approval first."""
    asyncio.run(_chat(server, _provider(provider), model, approval))  # type: ignore[arg-type]


async def _chat(server: str, provider: Provider, model: str | None, approval: ApprovalPolicy) -> None:
    llm = build_model(provider, model)
    async with mcp_tools(_connection(server)) as (session, tools):
        graph = build_graph(llm, tools, approval=approval, checkpointer=InMemorySaver())
        config: RunnableConfig = {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 40}
        console.print(
            f"[dim]{len(tools)} tools from {server} · "
            f"{provider}:{model or DEFAULT_MODELS[provider]} · Ctrl-D to quit[/dim]"
        )
        while True:
            try:
                text = console.input("[bold]you ›[/bold] ").strip()
            except EOFError:
                return
            if not text:
                continue
            state = await graph.ainvoke({"messages": [HumanMessage(text)]}, config)
            while "__interrupt__" in state:
                request = state["__interrupt__"][0].value
                decisions = {}
                for call in request["tool_calls"]:
                    target = await _describe_target(session, call["args"])
                    console.print(f"[yellow]⚠ {call['name']}[/yellow] {json.dumps(call['args'])}{target}")
                    decisions[call["id"]] = typer.confirm("  Allow?", default=False)
                state = await graph.ainvoke(Command(resume=decisions), config)
            last = state["messages"][-1]
            if isinstance(last, AIMessage):
                console.print(f"[bold green]agent ›[/bold green] {last.text}")


async def _describe_target(session: Any, args: dict[str, Any]) -> str:
    """Show the human what an id points at; approving "delete t1" blind is not consent."""
    if "id" not in args:
        return ""
    result = await session.call_tool("get_task", {"id": str(args["id"])})
    if result.isError:
        return " [red](no such task)[/red]"
    title = json.loads(result.content[0].text).get("title", "?")
    return f' → [bold]"{title}"[/bold]'


@app.command()
def login(url: str) -> None:
    """Authorize against a remote MCP server (opens your browser) and store the tokens."""

    async def run() -> None:
        async with mcp_tools(http_connection(url)) as (_, tools):
            console.print(f"[green]Connected.[/green] {len(tools)} tools: {', '.join(t.name for t in tools)}")

    asyncio.run(run())


@app.command()
def logout(url: str) -> None:
    """Forget the stored client registration and tokens for a server."""
    from mcp_agent.oauth import FileTokenStorage

    FileTokenStorage(url).clear()
    console.print("Signed out.")


@app.command("eval")
def eval_command(
    provider: ProviderOpt = "ollama",
    model: ModelOpt = None,
    cases: Annotated[Path, typer.Option(help="YAML file with golden cases.")] = Path("evals/cases.yaml"),
    repeats: Annotated[int, typer.Option(min=1, help="Attempts per case, to measure variance.")] = 1,
    only: Annotated[str | None, typer.Option(help="Run only cases whose id contains this.")] = None,
    out: Annotated[Path | None, typer.Option(help="Write the Markdown report here.")] = None,
) -> None:
    """Run the golden-conversation suite against the demo server and score it."""
    from mcp_agent.evals import load_cases, run_suite, to_markdown

    chosen = [c for c in load_cases(cases) if not only or only in c.id]
    p = _provider(provider)
    label = f"{p}:{model or DEFAULT_MODELS[p]}"

    def show(r: Any) -> None:
        mark = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
        console.print(f"{mark} {r.case_id} #{r.attempt} ({r.seconds:.1f}s)")
        for check in r.checks:
            if not check.passed:
                console.print(f"     [red]✗[/red] {check.name} {check.detail}")
        if r.error:
            console.print(f"     [red]error[/red] {r.error}")

    report = asyncio.run(run_suite(build_model(p, model), label, chosen, repeats, show))
    markdown = to_markdown(report, repeats)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown)
        console.print(f"Report written to {out}")
    passed = sum(r.passed for r in report.results)
    console.print(f"[bold]{passed}/{len(report.results)} attempts passed[/bold]")
    raise typer.Exit(0 if passed == len(report.results) else 1)


if __name__ == "__main__":
    app()
