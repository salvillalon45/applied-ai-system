from datetime import date, timedelta

from pawpal_system import Frequency, Owner, Pet, Priority, Scheduler, Task


def make_task(name: str = "walk") -> Task:
    return Task(name, "a test task", 30, Priority.HIGH, "morning", Frequency.DAILY)


def make_scheduler(*pets: Pet) -> Scheduler:
    owner = Owner("Sam")
    for pet in pets:
        owner.add_pet(pet)
    return Scheduler(owner)


def test_mark_complete_changes_status():
    task = make_task()
    assert task.completion_status is False

    task.mark_complete()

    assert task.completion_status is True


def test_adding_task_increases_pet_task_count():
    pet = Pet("Rex")
    assert len(pet.tasks) == 0

    pet.create_task(make_task("walk"))
    assert len(pet.tasks) == 1

    pet.create_task(make_task("feed"))
    assert len(pet.tasks) == 2


# ---------- Sorting correctness ----------


def test_sort_by_time_orders_blocks_morning_afternoon_evening():
    pet = Pet("Rex")
    # added deliberately out of chronological order
    pet.create_task(Task("evening play", "", 20, Priority.HIGH, "evening"))
    pet.create_task(Task("morning walk", "", 20, Priority.HIGH, "morning"))
    pet.create_task(Task("afternoon nap", "", 20, Priority.HIGH, "afternoon"))
    scheduler = make_scheduler(pet)

    names = []
    for task in scheduler.sort_by_time():
        names.append(task.name)

    assert names == ["morning walk", "afternoon nap", "evening play"]


def test_sort_by_time_breaks_ties_by_priority_then_duration():
    pet = Pet("Rex")
    pet.create_task(Task("low", "", 10, Priority.LOW, "morning"))
    pet.create_task(Task("high long", "", 40, Priority.HIGH, "morning"))
    pet.create_task(Task("high short", "", 5, Priority.HIGH, "morning"))
    scheduler = make_scheduler(pet)

    names = []
    for task in scheduler.sort_by_time():
        names.append(task.name)

    assert names == ["high short", "high long", "low"]


def test_sort_by_time_orders_same_block_by_start_time():
    pet = Pet("Rex")
    # lower priority but earlier start time should come first within the block
    pet.create_task(Task("late high", "", 10, Priority.HIGH, "morning", start_time="10:30"))
    pet.create_task(Task("early low", "", 10, Priority.LOW, "morning", start_time="07:00"))
    scheduler = make_scheduler(pet)

    names = []
    for task in scheduler.sort_by_time():
        names.append(task.name)

    assert names == ["early low", "late high"]


def test_sort_by_time_puts_timed_tasks_before_untimed_in_same_block():
    pet = Pet("Rex")
    pet.create_task(Task("untimed", "", 10, Priority.HIGH, "morning"))
    pet.create_task(Task("timed", "", 10, Priority.LOW, "morning", start_time="11:00"))
    scheduler = make_scheduler(pet)

    names = []
    for task in scheduler.sort_by_time():
        names.append(task.name)

    assert names == ["timed", "untimed"]


def test_sort_by_time_start_time_does_not_override_block_order():
    pet = Pet("Rex")
    pet.create_task(Task("evening early", "", 10, Priority.HIGH, "evening", start_time="05:00"))
    pet.create_task(Task("morning late", "", 10, Priority.HIGH, "morning", start_time="11:00"))
    scheduler = make_scheduler(pet)

    names = []
    for task in scheduler.sort_by_time():
        names.append(task.name)

    assert names == ["morning late", "evening early"]


def test_sort_by_time_sorts_across_pets():
    rex = Pet("Rex")
    mia = Pet("Mia")
    rex.create_task(Task("rex evening", "", 15, Priority.HIGH, "evening"))
    mia.create_task(Task("mia morning", "", 15, Priority.HIGH, "morning"))
    scheduler = make_scheduler(rex, mia)

    pairs = []
    for task in scheduler.sort_by_time():
        pairs.append((task.pet_name, task.name))

    assert pairs == [("Mia", "mia morning"), ("Rex", "rex evening")]


def test_sort_by_time_skips_completed_tasks():
    pet = Pet("Rex")
    pet.create_task(Task("done", "", 10, Priority.HIGH, "morning", Frequency.ONCE))
    pet.create_task(Task("todo", "", 10, Priority.HIGH, "evening", Frequency.ONCE))
    pet.complete_task("done")
    scheduler = make_scheduler(pet)

    names = []
    for task in scheduler.sort_by_time():
        names.append(task.name)

    assert names == ["todo"]


def test_sort_by_time_with_no_tasks_returns_empty_list():
    scheduler = make_scheduler(Pet("Rex"))

    assert scheduler.sort_by_time() == []


# ---------- Recurrence logic ----------


def test_completing_daily_task_creates_task_for_next_day():
    pet = Pet("Rex")
    today = date.today()
    pet.create_task(
        Task("walk", "", 30, Priority.HIGH, "morning", Frequency.DAILY, due_date=today)
    )
    scheduler = make_scheduler(pet)

    new_task = scheduler.mark_task_complete("Rex", "walk")

    assert new_task is not None
    assert new_task.due_date == today + timedelta(days=1)
    assert new_task.completion_status is False
    assert new_task.pet_name == "Rex"
    assert new_task in pet.tasks
    assert len(pet.tasks) == 2


def test_completing_daily_task_marks_original_complete():
    pet = Pet("Rex")
    pet.create_task(Task("walk", "a test task", 30, Priority.HIGH, "morning", Frequency.DAILY))
    original = pet.tasks[0]

    pet.complete_task("walk")

    assert original.completion_status is True


