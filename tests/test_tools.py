"""Tests for the agent's whitelisted tool executor (agent/tools.py)."""

from datetime import date

import pytest

from agent.tools import TOOL_SCHEMAS, ToolExecutor
from pawpal_system import Owner, Pet

PLAN_DATE = date(2026, 11, 12)


@pytest.fixture
def owner():
    """An owner with one pet and 60 free minutes in the morning."""
    result = Owner(name="Sam", constraints={"morning": 60})
    result.add_pet(Pet(name="Rex", species="dog"))
    return result


@pytest.fixture
def executor(owner):
    return ToolExecutor(owner, plan_date=PLAN_DATE)


def walk_args(**overrides):
    """Valid create_task arguments, with optional overrides."""
    args = {
        "pet_name": "Rex",
        "name": "walk",
        "duration": 30,
        "priority": "high",
        "preferred_time": "morning",
    }
    args.update(overrides)
    return args


# ---------------------------------------------------------------- whitelist


def test_every_schema_has_an_implementation(executor):
    schema_names = []
    for schema in TOOL_SCHEMAS:
        schema_names.append(schema["name"])
    assert sorted(schema_names) == sorted(executor.tool_names)


@pytest.mark.parametrize("name", ["delete_all_pets", "remove_task", "run_shell", ""])
def test_unknown_tool_is_refused(executor, name):
    result = executor.execute(name, {})
    assert not result.ok
    assert "unknown tool" in result.content
    assert "create_task" in result.content  # tells Claude what IS allowed


# --------------------------------------------------------------- validation


def test_missing_required_argument(executor):
    args = walk_args()
    del args["duration"]
    result = executor.execute("create_task", args)
    assert not result.ok
    assert "duration" in result.content


def test_unexpected_argument_is_refused(executor):
    result = executor.execute("create_task", walk_args(pet_name_override="x"))
    assert not result.ok
    assert "unexpected argument" in result.content


def test_wrong_type_is_refused(executor):
    result = executor.execute("create_task", walk_args(duration="30"))
    assert not result.ok


def test_bool_is_not_accepted_as_integer(executor):
    result = executor.execute("create_task", walk_args(duration=True))
    assert not result.ok


def test_bad_enum_value_is_refused(executor):
    result = executor.execute("create_task", walk_args(priority="urgent"))
    assert not result.ok
    assert "priority" in result.content


def test_overlong_text_is_refused(executor):
    result = executor.execute("create_task", walk_args(name="x" * 500))
    assert not result.ok
    assert "too long" in result.content


def test_non_dict_arguments_are_refused(executor):
    result = executor.execute("create_task", "not a dict")
    assert not result.ok


# ------------------------------------------------------------------- tools


def test_add_pet_and_create_task(executor):
    assert executor.execute("add_pet", {"name": "Mia", "species": "cat"}).ok
    result = executor.execute("create_task", walk_args(pet_name="Mia"))
    assert result.ok
    assert executor.draft.pets[1].tasks[0].name == "walk"


def test_create_task_defaults_due_date_to_plan_date(executor):
    executor.execute("create_task", walk_args())
    assert executor.draft.pets[0].tasks[0].due_date == PLAN_DATE


def test_create_task_unknown_pet(executor):
    result = executor.execute("create_task", walk_args(pet_name="Ghost"))
    assert not result.ok
    assert "no pet named 'Ghost'" in result.content


def test_create_task_pawpal_validation_error_is_returned(executor):
    result = executor.execute("create_task", walk_args(duration=0))
    assert not result.ok
    assert "duration" in result.content


def test_create_task_bad_start_time(executor):
    result = executor.execute("create_task", walk_args(start_time="8:30"))
    assert not result.ok
    assert "HH:MM" in result.content


def test_create_task_bad_date(executor):
    result = executor.execute("create_task", walk_args(due_date="tomorrow"))
    assert not result.ok
    assert "YYYY-MM-DD" in result.content


def test_duplicate_task_name_is_refused(executor):
    assert executor.execute("create_task", walk_args()).ok
    assert not executor.execute("create_task", walk_args()).ok


def test_edit_task_changes_field(executor):
    executor.execute("create_task", walk_args())
    result = executor.execute(
        "edit_task", {"pet_name": "Rex", "task_name": "walk", "duration": 20}
    )
    assert result.ok
    assert executor.draft.pets[0].tasks[0].duration == 20


