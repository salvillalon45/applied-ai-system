"""Tool definitions and the whitelisted executor for the PawPal+ agent.

Claude never runs code. It sends a request like "call create_task with these
arguments", and ToolExecutor decides whether to run it:

  1. The tool name must be in the whitelist (TOOL_SCHEMAS). Anything else is refused.
  2. The arguments are validated against that tool's schema (required keys, no
     extra keys, types, allowed values, length limits).
  3. The call runs against a DRAFT copy of the owner, never the real data.
  4. Any error from PawPal+ is returned to Claude as text so it can fix the call.

execute() never raises. It always returns a ToolResult.
"""

import copy
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from agent.checks import CheckReport, run_checks
from pawpal_system import TIME_BLOCKS, Frequency, Owner, Pet, Priority, Scheduler, Task

MAX_TEXT_LENGTH = 200
MAX_MINUTES = 1440  # minutes in a day

PRIORITY_NAMES = ("high", "medium", "low")
FREQUENCY_NAMES = ("once", "daily", "weekly")


# ---------------------------------------------------------------------------
# Tool schemas (Anthropic tool-use format). This dict is the whitelist.
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "get_state",
        "description": (
            "Read-only. List every pet, its tasks, and the owner's available minutes "
            "per time block. Call this first to see what already exists."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "add_pet",
        "description": "Add a new pet. Pet names must be unique.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": MAX_TEXT_LENGTH},
                "species": {
                    "type": "string",
                    "description": "e.g. dog, cat, snake. Required: if the owner has not said, ask them.",
                    "maxLength": MAX_TEXT_LENGTH,
                },
            },
            "required": ["name", "species"],
        },
    },
    {
        "name": "create_task",
        "description": (
            "Add a task to an existing pet. Incomplete task names must be unique per "
            "pet. start_time is only needed when the task must happen at an exact time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pet_name": {"type": "string", "maxLength": MAX_TEXT_LENGTH},
                "name": {"type": "string", "maxLength": MAX_TEXT_LENGTH},
                "description": {"type": "string", "maxLength": MAX_TEXT_LENGTH},
                "duration": {"type": "integer", "description": "minutes, must be > 0"},
                "priority": {"type": "string", "enum": list(PRIORITY_NAMES)},
                "preferred_time": {"type": "string", "enum": list(TIME_BLOCKS)},
                "frequency": {
                    "type": "string",
                    "enum": list(FREQUENCY_NAMES),
                    "description": "Defaults to daily.",
                },
                "start_time": {
                    "type": "string",
                    "description": "Optional 24h zero-padded HH:MM, e.g. 14:00.",
                },
                "due_date": {
                    "type": "string",
                    "description": "Optional YYYY-MM-DD. Defaults to the plan date.",
                },
            },
            "required": ["pet_name", "name", "duration", "priority", "preferred_time"],
        },
    },
    {
        "name": "edit_task",
        "description": (
            "Change fields on an existing task. Give at least one field to change. "
            "Set start_time to null to remove its exact time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pet_name": {"type": "string", "maxLength": MAX_TEXT_LENGTH},
                "task_name": {"type": "string", "maxLength": MAX_TEXT_LENGTH},
                "description": {"type": "string", "maxLength": MAX_TEXT_LENGTH},
                "duration": {"type": "integer"},
                "priority": {"type": "string", "enum": list(PRIORITY_NAMES)},
                "preferred_time": {"type": "string", "enum": list(TIME_BLOCKS)},
                "frequency": {"type": "string", "enum": list(FREQUENCY_NAMES)},
                "start_time": {"type": ["string", "null"]},
                "due_date": {"type": "string"},
            },
            "required": ["pet_name", "task_name"],
        },
    },
    {
        "name": "set_availability",
        "description": "Set how many minutes the owner has free in one time block.",
        "input_schema": {
            "type": "object",
            "properties": {
                "block": {"type": "string", "enum": list(TIME_BLOCKS)},
                "minutes": {"type": "integer", "description": "0 to 1440"},
            },
            "required": ["block", "minutes"],
        },
    },
    {
        "name": "generate_plan",
        "description": (
            "Build the daily plan for one date from the current tasks and availability, "
            "then run the verifier. Only tasks due on or before that date are planned. "
            "Returns the plan plus any problems found. Call this after making changes, "
            "and fix any ERROR lines before finishing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_date": {
                    "type": "string",
                    "description": (
                        "YYYY-MM-DD of the day to plan. Use the day the owner's request "
                        "is about (e.g. tomorrow's date). Defaults to today."
                    ),
                },
            },
            "required": [],
        },
    },
]


