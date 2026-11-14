"""Tests for the verifier's unscheduled-task check (agent/checks.py)."""

from datetime import date

from agent.checks import UNSCHEDULED_TASK, run_checks
from pawpal_system import Frequency, Owner, Pet, Priority, Scheduler, Task

PLAN_DATE = date(2026, 11, 14)


def build(constraints, duration, due_date=PLAN_DATE):
    owner = Owner(name="Sam", constraints=constraints)
    pet = Pet(name="Rex", species="dog")
    owner.add_pet(pet)
    pet.create_task(
        Task(
            name="long walk",
            description="a long walk",
            duration=duration,
            priority=Priority.MEDIUM,
            frequency=Frequency.ONCE,
            preferred_time="morning",
            due_date=due_date,
        )
    )
    scheduler = Scheduler(owner)
    scheduler.generate_daily_plan(PLAN_DATE)
    return scheduler


def codes(report):
    result = []
    for issue in report.issues:
        result.append(issue.code)
    return result


def test_task_that_fits_nowhere_is_an_error():
    scheduler = build({"morning": 30, "afternoon": 30, "evening": 30}, duration=60)
    report = run_checks(scheduler, PLAN_DATE)
    assert UNSCHEDULED_TASK in codes(report)
    assert not report.passed
    assert "ask the owner" in report.summary()


def test_task_moved_to_another_block_is_fine():
    # morning is too small, but the afternoon has room, so the plan is complete
    scheduler = build({"morning": 30, "afternoon": 60, "evening": 30}, duration=60)
    report = run_checks(scheduler, PLAN_DATE)
    assert UNSCHEDULED_TASK not in codes(report)
    assert report.passed


def test_task_due_after_the_plan_date_is_not_reported():
    scheduler = build(
        {"morning": 30, "afternoon": 30, "evening": 30},
        duration=60,
        due_date=date(2026, 11, 20),
    )
    report = run_checks(scheduler, PLAN_DATE)
    assert UNSCHEDULED_TASK not in codes(report)