def test_next_daily_occurrence_counts_from_due_date_not_today():
    pet = Pet("Rex")
    past_due = date.today() - timedelta(days=3)
    pet.create_task(
        Task("walk", "", 30, Priority.HIGH, "morning", Frequency.DAILY, due_date=past_due)
    )

    new_task = pet.complete_task("walk")

    assert new_task.due_date == past_due + timedelta(days=1)


def test_completing_weekly_task_creates_task_seven_days_later():
    pet = Pet("Rex")
    today = date.today()
    pet.create_task(
        Task("bath", "", 30, Priority.HIGH, "morning", Frequency.WEEKLY, due_date=today)
    )

    new_task = pet.complete_task("bath")

    assert new_task.due_date == today + timedelta(days=7)


def test_completing_one_time_task_creates_no_new_task():
    pet = Pet("Rex")
    pet.create_task(Task("vet", "", 60, Priority.HIGH, "morning", Frequency.ONCE))

    new_task = pet.complete_task("vet")

    assert new_task is None
    assert len(pet.tasks) == 1


def test_daily_recurrence_rolls_over_month_end():
    pet = Pet("Rex")
    pet.create_task(
        Task(
            "walk", "", 30, Priority.HIGH, "morning", Frequency.DAILY,
            due_date=date(2026, 1, 31),
        )
    )

    new_task = pet.complete_task("walk")

    assert new_task.due_date == date(2026, 2, 1)


def test_new_occurrence_keeps_original_task_details():
    pet = Pet("Rex")
    pet.create_task(
        Task(
            "walk", "around the block", 25, Priority.MEDIUM, "evening",
            Frequency.DAILY, start_time="18:00",
        )
    )

    new_task = pet.complete_task("walk")

    assert new_task.name == "walk"
    assert new_task.description == "around the block"
    assert new_task.duration == 25
    assert new_task.priority == Priority.MEDIUM
    assert new_task.preferred_time == "evening"
    assert new_task.start_time == "18:00"


# ---------- Conflict detection ----------


def test_detect_conflicts_flags_duplicate_start_times_same_pet():
    pet = Pet("Rex")
    pet.create_task(Task("walk", "", 30, Priority.HIGH, "morning", start_time="08:00"))
    pet.create_task(Task("feed", "", 10, Priority.HIGH, "morning", start_time="08:00"))
    scheduler = make_scheduler(pet)

    warnings = scheduler.detect_conflicts()

    assert len(warnings) == 1
    assert "walk" in warnings[0]
    assert "feed" in warnings[0]
    assert "08:00" in warnings[0]


def test_detect_conflicts_flags_duplicate_start_times_across_pets():
    rex = Pet("Rex")
    mia = Pet("Mia")
    rex.create_task(Task("walk", "", 30, Priority.HIGH, "morning", start_time="09:00"))
    mia.create_task(Task("feed", "", 15, Priority.HIGH, "morning", start_time="09:00"))
    scheduler = make_scheduler(rex, mia)

    warnings = scheduler.detect_conflicts()

    assert len(warnings) == 1
    assert "Rex" in warnings[0]
    assert "Mia" in warnings[0]


def test_detect_conflicts_flags_partial_overlap():
    pet = Pet("Rex")
    pet.create_task(Task("walk", "", 30, Priority.HIGH, "morning", start_time="08:00"))
    pet.create_task(Task("groom", "", 30, Priority.HIGH, "morning", start_time="08:15"))
    scheduler = make_scheduler(pet)

    assert len(scheduler.detect_conflicts()) == 1


def test_detect_conflicts_ignores_back_to_back_tasks():
    pet = Pet("Rex")
    pet.create_task(Task("walk", "", 30, Priority.HIGH, "morning", start_time="08:00"))
    pet.create_task(Task("feed", "", 10, Priority.HIGH, "morning", start_time="08:30"))
    scheduler = make_scheduler(pet)

    assert scheduler.detect_conflicts() == []


def test_detect_conflicts_ignores_same_time_on_different_days():
    pet = Pet("Rex")
    today = date.today()
    pet.create_task(
        Task(
            "walk", "", 30, Priority.HIGH, "morning",
            start_time="08:00", due_date=today,
        )
    )
    pet.create_task(
        Task(
            "feed", "", 10, Priority.HIGH, "morning",
            start_time="08:00", due_date=today + timedelta(days=1),
        )
    )
    scheduler = make_scheduler(pet)

    assert scheduler.detect_conflicts() == []


def test_detect_conflicts_ignores_completed_tasks():
    pet = Pet("Rex")
    pet.create_task(
        Task("walk", "", 30, Priority.HIGH, "morning", Frequency.ONCE, start_time="08:00")
    )
    pet.create_task(
        Task("feed", "", 10, Priority.HIGH, "morning", Frequency.ONCE, start_time="08:00")
    )
    pet.complete_task("walk")
    scheduler = make_scheduler(pet)

    assert scheduler.detect_conflicts() == []


def test_detect_conflicts_ignores_tasks_without_start_time():
    pet = Pet("Rex")
    pet.create_task(Task("walk", "", 30, Priority.HIGH, "morning"))
    pet.create_task(Task("feed", "", 10, Priority.HIGH, "morning"))
    scheduler = make_scheduler(pet)

    assert scheduler.detect_conflicts() == []


def test_detect_conflicts_with_no_tasks_returns_empty_list():
    scheduler = make_scheduler(Pet("Rex"))

    assert scheduler.detect_conflicts() == []