def test_edit_task_failure_rolls_back(executor):
    executor.execute("create_task", walk_args())
    result = executor.execute(
        "edit_task",
        {"pet_name": "Rex", "task_name": "walk", "duration": -5, "priority": "low"},
    )
    assert not result.ok
    task = executor.draft.pets[0].tasks[0]
    assert task.duration == 30  # not left half-edited
    assert task.priority.name == "HIGH"


def test_edit_task_needs_a_change(executor):
    executor.execute("create_task", walk_args())
    result = executor.execute("edit_task", {"pet_name": "Rex", "task_name": "walk"})
    assert not result.ok
    assert "edit_task needs at least one field to change" in result.content


def test_edit_task_can_clear_start_time(executor):
    executor.execute("create_task", walk_args(start_time="08:00"))
    result = executor.execute(
        "edit_task", {"pet_name": "Rex", "task_name": "walk", "start_time": None}
    )
    assert result.ok
    assert executor.draft.pets[0].tasks[0].start_time is None


def test_set_availability(executor):
    assert executor.execute("set_availability", {"block": "evening", "minutes": 45}).ok
    assert executor.draft.constraints["evening"] == 45


@pytest.mark.parametrize("minutes", [-1, 1441])
def test_set_availability_out_of_range(executor, minutes):
    result = executor.execute("set_availability", {"block": "evening", "minutes": minutes})
    assert not result.ok


def test_get_state_lists_pets_and_tasks(executor):
    executor.execute("create_task", walk_args())
    result = executor.execute("get_state", {})
    assert result.ok
    assert "Rex" in result.content
    assert "walk" in result.content


# -------------------------------------------------------------------- draft


def test_real_owner_is_never_modified(owner, executor):
    executor.execute("create_task", walk_args())
    executor.execute("set_availability", {"block": "morning", "minutes": 5})
    assert owner.pets[0].tasks == []
    assert owner.constraints["morning"] == 60


# ------------------------------------------------------------ generate_plan


def test_generate_plan_runs_verifier_and_is_ready(executor):
    executor.execute("create_task", walk_args())
    result = executor.execute("generate_plan", {})
    assert result.ok
    assert "Verifier:" in result.content
    assert executor.ready_for_review


def test_generate_plan_reports_verifier_errors(executor):
    # two overlapping timed tasks -> conflict error
    executor.execute("create_task", walk_args(name="walk", start_time="08:00"))
    executor.execute("create_task", walk_args(name="bath", start_time="08:15"))
    result = executor.execute("generate_plan", {})
    assert result.ok  # the tool ran; the PLAN has a problem
    assert "ERROR" in result.content
    assert not executor.ready_for_review


def test_change_after_plan_makes_it_stale(executor):
    executor.execute("create_task", walk_args())
    executor.execute("generate_plan", {})
    assert executor.ready_for_review
    executor.execute("set_availability", {"block": "evening", "minutes": 10})
    assert not executor.ready_for_review


def test_not_ready_before_any_plan(executor):
    assert not executor.ready_for_review


# ------------------------------------------------- generate_plan with a date


def test_generate_plan_uses_the_requested_date(executor):
    tomorrow = "2026-11-13"
    executor.execute("create_task", walk_args(due_date=tomorrow))
    result = executor.execute("generate_plan", {"plan_date": tomorrow})
    assert result.ok
    assert "Plan for Friday 2026-11-13" in result.content
    assert "walk" in result.content
    assert executor.last_plan_date == date(2026, 11, 13)
    assert "NOTE" not in result.content


def test_generate_plan_warns_when_tasks_are_due_after_the_plan_date(executor):
    # the plan date defaults to PLAN_DATE (11-12); the task is due the day after
    executor.execute("create_task", walk_args(due_date="2026-11-13"))
    result = executor.execute("generate_plan", {})
    assert result.ok
    assert "NOTE" in result.content
    assert "walk" in result.content.split("NOTE")[1]
    assert "plan_date" in result.content


def test_generate_plan_rejects_a_bad_date(executor):
    result = executor.execute("generate_plan", {"plan_date": "tomorrow"})
    assert not result.ok
    assert "YYYY-MM-DD" in result.content
