"""Tests for the reliability harness itself: it must pass good runs AND catch bad ones."""

from agent.agent import Status
from agent.llm import text_response, tool_use_response
from eval.cases import Case, build_cases
from eval.run_eval import format_table, main, run_case


def test_every_offline_case_passes():
    for case in build_cases():
        outcome = run_case(case, live=False, provider=None)
        assert outcome.passed, f"{case.id}: {outcome.failures}"


def test_case_ids_are_unique():
    ids = []
    for case in build_cases():
        ids.append(case.id)
    assert len(ids) == len(set(ids))


def test_wrong_status_is_caught():
    case = Case(
        id="x",
        description="d",
        request="Rex needs a walk.",
        expected=[Status.READY],  # but the mock asks a question instead
        script=lambda: [text_response("How long?")],
    )
    outcome = run_case(case, live=False, provider=None)
    assert not outcome.passed
    assert "status needs_input" in outcome.failures[0]


def test_shortened_duration_is_caught():
    walk = {
        "pet_name": "Rex", "name": "walk", "duration": 15,  # owner asked for 30
        "priority": "high", "preferred_time": "morning",
    }
    case = Case(
        id="x",
        description="d",
        request="Rex needs a 30 minute walk.",
        expected=[Status.READY],
        expect_durations=[30],
        script=lambda: [
            tool_use_response(("create_task", walk), ("generate_plan", {})),
            text_response("Added a walk."),
        ],
    )
    outcome = run_case(case, live=False, provider=None)
    assert not outcome.passed
    assert "30 minute duration is missing" in outcome.failures[0]


def test_a_crash_is_a_failure_not_an_exception():
    case = Case(
        id="x", description="d", request="hi", expected=[Status.READY], script=None
    )  # script() will raise TypeError
    outcome = run_case(case, live=False, provider=None)
    assert not outcome.passed
    assert "crashed" in outcome.failures[0]


def test_table_lists_every_case_and_a_total():
    outcomes = []
    for case in build_cases():
        outcomes.append(run_case(case, live=False, provider=None))
    table = format_table(outcomes, live=False)
    assert "13/13 cases passed" in table or "cases passed" in table
    for case in build_cases():
        assert case.id in table


def test_main_returns_zero_offline_and_writes_the_file(tmp_path, capsys):
    out = tmp_path / "results.md"
    assert main(["--out", str(out)]) == 0
    assert "cases passed" in out.read_text()


def test_main_rejects_an_unknown_case_id(capsys):
    assert main(["--case", "no_such_case"]) == 2
