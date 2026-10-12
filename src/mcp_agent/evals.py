"""Golden-conversation evals.

Each case seeds the demo server, sends one user message, and then checks what the agent *did*
(which tools, with which arguments, how many calls), what the world looks like afterwards
(the task list), and what it *said*. Checks are deterministic; no LLM grades another LLM.
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from mcp_agent.connections import demo_connection, mcp_tools
from mcp_agent.graph import ApprovalPolicy, build_graph

EVAL_TODAY = dt.date(2026, 10, 12)  # a Monday; cases with relative dates rely on it


class Expect(BaseModel):
    tools_called: list[str] = Field(default_factory=list)
    tools_not_called: list[str] = Field(default_factory=list)
    args: dict[str, dict[str, Any]] = Field(default_factory=dict)
    max_tool_calls: int | None = None
    task_count: int | None = None
    final_state: list[dict[str, Any]] = Field(default_factory=list)
    absent_ids: list[str] = Field(default_factory=list)
    new_task: dict[str, Any] | None = None
    answer_contains_any: list[str] = Field(default_factory=list)


class Case(BaseModel):
    id: str
    category: str
    prompt: str
    seed: list[dict[str, Any]] = Field(default_factory=list)
    approval: ApprovalPolicy = "auto"
    expect: Expect


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    category: str
    attempt: int
    checks: list[Check]
    tool_calls: list[dict[str, Any]]
    answer: str
    seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)


@dataclass
class Report:
    model: str
    results: list[CaseResult] = field(default_factory=list)


def load_cases(path: Path) -> list[Case]:
    return [Case.model_validate(item) for item in yaml.safe_load(path.read_text())]


def _matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, want in expected.items():
        got = actual.get(key)
        if isinstance(want, str) and isinstance(got, str):
            if want.lower() not in got.lower():
                return False
        elif got != want:
            return False
    return True


def score(
    case: Case,
    calls: list[dict[str, Any]],
    answer: str,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> list[Check]:
    e = case.expect
    names = [c["name"] for c in calls]
    checks: list[Check] = []
    for tool in e.tools_called:
        checks.append(Check(f"calls {tool}", tool in names))
    for tool in e.tools_not_called:
        checks.append(Check(f"never calls {tool}", tool not in names))
    for tool, want in e.args.items():
        ok = any(c["name"] == tool and _matches(c["args"], want) for c in calls)
        checks.append(Check(f"{tool} args ⊇ {json.dumps(want)}", ok))
    if e.max_tool_calls is not None:
        checks.append(
            Check(f"≤ {e.max_tool_calls} tool calls", len(calls) <= e.max_tool_calls, str(len(calls)))
        )
    by_id = {t["id"]: t for t in after}
    if e.task_count is not None:
        checks.append(Check(f"{e.task_count} tasks remain", len(after) == e.task_count, str(len(after))))
    for want in e.final_state:
        task = by_id.get(want["id"])
        checks.append(
            Check(f"task {want['id']} ⊇ {json.dumps(want)}", task is not None and _matches(task, want))
        )
    for missing in e.absent_ids:
        checks.append(Check(f"task {missing} deleted", missing not in by_id))
    if e.new_task is not None:
        old = {t["id"] for t in before}
        created = [t for t in after if t["id"] not in old]
        ok = len(created) == 1 and _matches(created[0], e.new_task)
        checks.append(Check(f"one new task ⊇ {json.dumps(e.new_task)}", ok, json.dumps(created)))
    if e.answer_contains_any:
        ok = any(word.lower() in answer.lower() for word in e.answer_contains_any)
        checks.append(Check(f"answer mentions one of {e.answer_contains_any}", ok))
    return checks


async def _snapshot(session: Any) -> list[dict[str, Any]]:
    result = await session.call_tool("list_tasks", {"limit": 100})
    payload: dict[str, Any] = json.loads(result.content[0].text)
    tasks: list[dict[str, Any]] = payload["tasks"]
    return tasks


async def run_case(model: BaseChatModel, case: Case, attempt: int = 1) -> CaseResult:
    started = time.perf_counter()
    async with mcp_tools(demo_connection(case.seed)) as (session, tools):
        before = await _snapshot(session)
        graph = build_graph(model, tools, approval=case.approval, today=EVAL_TODAY)
        try:
            state = await graph.ainvoke({"messages": [HumanMessage(case.prompt)]}, {"recursion_limit": 40})
        except Exception as error:  # record and keep going; one broken case must not stop the suite
            return CaseResult(
                case.id, case.category, attempt, [], [], "", time.perf_counter() - started, error=repr(error)
            )
        after = await _snapshot(session)

    ai = [m for m in state["messages"] if isinstance(m, AIMessage)]
    calls = [{"name": c["name"], "args": c["args"]} for m in ai for c in m.tool_calls]
    answer = ai[-1].text if ai else ""
    usage = [m.usage_metadata for m in ai if m.usage_metadata]
    return CaseResult(
        case_id=case.id,
        category=case.category,
        attempt=attempt,
        checks=score(case, calls, answer, before, after),
        tool_calls=calls,
        answer=answer,
        seconds=time.perf_counter() - started,
        input_tokens=sum(u["input_tokens"] for u in usage),
        output_tokens=sum(u["output_tokens"] for u in usage),
    )


async def run_suite(
    model: BaseChatModel,
    model_label: str,
    cases: list[Case],
    repeats: int = 1,
    on_result: Callable[[CaseResult], None] | None = None,
) -> Report:
    report = Report(model=model_label)
    for case in cases:
        for attempt in range(1, repeats + 1):
            result = await run_case(model, case, attempt)
            report.results.append(result)
            if on_result:
                on_result(result)
    return report


def to_markdown(report: Report, repeats: int) -> str:
    results = report.results
    by_case: dict[str, list[CaseResult]] = {}
    for r in results:
        by_case.setdefault(r.case_id, []).append(r)
    pass_rate = sum(r.passed for r in results) / len(results)
    pass_all = sum(all(r.passed for r in rs) for rs in by_case.values()) / len(by_case)
    checks = [c for r in results for c in r.checks]
    lines = [
        f"# Eval report — `{report.model}`",
        "",
        f"Run on {dt.date.today().isoformat()} · {len(by_case)} cases × {repeats} attempt(s)",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Attempts passed | **{pass_rate:.0%}** ({sum(r.passed for r in results)}/{len(results)}) |",
        f"| Cases passed on every attempt (pass^{repeats}) | **{pass_all:.0%}** |",
        f"| Individual checks passed | {sum(c.passed for c in checks)}/{len(checks)} |",
        f"| Median latency per case | {statistics.median(r.seconds for r in results):.1f} s |",
        f"| Mean tool calls per case | {statistics.mean(len(r.tool_calls) for r in results):.1f} |",
        f"| Tokens in / out (total) | {sum(r.input_tokens for r in results):,} / "
        f"{sum(r.output_tokens for r in results):,} |",
        "",
        "## By category",
        "",
        "| Category | Passed |",
        "|---|---|",
    ]
    categories: dict[str, list[CaseResult]] = {}
    for r in results:
        categories.setdefault(r.category, []).append(r)
    for name, rs in sorted(categories.items()):
        lines.append(f"| {name} | {sum(r.passed for r in rs)}/{len(rs)} |")
    lines += ["", "## Cases", "", "| Case | Result | Tool calls | Failed checks |", "|---|---|---|---|"]
    for case_id, rs in by_case.items():
        mark = f"{sum(r.passed for r in rs)}/{len(rs)}"
        tools = "; ".join(" → ".join(c["name"] for c in r.tool_calls) or "—" for r in rs[:1])
        failed = sorted(
            {c.name for r in rs for c in r.checks if not c.passed}
            | {f"error: {r.error}" for r in rs if r.error}
        )
        lines.append(f"| `{case_id}` | {mark} | {tools} | {'<br>'.join(failed) or '—'} |")
    return "\n".join(lines) + "\n"