@dataclass
class ToolResult:
    """What a tool call returned. is_error maps to tool_result.is_error in the API."""
    ok: bool
    content: str


class ToolExecutor:
    """Runs whitelisted tools against a draft copy of the owner's data."""

    def __init__(self, owner: Owner, plan_date: Optional[date] = None):
        """Copy the owner into a draft; the real owner is never touched."""
        self.draft: Owner = copy.deepcopy(owner)
        self.scheduler = Scheduler(self.draft)
        self.plan_date: date = plan_date or date.today()
        self.last_report: Optional[CheckReport] = None
        self.last_plan_date: Optional[date] = None  # the date the last plan was built for
        self.plan_is_current = False  # False once anything changes after generate_plan
        self.changes_made = 0  # count of successful mutations; the agent loop compares it

        self._tools: Dict[str, Callable[[Dict[str, Any]], str]] = {
            "get_state": self._get_state,
            "add_pet": self._add_pet,
            "create_task": self._create_task,
            "edit_task": self._edit_task,
            "set_availability": self._set_availability,
            "generate_plan": self._generate_plan,
        }
        self._schemas: Dict[str, Dict[str, Any]] = {}
        for schema in TOOL_SCHEMAS:
            self._schemas[schema["name"]] = schema["input_schema"]

    @property
    def tool_names(self) -> List[str]:
        """Names of every tool Claude is allowed to call."""
        return list(self._tools)

    @property
    def ready_for_review(self) -> bool:
        """True when the plan is up to date and the verifier found no errors."""
        if not self.plan_is_current:
            return False
        if self.last_report is None:
            return False
        return self.last_report.passed

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def execute(self, name: str, args: Any) -> ToolResult:
        """Validate and run one tool call. Never raises."""
        if name not in self._tools:
            allowed = ", ".join(self.tool_names)
            return ToolResult(False, f"unknown tool {name!r}. Allowed tools: {allowed}")
        try:
            self._validate(args, self._schemas[name])
            return ToolResult(True, self._tools[name](args))
        except (ValueError, KeyError, TypeError) as error:
            # KeyError str() wraps the message in quotes, so unwrap it
            message = error.args[0] if error.args else str(error)
            return ToolResult(False, str(message))

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, args: Any, schema: Dict[str, Any]) -> None:
        """Check args against a tool's input_schema. Raises ValueError on a problem."""
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        properties = schema["properties"]

        for key in args:
            if key not in properties:
                raise ValueError(f"unexpected argument {key!r}")
        for key in schema["required"]:
            if key not in args:
                raise ValueError(f"missing required argument {key!r}")

        for key, value in args.items():
            spec = properties[key]
            if not self._type_ok(value, spec["type"]):
                raise ValueError(f"{key!r} must be of type {spec['type']}")
            if "enum" in spec and value not in spec["enum"]:
                raise ValueError(f"{key!r} must be one of {spec['enum']}, got {value!r}")
            if isinstance(value, str) and len(value) > spec.get("maxLength", MAX_TEXT_LENGTH):
                raise ValueError(f"{key!r} is too long (max {MAX_TEXT_LENGTH} characters)")

    @staticmethod
    def _type_ok(value: Any, type_spec: Any) -> bool:
        """Match a value to a JSON-schema type name, or a list of names."""
        names = type_spec
        if isinstance(type_spec, str):
            names = [type_spec]
        for type_name in names:
            if type_name == "null" and value is None:
                return True
            if type_name == "string" and isinstance(value, str):
                return True
            # bool is a subclass of int in Python, so reject it explicitly
            if type_name == "integer" and isinstance(value, int) and not isinstance(value, bool):
                return True
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_pet(self, pet_name: str) -> Pet:
        """Return the draft pet with this name, or raise KeyError."""
        for pet in self.draft.pets:
            if pet.name == pet_name:
                return pet
        raise KeyError(f"no pet named {pet_name!r}")

    @staticmethod
    def _parse_date(text: str) -> date:
        """Parse YYYY-MM-DD, raising a clear ValueError."""
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"date must be YYYY-MM-DD, got {text!r}") from None

    def _mark_changed(self) -> None:
        """Any mutation makes the last generated plan out of date."""
        self.plan_is_current = False
        self.changes_made += 1

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    def _get_state(self, _args: Dict[str, Any]) -> str:
        """Describe the draft: availability, pets and tasks."""
        lines = [f"Plan date: {self.plan_date}"]
        lines.append(f"Available minutes: {self.draft.constraints}")
        if not self.draft.pets:
            lines.append("No pets yet.")
        for pet in self.draft.pets:
            lines.append(f"Pet: {pet.name} ({pet.species})")
            if not pet.tasks:
                lines.append("  (no tasks)")
            for task in pet.tasks:
                status = "done" if task.completion_status else "todo"
                start = task.start_time or "any time"
                lines.append(
                    f"  - {task.name}: {task.duration} min, {task.priority.name.lower()} "
                    f"priority, prefers {task.preferred_time}, {task.frequency.value}, "
                    f"at {start}, due {task.due_date}, {status}"
                )
        return "\n".join(lines)

    def _add_pet(self, args: Dict[str, Any]) -> str:
        """Add a pet to the draft."""
        pet = Pet(name=args["name"], species=args["species"])
        self.draft.add_pet(pet)
        self._mark_changed()
        return f"added pet {pet.name!r} ({pet.species})"

    def _create_task(self, args: Dict[str, Any]) -> str:
        """Create a task on an existing pet."""
        pet = self._get_pet(args["pet_name"])

        due_date = self.plan_date
        if "due_date" in args:
            due_date = self._parse_date(args["due_date"])

        task = Task(
            name=args["name"],
            description=args.get("description", ""),
            duration=args["duration"],
            priority=Priority[args["priority"].upper()],
            preferred_time=args["preferred_time"],
            frequency=Frequency(args.get("frequency", "daily")),
            start_time=args.get("start_time"),
            due_date=due_date,
        )
        pet.create_task(task)
        self._mark_changed()
        return f"added task {task.name!r} ({task.duration} min) to {pet.name!r}"

    def _edit_task(self, args: Dict[str, Any]) -> str:
        """Edit fields on an existing task, restoring the pet's tasks if it fails."""
        pet = self._get_pet(args["pet_name"])

        changes: Dict[str, Any] = {}
        for key, value in args.items():
            if key in ("pet_name", "task_name"):
                continue
            if key == "priority":
                value = Priority[value.upper()]
            elif key == "frequency":
                value = Frequency(value)
            elif key == "due_date":
                value = self._parse_date(value)
            changes[key] = value
        if not changes:
            raise ValueError("edit_task needs at least one field to change")

        # Pet.edit_task sets fields before validating, so a bad value would leave
        # the task half-edited. Snapshot first and roll back on failure.
        snapshot = copy.deepcopy(pet.tasks)
        try:
            pet.edit_task(args["task_name"], **changes)
        except (ValueError, KeyError, TypeError):
            pet.tasks[:] = snapshot
            raise
        self._mark_changed()
        return f"updated task {args['task_name']!r} for {pet.name!r}: {sorted(changes)}"

    def _set_availability(self, args: Dict[str, Any]) -> str:
        """Set the owner's free minutes for one block."""
        minutes = args["minutes"]
        if minutes < 0 or minutes > MAX_MINUTES:
            raise ValueError(f"minutes must be between 0 and {MAX_MINUTES}, got {minutes}")
        self.draft.constraints[args["block"]] = minutes
        self._mark_changed()
        return f"{args['block']} availability set to {minutes} min"
    
    def _generate_plan(self, args: Dict[str, Any]) -> str:
        """Build the plan for one date, run the verifier, and report both."""
        plan_date = self.plan_date
        if "plan_date" in args:
            plan_date = self._parse_date(args["plan_date"])

        self.scheduler.generate_daily_plan(plan_date)
        self.scheduler.generate_explanation_of_plan()
        self.last_report = run_checks(self.scheduler, plan_date)
        self.last_plan_date = plan_date
        self.plan_is_current = True

        result = (
            f"Plan for {plan_date:%A %Y-%m-%d}:\n{self.scheduler.explanation_of_plan}\n\n"
            f"Verifier:\n{self.last_report.summary()}"
        )

        # Tasks due after the plan date are left out of the plan on purpose, but an
        # agent that doesn't notice can approve an empty plan. Say so explicitly.
        later = []
        for task in self.draft.get_all_tasks():
            if not task.completion_status and task.due_date > plan_date:
                later.append(f"{task.pet_name}: {task.name} (due {task.due_date})")
        if later:
            result += (
                "\n\nNOTE: these tasks are due after this plan date, so they are NOT in "
                "this plan: " + "; ".join(later) + ". If the owner meant this date, "
                "call generate_plan again with the right plan_date."
            )
        return result
