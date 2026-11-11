"""Deterministic verifier for a generated PawPal+ plan.

This is the "check its own work" step of the agent loop. It uses no LLM, so the
same checks serve the agent, the unit tests and the eval harness.

Call run_checks(scheduler, plan_date) AFTER scheduler.generate_daily_plan(plan_date).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from pawpal_system import TIME_BLOCKS, Scheduler

# Issue codes
CONFLICT = "conflict"
BLOCK_OVERFLOW = "block_overflow"
PRIORITY_INVERSION = "priority_inversion"
DROPPED_TASK = "dropped_task"
DUPLICATE_PLACEMENT = "duplicate_placement"

# Severities: "error" means the plan is invalid, "warning" means it is valid but poor.
ERROR = "error"
WARNING = "warning"


@dataclass
class CheckIssue:
    """One problem found in a plan."""
    code: str
    severity: str
    message: str  # written so it can be fed straight back to the LLM
    pet_name: Optional[str] = None
    task_name: Optional[str] = None


@dataclass
class CheckReport:
    """Result of running every check on a plan."""
    issues: List[CheckIssue] = field(default_factory=list)

    @property
    def errors(self) -> List[CheckIssue]:
        """Issues that make the plan invalid."""
        result = []
        for issue in self.issues:
            if issue.severity == ERROR:
                result.append(issue)
        return result

    @property
    def warnings(self) -> List[CheckIssue]:
        """Issues that leave the plan valid but suboptimal."""
        result = []
        for issue in self.issues:
            if issue.severity == WARNING:
                result.append(issue)
        return result

    @property
    def passed(self) -> bool:
        """True when there are no errors (warnings are allowed)."""
        return len(self.errors) == 0

    @property
    def clean(self) -> bool:
        """True when there are no issues at all."""
        return len(self.issues) == 0

    def summary(self) -> str:
        """Plain-text summary suitable for logging or sending back to the LLM."""
        if self.clean:
            return "No issues found."
        lines = []
        for issue in self.issues:
            lines.append(f"[{issue.severity.upper()}] {issue.code}: {issue.message}")
        return "\n".join(lines)


def check_conflicts(scheduler: Scheduler) -> List[CheckIssue]:
    """Report every pair of overlapping timed tasks (wraps Scheduler.detect_conflicts).

    Note: detect_conflicts covers all incomplete timed tasks, not only those due
    on the plan date.
    """
    issues = []
    for warning in scheduler.detect_conflicts():
        issues.append(CheckIssue(code=CONFLICT, severity=ERROR, message=warning))
    return issues


def check_block_overflow(scheduler: Scheduler) -> List[CheckIssue]:
    """Report any time block whose planned minutes exceed the owner's available minutes."""
    issues = []
    for block in TIME_BLOCKS:
        used = 0
        for task in scheduler.plan[block]:
            used += task.duration
        available = scheduler.owner.constraints[block]
        if used > available:
            issues.append(
                CheckIssue(
                    code=BLOCK_OVERFLOW,
                    severity=ERROR,
                    message=(
                        f"'{block}' has {used} min planned but only {available} min available."
                    ),
                )
            )
    return issues


def check_priority_inversion(scheduler: Scheduler) -> List[CheckIssue]:
    """Report skipped tasks that are more important than a task that was placed.

    This is a warning, not an error: the greedy scheduler can legitimately skip a
    long high-priority task that fits nowhere. But the agent should try to fix it,
    for example by shortening that task or freeing time in a block.
    """
    placed = []
    for block in TIME_BLOCKS:
        for task in scheduler.plan[block]:
            placed.append(task)

    # Index every incomplete task by (pet, name) so skipped keys can be resolved
    tasks_by_key = {}
    for task in scheduler.filter_tasks(completed=False):
        tasks_by_key[(task.pet_name, task.name)] = task

    issues = []
    for key in scheduler.skipped_tasks:
        skipped = tasks_by_key.get(key)
        if skipped is None:
            continue
        less_important = []
        for task in placed:
            if task.priority > skipped.priority:
                less_important.append(task)
        if len(less_important) == 0:
            continue
        names = []
        for task in less_important:
            names.append(f"{task.pet_name}: {task.name} ({task.priority.name})")
        issues.append(
            CheckIssue(
                code=PRIORITY_INVERSION,
                severity=WARNING,
                message=(
                    f"{skipped.priority.name}-priority task '{skipped.name}' for "
                    f"{skipped.pet_name} was skipped ({scheduler.skipped_tasks[key]}) "
                    f"while less important tasks were placed: {', '.join(names)}."
                ),
                pet_name=skipped.pet_name,
                task_name=skipped.name,
            )
        )
    return issues


def check_no_dropped_tasks(
    scheduler: Scheduler, plan_date: Optional[date] = None
) -> List[CheckIssue]:
    """Report eligible tasks that are neither placed nor skipped.

    Every incomplete task due on or before plan_date must appear in exactly one
    of the plan blocks or in skipped_tasks, otherwise the user is never told.
    """
    accounted = set()
    for block in TIME_BLOCKS:
        for task in scheduler.plan[block]:
            accounted.add((task.pet_name, task.name))
    for key in scheduler.skipped_tasks:
        accounted.add(key)

    issues = []
    for task in scheduler.filter_tasks(completed=False):
        if plan_date is not None and task.due_date > plan_date:
            continue
        key = (task.pet_name, task.name)
        if key not in accounted:
            issues.append(
                CheckIssue(
                    code=DROPPED_TASK,
                    severity=ERROR,
                    message=(
                        f"'{task.name}' for {task.pet_name} is neither scheduled nor "
                        f"listed as skipped."
                    ),
                    pet_name=task.pet_name,
                    task_name=task.name,
                )
            )
    return issues


def check_no_duplicate_placements(scheduler: Scheduler) -> List[CheckIssue]:
    """Report any task that appears more than once across the plan blocks."""
    counts: Dict[Tuple[str, str], int] = {}
    for block in TIME_BLOCKS:
        for task in scheduler.plan[block]:
            key = (task.pet_name, task.name)
            counts[key] = counts.get(key, 0) + 1

    issues = []
    for key, count in counts.items():
        if count > 1:
            issues.append(
                CheckIssue(
                    code=DUPLICATE_PLACEMENT,
                    severity=ERROR,
                    message=f"'{key[1]}' for {key[0]} is placed {count} times in the plan.",
                    pet_name=key[0],
                    task_name=key[1],
                )
            )
    return issues


def run_checks(scheduler: Scheduler, plan_date: Optional[date] = None) -> CheckReport:
    """Run every check against the scheduler's current plan and return one report.

    generate_daily_plan(plan_date) must have been called first.
    """
    report = CheckReport()
    report.issues.extend(check_conflicts(scheduler))
    report.issues.extend(check_block_overflow(scheduler))
    report.issues.extend(check_no_dropped_tasks(scheduler, plan_date))
    report.issues.extend(check_no_duplicate_placements(scheduler))
    report.issues.extend(check_priority_inversion(scheduler))
    return report
