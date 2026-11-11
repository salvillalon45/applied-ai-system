from pawpal_system import Scheduler, Owner, Pet, Task, Priority, Frequency

# Sample data is chosen so every scheduler path shows up in the output:
#   - preferred block used        (meal prep fits in the morning)
#   - fallback to another block   (walks don't fit in the morning, move to evening)
#   - too long for any block      (bath 120 min and groom 150 min, longest block is 90)
#   - fits nowhere                (play fetch: morning and evening are nearly full)
#   - completed task ignored      (brush teeth)
#   - priority ordering           (HIGH tasks claim space before MEDIUM/LOW)

rex = Pet("Rex")
rex.create_task(
    Task(
        "walk", 
        "walk Rex in the local park nearby",
        30,
        Priority.HIGH,
        "morning",
        Frequency.DAILY
    )
)
rex.create_task(
    Task(
        "prep meal",
        "have the meal ready so I can just serve it",
        20,
        Priority.HIGH,
        "morning",
        Frequency.DAILY
    )
)
rex.create_task(
    Task(
        "bath",
        "give Rex a bath to maintain hygiene", 
        120,
        Priority.MEDIUM,
        "morning",
        Frequency.WEEKLY
    )
)
rex.create_task(
    Task(
        "play fetch", 
        "play fetch in the backyard",
        40,
        Priority.LOW,
        "morning",
        Frequency.DAILY
    )
)
rex.create_task(
    Task(
        "brush teeth",
        "brush Rex's teeth",
        10,
        Priority.LOW,
        "morning",
        Frequency.DAILY,
        completion_status=True
    )
)

buddy = Pet("Buddy")
buddy.create_task(
    Task(
        "walk",
        "walk Buddy in the local park nearby",
        30,
        Priority.HIGH,
        "morning",
        Frequency.DAILY
    )
)
buddy.create_task(
    Task(
        "prep meal",
        "have the meal ready so I can just serve it",
        20,
        Priority.HIGH,
        "morning",
        Frequency.DAILY
    )
)
buddy.create_task(
    Task(
        "nail trim",
        "trim Buddy's nails",
        15,
        Priority.MEDIUM,
        "morning",
        Frequency.WEEKLY
    )
)
buddy.create_task(
    Task(
        "full groom",
        "full grooming session",
        150,
        Priority.MEDIUM,
        "evening",
        Frequency.WEEKLY
    )
)

constraints = {
    "morning": 60,
    "afternoon": 0,
    "evening": 90,
}

jerry = Owner("Jerry", [rex, buddy], constraints)

scheduler = Scheduler(jerry)
scheduler.generate_daily_plan()
scheduler.generate_explanation_of_plan()

print(scheduler.explanation_of_plan)


# --- Sorting and filtering demo ---
# Tasks are added deliberately out of order (evening first, mixed priorities and
# durations) so the sorted output differs visibly from the insertion order.

def show(title, tasks):
    print(f"\n{title}")
    if not tasks:
        print("  (none)")
    for t in tasks:
        status = "done" if t.completion_status else "todo"
        start = t.start_time or "no start time"
        print(
            f"  - [{status}] {t.pet_name}: {t.name} "
            f"({t.preferred_time}, {start}, {t.priority.name}, {t.duration} min)"
        )


# Some tasks have a start_time and some don't, so the sort shows timed tasks
# ordered by clock time within a block, with untimed tasks after them.
whiskers = Pet("Whiskers", "cat")
whiskers.create_task(Task("play", "feather wand play", 15, Priority.LOW, "evening", start_time="18:00"))
whiskers.create_task(Task("litter box", "scoop litter", 10, Priority.MEDIUM, "afternoon"))
whiskers.create_task(Task("breakfast", "wet food", 5, Priority.HIGH, "morning", start_time="07:30"))
whiskers.create_task(Task("medicine", "ear drops", 5, Priority.HIGH, "evening", start_time="19:00"))
whiskers.create_task(Task("brush fur", "brush coat", 20, Priority.LOW, "morning", start_time="07:00"))
whiskers.create_task(Task("vet note", "write up vet notes", 10, Priority.LOW, "afternoon"))
whiskers.tasks[-1].mark_complete()

