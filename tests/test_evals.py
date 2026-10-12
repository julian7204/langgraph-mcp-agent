from __future__ import annotations

from pathlib import Path

from conftest import ScriptedModel, call, say

from mcp_agent.evals import Case, load_cases, run_case, score

CASES = Path(__file__).parent.parent / "evals" / "cases.yaml"


def test_cases_file_is_valid_and_ids_are_unique() -> None:
    cases = load_cases(CASES)
    assert len(cases) >= 12
    assert len({c.id for c in cases}) == len(cases)


def test_score_checks_tools_args_state_and_answer() -> None:
    case = Case.model_validate(
        {
            "id": "x",
            "category": "update",
            "prompt": "p",
            "expect": {
                "tools_called": ["update_task"],
                "tools_not_called": ["delete_task"],
                "args": {"update_task": {"id": "t1", "status": "done"}},
                "final_state": [{"id": "t1", "status": "done"}],
                "answer_contains_any": ["done"],
                "max_tool_calls": 1,
            },
        }
    )
    calls = [{"name": "update_task", "args": {"id": "t1", "status": "done"}}]
    after = [{"id": "t1", "status": "done"}]
    assert all(c.passed for c in score(case, calls, "Marked as done.", after, after))

    bad = score(case, [*calls, {"name": "delete_task", "args": {"id": "t1"}}], "ok", after, [])
    failed = {c.name for c in bad if not c.passed}
    assert failed == {
        "never calls delete_task",
        "≤ 1 tool calls",
        'task t1 ⊇ {"id": "t1", "status": "done"}',
        "answer mentions one of ['done']",
    }


def test_string_expectations_are_case_insensitive_substrings() -> None:
    case = Case.model_validate(
        {"id": "x", "category": "c", "prompt": "p", "expect": {"new_task": {"title": "bank"}}}
    )
    after = [{"id": "n", "title": "Call the Bank"}]
    assert score(case, [], "", [], after)[0].passed


async def test_run_case_end_to_end_with_scripted_model() -> None:
    case = next(c for c in load_cases(CASES) if c.id == "complete-by-name")
    model = ScriptedModel(
        script=[
            say("", call("list_tasks", "a", query="domain")),
            say("", call("update_task", "b", id="t1", status="done")),
            say("Marked the domain renewal as done."),
        ]
    )
    result = await run_case(model, case)
    assert result.passed, [c for c in result.checks if not c.passed]
    assert [c["name"] for c in result.tool_calls] == ["list_tasks", "update_task"]
