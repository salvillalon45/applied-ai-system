"""PawPal+ logic layer: backend classes for tasks, pets, owners, and scheduling."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import Enum, IntEnum
from itertools import combinations
from typing import Dict, List, Optional, Tuple

TIME_BLOCKS = ("morning", "afternoon", "evening")


class Priority(IntEnum):
    """Lower number = more important."""
    HIGH = 1
    MEDIUM = 2
    LOW = 3


class Frequency(str, Enum):
    """How often a task recurs."""
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass
class Task:
    name: str
    description: str
    duration: int  # minutes, must be > 0
    priority: Priority  # Priority.HIGH (1) is scheduled first
    preferred_time: str  # one of TIME_BLOCKS; soft preference, scheduler may fall back
    frequency: Frequency = Frequency.DAILY
    completion_status: bool = False
    pet_name: str = ""  # set by Pet.create_task; don't pass it yourself
    due_date: date = field(default_factory=date.today)  # day this task is due; defaults to today
    start_time: Optional[str] = None  # optional "HH:MM" (24h); used for conflict detection

    def __post_init__(self) -> None:
        """Validate duration, preferred_time and start_time, and coerce priority and frequency to enums."""
        if self.duration <= 0:
            raise ValueError(f"duration must be > 0, got {self.duration}")
        if self.start_time is not None:
            try:
                if len(self.start_time) != 5:  # strptime would also accept "8:30"
                    raise ValueError
                datetime.strptime(self.start_time, "%H:%M")
            except ValueError:
                raise ValueError(
                    f"start_time must be zero-padded 24h 'HH:MM', got {self.start_time!r}"
                ) from None
        if self.preferred_time not in TIME_BLOCKS:
            raise ValueError(
                f"preferred_time must be one of {TIME_BLOCKS}, got {self.preferred_time!r}"
            )
        self.priority = Priority(self.priority)
        self.frequency = Frequency(self.frequency)

    @property
    def start_minutes(self) -> Optional[int]:
        """start_time as minutes after midnight (e.g. "08:30" -> 510), or None if unset."""
        if self.start_time is None:
            return None
        hours, minutes = self.start_time.split(":")
        return int(hours) * 60 + int(minutes)

    def mark_complete(self) -> None:
        """Mark this task as done so the scheduler skips it."""
        self.completion_status = True

    def next_occurrence(self) -> Optional["Task"]:
        """Return an incomplete copy due on the next occurrence, or None for ONCE tasks.

        Daily tasks move forward 1 day and weekly tasks 7 days, counted from this
        task's due_date (not today), so finishing a task late doesn't skip a day.
        """
        if self.frequency == Frequency.DAILY:
            step = timedelta(days=1)
        elif self.frequency == Frequency.WEEKLY:
            step = timedelta(weeks=1)
        else:
            return None
        # pet_name is cleared so Pet.create_task can attach the copy
        return replace(
            self, completion_status=False, pet_name="", due_date=self.due_date + step
        )


@dataclass
class Pet:
    name: str
    species: str = "other"  # e.g. "dog", "cat", "other"
    tasks: List[Task] = field(default_factory=list)

    def create_task(self, task: Task) -> None:
        """Attach a task to this pet. Incomplete task names must be unique per pet.

        Completed tasks are kept as history, so a new task may reuse their name
        (this is how recurring tasks get their next occurrence).
        """
        if task.pet_name:
            raise ValueError(f"task {task.name!r} already belongs to {task.pet_name!r}")
        if any(t.name == task.name and not t.completion_status for t in self.tasks):
            raise ValueError(f"{self.name} already has a task named {task.name!r}")
        task.pet_name = self.name
        self.tasks.append(task)

    def complete_task(self, task_name: str) -> Optional[Task]:
        """Mark the named task complete and, if it recurs, add its next occurrence.

        Targets the pet's incomplete task with that name, so completing "walk" twice
        completes today's walk and then the next day's. Returns the newly created
        task, or None if the task doesn't recur or only completed copies remain.
        """
        task = self._find_task(task_name)
        if task.completion_status:
            return None
        task.mark_complete()
        next_task = task.next_occurrence()
        if next_task is not None:
            self.create_task(next_task)
        return next_task

    def edit_task(self, task_name: str, **changes) -> None:
        """Update fields on the task with the given name."""
        if "pet_name" in changes:
            raise ValueError("pet_name is set by create_task and can't be edited")
        task = self._find_task(task_name)
        for key, value in changes.items():
            if not hasattr(task, key):
                raise AttributeError(f"Task has no field {key!r}")
            setattr(task, key, value)
        task.__post_init__()  # re-validate

    def remove_task(self, task_name: str) -> None:
        """Delete the task with the given name from this pet."""
        self.tasks.remove(self._find_task(task_name))

    def _find_task(self, task_name: str) -> Task:
        """Return the task with the given name, or raise KeyError if there isn't one.

        Prefers the incomplete task when a completed one shares the name.
        """
        matches = [t for t in self.tasks if t.name == task_name]
        if not matches:
            raise KeyError(f"no task named {task_name!r}")
        for task in matches:
            if not task.completion_status:
                return task
        return matches[0]


@dataclass
class Owner:
    name: str
    pets: List[Pet] = field(default_factory=list)
    # available minutes per time block, e.g. {"morning": 120, "afternoon": 0, "evening": 90}
    constraints: Dict[str, int] = field(
        default_factory=lambda: {block: 0 for block in TIME_BLOCKS}
    )

    def __post_init__(self) -> None:
        """Reject unknown time blocks and default any missing blocks to 0 minutes."""
        unknown =set(self.constraints) - set(TIME_BLOCKS)
        if unknown:
            raise ValueError(f"unknown time blocks in constraints: {sorted(unknown)}")
        # blocks the caller left out have no available time
        for block in TIME_BLOCKS:
            self.constraints.setdefault(block, 0)

    def add_pet(self, pet: Pet) -> None:
        """Add a pet to this owner; pet names must be unique."""
        if any(p.name == pet.name for p in self.pets):
            raise ValueError(f"a pet named {pet.name!r} already exists")
        self.pets.append(pet)

    def get_all_tasks(self) -> List[Task]:
        """Every task across all of the owner's pets."""
        result = []
        for pet in self.pets:
            for task in pet.tasks:
                result.append(task)
        return result