demo_owner = Owner("Sam", [whiskers, Pet("Mochi", "dog")], {"morning": 60, "afternoon": 30, "evening": 60})
demo_owner.pets[1].create_task(Task("evening walk", "walk Mochi", 30, Priority.HIGH, "evening"))
demo_owner.pets[1].create_task(Task("breakfast", "kibble", 5, Priority.HIGH, "morning"))
demo_owner.pets[1].tasks[0].mark_complete()

demo = Scheduler(demo_owner)

show("As added (unsorted):", demo_owner.get_all_tasks())
show("Sorted by time block, start time, priority, duration (incomplete only):", demo.sort_by_time())
show("Filter: incomplete tasks", demo.filter_tasks(completed=False))
show("Filter: completed tasks", demo.filter_tasks(completed=True))
show("Filter: Whiskers' tasks", demo.filter_tasks(pet_name="Whiskers"))
show("Filter: Mochi's incomplete tasks", demo.filter_tasks(pet_name="Mochi", completed=False))


# --- Recurring tasks demo ---
# Completing a daily/weekly task creates the next occurrence; a one-time task does not.
from datetime import date

pip = Pet("Pip", "dog")
pip.create_task(Task("walk", "walk Pip", 20, Priority.HIGH, "morning", Frequency.DAILY, due_date=date(2026, 10, 18)))
pip.create_task(Task("bath", "bathe Pip", 30, Priority.MEDIUM, "evening", Frequency.WEEKLY, due_date=date(2026, 10, 18)))
pip.create_task(Task("vet visit", "annual checkup", 45, Priority.HIGH, "afternoon", Frequency.ONCE, due_date=date(2026, 10, 18)))

recurring = Scheduler(Owner("Alex", [pip], {"morning": 60, "afternoon": 60, "evening": 60}))


def show_with_dates(title, tasks):
    print(f"\n{title}")
    for t in tasks:
        status = "done" if t.completion_status else "todo"
        print(f"  - [{status}] {t.name} ({t.frequency.value}) due {t.due_date}")


show_with_dates("Before completing anything:", pip.tasks)
for name in ("walk", "bath", "vet visit"):
    new_task = recurring.mark_task_complete("Pip", name)
    if new_task:
        print(f"\nCompleted {name!r} -> next one created, due {new_task.due_date}")
    else:
        print(f"\nCompleted {name!r} -> no next occurrence ({'one-time task'})")
show_with_dates("After completing all three:", pip.tasks)
show_with_dates("Still to do (incomplete):", recurring.filter_tasks(completed=False))


# --- Conflict detection demo ---
# detect_conflicts() returns warning strings instead of raising, so the program keeps running.
today = date.today()

luna = Pet("Luna", "dog")
milo = Pet("Milo", "cat")
luna.create_task(Task("walk", "morning walk", 30, Priority.HIGH, "morning", start_time="08:00", due_date=today))
luna.create_task(Task("feed", "breakfast", 10, Priority.HIGH, "morning", start_time="08:00", due_date=today))  # same time as walk, same pet
milo.create_task(Task("vet call", "phone the vet", 20, Priority.MEDIUM, "morning", start_time="08:15", due_date=today))  # overlaps walk, different pet
milo.create_task(Task("play", "laser pointer", 15, Priority.LOW, "evening", start_time="18:00", due_date=today))  # no conflict
milo.create_task(Task("brush", "brush coat", 10, Priority.LOW, "evening", due_date=today))  # no start_time, never checked

conflict_scheduler = Scheduler(Owner("Casey", [luna, milo], {"morning": 120, "afternoon": 0, "evening": 60}))

print("\nChecking for conflicts:")
warnings = conflict_scheduler.detect_conflicts()
if warnings:
    for w in warnings:
        print(f"  {w}")
else:
    print("  No conflicts found.")

# Resolve the conflicts by moving tasks, then check again.
luna.edit_task("feed", start_time="08:30")  # back-to-back with walk, not overlapping
milo.edit_task("vet call", start_time="09:00")
print("\nAfter rescheduling feed to 08:30 and vet call to 09:00:")
warnings = conflict_scheduler.detect_conflicts()
for w in warnings or ["No conflicts found."]:
    print(f"  {w}")