class Scheduler:
    def __init__(self, owner: Owner):
        """Create a scheduler for the given owner with an empty plan."""
        self.owner = owner
        self.plan: Dict[str, List[Task]] = {block: [] for block in TIME_BLOCKS}
        self.explanation_of_plan: str = ""
        self.skipped_tasks: Dict[Tuple[str, str], str] = {}  # (pet, task) -> reason skipped
        self.placement_reasons: Dict[Tuple[str, str], str] = {}  # (pet, task) -> why it went where it did

    def sort_by_time(self, tasks: Optional[List[Task]] = None) -> List[Task]:
        """
        Return tasks ordered by preferred time block (morning, afternoon, evening),
        then start_time, then highest priority, then shortest duration.

        By default sorts the owner's incomplete tasks. Pass tasks (for example the
        result of filter_tasks) to sort that list instead, whatever its status.

        Within a block, tasks with a start_time come first (earliest first), followed
        by untimed tasks. Ties fall back to priority, then duration.
        Doesn't modify the plan, any task, or the list passed in.
        """
        if tasks is None:
            tasks = self.filter_tasks(completed=False)
        tasks = list(tasks)  # sort a copy so the caller's list keeps its order
        tasks.sort(
            key=lambda t: (
                TIME_BLOCKS.index(t.preferred_time),
                t.start_minutes is None,
                t.start_minutes or 0,
                t.priority,
                t.duration,
            )
        )
        return tasks

    def mark_task_complete(self, pet_name: str, task_name: str) -> Optional[Task]:
        """Complete a pet's task; recurring tasks get their next occurrence automatically.

        Returns the newly created task (or None). Raises KeyError for an unknown pet or task.
        """
        for pet in self.owner.pets:
            if pet.name == pet_name:
                return pet.complete_task(task_name)
        raise KeyError(f"no pet named {pet_name!r}")

    def filter_tasks(
        self, pet_name: Optional[str] = None, completed: Optional[bool] = None
    ) -> List[Task]:
        """Return the owner's tasks matching the given filters.

        pet_name: only tasks belonging to that pet (None = all pets).
        completed: True = only finished tasks, False = only unfinished tasks
            (None = both). Filters combine, so both can be given at once.
        """
        tasks = []
        for t in self.owner.get_all_tasks():
            if pet_name is not None and t.pet_name != pet_name:
                continue
            if completed is not None and t.completion_status != completed:
                continue
            tasks.append(t)
        return tasks

    def detect_conflicts(self) -> List[str]:
        """Return a warning message for every pair of overlapping timed tasks.

        Only incomplete tasks with a start_time are checked, and only tasks due on
        the same date can conflict. Two tasks overlap when each starts before the
        other ends (back-to-back tasks don't count). Pairs can belong to the same
        pet or different pets. Never raises; an empty list means no conflicts.
        """
        timed = [
            t for t in self.filter_tasks(completed=False) if t.start_minutes is not None
        ]
        timed.sort(key=lambda t: (t.due_date, t.start_minutes))

        warnings = []
        for a, b in combinations(timed, 2):
            if a.due_date != b.due_date:
                continue
            if a.start_minutes < b.start_minutes + b.duration and (
                b.start_minutes < a.start_minutes + a.duration
            ):
                whose = (
                    f"for {a.pet_name}" if a.pet_name == b.pet_name
                    else f"between {a.pet_name} and {b.pet_name}"
                )
                warnings.append(
                    f"Warning: conflict {whose} on {a.due_date}: "
                    f"'{a.name}' ({self._time_range(a)}) overlaps "
                    f"'{b.name}' ({self._time_range(b)})"
                )
        return warnings

    @staticmethod
    def _time_range(task: Task) -> str:
        """Format a timed task's span as 'HH:MM-HH:MM'."""
        end = task.start_minutes + task.duration
        return f"{task.start_time}-{end // 60 % 24:02d}:{end % 60:02d}"

    def generate_daily_plan(self, plan_date: Optional[date] = None) -> None:
        """Build self.plan from the owner's incomplete tasks.

        plan_date: if given, only tasks due on or before that date are planned
        (overdue tasks are included, future ones such as a recurring task's next
        occurrence are left out). None plans every incomplete task.

        Intended approach:
        - Reset plan, skipped_tasks and placement_reasons first (safe to re-run).
        - Work from a copy of owner.constraints; never mutate the owner's data.
        - Sort incomplete tasks by priority, then duration.
        - Try preferred_time first, then fall back to other blocks.
        - Skip (with a reason) tasks longer than every block or that fit nowhere.
        """
        self.plan = {block: [] for block in TIME_BLOCKS}
        self.skipped_tasks = {}
        self.placement_reasons = {}

        remaining = dict(self.owner.constraints)  # copy; owner's data stays untouched
        longest_block = max(remaining.values(), default=0)

        tasks = []
        for t in self.owner.get_all_tasks():
            if t.completion_status:
                continue
            if plan_date is not None and t.due_date > plan_date:
                continue
            tasks.append(t)
        tasks.sort(key=lambda t: (t.priority, t.duration))

        for task in tasks:
            key = (task.pet_name, task.name)

            if task.duration > longest_block:
                self.skipped_tasks[key] = (
                    f"{task.duration} min is longer than any available time block "
                    f"(longest is {longest_block} min)"
                )
                continue

            # preferred block first, then the others in their normal order
            order = [task.preferred_time] + [
                b for b in TIME_BLOCKS if b != task.preferred_time
            ]
            for block in order:
                if task.duration <= remaining[block]:
                    self.plan[block].append(task)
                    remaining[block] -= task.duration
                    if block == task.preferred_time:
                        self.placement_reasons[key] = (
                            f"placed in preferred block '{block}' "
                            f"({task.priority.name} priority)"
                        )
                    else:
                        self.placement_reasons[key] = (
                            f"preferred '{task.preferred_time}' was full; "
                            f"moved to '{block}' ({task.priority.name} priority)"
                        )
                    break
            else:
                self.skipped_tasks[key] = (
                    f"no block has {task.duration} min left "
                    f"(remaining: {remaining})"
                )

    def generate_explanation_of_plan(self) -> None:
        """Format placement_reasons and skipped_tasks into explanation_of_plan."""
        lines = []

        for block in TIME_BLOCKS:
            tasks = self.plan[block]
            used = sum(t.duration for t in tasks)
            available = self.owner.constraints[block]
            lines.append(f"{block.capitalize()} ({used}/{available} min used):")
            if not tasks:
                lines.append("  (nothing scheduled)")
            for task in tasks:
                reason = self.placement_reasons[(task.pet_name, task.name)]
                lines.append(
                    f"  - {task.pet_name}: {task.name} ({task.duration} min) - {reason}"
                )

        if self.skipped_tasks:
            lines.append("")
            lines.append("Skipped:")
            for (pet_name, task_name), reason in self.skipped_tasks.items():
                lines.append(f"  - {pet_name}: {task_name} - {reason}")

        self.explanation_of_plan = "\n".join(lines)
