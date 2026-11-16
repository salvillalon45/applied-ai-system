"""PawPal+ Planning Agent: command-line interface.

Interactive:
    python main.py

One request, then exit (still asks you to approve unless --auto-approve is given):
    python main.py --request "Rex needs a 30 minute walk every morning"

The flow for every request:
    you type a request -> the agent plans, acts and checks its own work on a DRAFT
    -> you review the proposed plan -> approve (saved), revise (give feedback) or
    discard. Nothing is saved to your schedule until you approve.
"""

import argparse
import copy
import sys
from datetime import date
from typing import Callable, Optional

from agent.agent import PlanningAgent, Status, setup_logging
from agent.llm import LLMClient, LLMError, create_client
from pawpal_system import TIME_BLOCKS, Owner, Scheduler

DIVIDER = "-" * 70
HINT = "Tip: /plan = minutes used per time block, /state = saved tasks, /help = more"

HELP_TEXT = """Describe what you want in plain English, for example:
  Rex needs a 30 minute walk every morning and a 20 minute play session in the evening.
  Add a cat named Mia with a vet visit on Friday at 14:00 for 60 minutes.

Commands:
  /plan    show today's plan from your saved tasks, with minutes used per time block
  /state   show your saved tasks and availability
  /new     discard the current draft and start over
  /help    show this message
  /quit    leave"""

# What each non-ready outcome looks like to the user.
STATUS_LABELS = {
    Status.NEEDS_INPUT: "The agent needs more information",
    Status.UNRESOLVED: "The agent could not produce a valid plan",
    Status.MAX_ITERATIONS: "The agent ran out of attempts",
    Status.REJECTED_INPUT: "Request not accepted",
    Status.REFUSED: "The model declined this request",
    Status.LLM_ERROR: "Something went wrong talking to the model",
}


def describe_owner(owner: Owner) -> str:
    """Text summary of the owner's saved availability, pets and tasks."""
    lines = ["Your daily availability (total minutes per time block; /plan shows how much is used):"]
    for block in TIME_BLOCKS:
        lines.append(f"  {block}: {owner.constraints[block]}")
    if not owner.pets:
        lines.append("No pets or tasks saved yet.")
    for pet in owner.pets:
        lines.append(f"{pet.name} ({pet.species}):")
        if not pet.tasks:
            lines.append("  (no tasks)")
        for task in pet.tasks:
            start = f" at {task.start_time}" if task.start_time else ""
            lines.append(
                f"  - {task.name}: {task.duration} min, {task.priority.name.lower()} priority, "
                f"prefers {task.preferred_time}{start}, {task.frequency.value}, due {task.due_date}"
            )
    return "\n".join(lines)


def current_plan_text(owner: Owner, plan_date: date) -> str:
    """Today's plan built from the SAVED tasks. No model call, so it is instant and free.

    Each block shows "used/available" minutes, so the owner can see how much free
    time is left before asking for the next task. It runs on a copy, so viewing the
    plan can never change the owner's data.
    """
    scheduler = Scheduler(copy.deepcopy(owner))
    scheduler.generate_daily_plan(plan_date)
    scheduler.generate_explanation_of_plan()
    return f"Plan for {plan_date:%A %Y-%m-%d}:\n{scheduler.explanation_of_plan}"


def show_block(output: Callable[[str], None], text: str) -> None:
    """Print text between two dividers, so it is clearly the program's reply to a You> line."""
    output(DIVIDER)
    output(text)
    output(DIVIDER)


def show_result(result, output: Callable[[str], None], draft: Optional[Owner] = None) -> None:
    """Print what the agent produced: its message, the plan, and verifier findings.

    When a draft is given and the plan is ready, also list exactly what approving
    will save. The plan text alone hides details like daily vs weekly.
    """
    output(DIVIDER)
    if result.ready:
        output(f"Proposed plan (the agent used {result.iterations} model call(s))")
    else:
        output(STATUS_LABELS[result.status])
    if result.message:
        output(f"\nAgent: {result.message}")
    if result.plan_text:
        output(f"\n{result.plan_text}")
    if result.report is not None and not result.report.clean:
        output(f"\nVerifier:\n{result.report.summary()}")
    if result.ready and draft is not None:
        output(
            "\nWhat approving will save "
            "(preferences only; the plan above shows where each task lands today):\n"
            f"{describe_owner(draft)}"
        )
    output(DIVIDER)


def read_line(prompt: str, input_fn: Callable[[str], str]) -> Optional[str]:
    """Read one line, returning None if input has ended (Ctrl-D / Ctrl-C)."""
    try:
        return input_fn(prompt)
    except (EOFError, KeyboardInterrupt):
        return None


def ask_decision(input_fn: Callable[[str], str], output: Callable[[str], None]) -> str:
    """Human-in-the-loop prompt. Returns 'approve', 'revise' or 'discard'."""
    while True:
        answer = read_line("Approve this plan? [y]es / [n]o, discard / [r]evise: ", input_fn)
        if answer is None:
            return "discard"
        answer = answer.strip().lower()
        if answer in ("y", "yes"):
            return "approve"
        if answer in ("n", "no"):
            return "discard"
        if answer in ("r", "revise"):
            return "revise"
        output("Please answer y, n or r.")


def run_session(
    owner: Owner,
    llm: LLMClient,
    plan_date: date,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    first_request: Optional[str] = None,
    auto_approve: bool = False,
) -> int:
    """Run the request / review loop. Returns a process exit code.

    With first_request the session handles that one request and then ends
    (one-shot mode, used for scripted runs). Without it, it loops until /quit.
    """
    one_shot = first_request is not None
    agent: Optional[PlanningAgent] = None
    pending = first_request
    exit_code = 0
    responded = False  # True once something was printed in reply to a You> line

    while True:
        # -- get the next piece of text from the user -----------------------
        if pending is not None:
            text = pending
            pending = None
        else:
            if responded:
                output(HINT)
            prompt = "You> " if agent is None else "You (reply to the agent)> "
            line = read_line(prompt, input_fn)
            if line is None:
                output("")
                return exit_code
            text = line.strip()
            if text == "/quit":
                return exit_code
            if text == "":
                continue
            responded = True
            if text == "/help":
                show_block(output, HELP_TEXT)
                continue
            if text == "/state":
                show_block(output, describe_owner(owner))
                continue
            if text == "/plan":
                show_block(output, current_plan_text(owner, plan_date))
                continue
            if text == "/new":
                agent = None
                show_block(output, "Started over with a clean draft.")
                continue

        # -- hand it to the agent (a fresh draft for each new request) ------
        if agent is None:
            agent = PlanningAgent(owner, llm, plan_date=plan_date)
        result = agent.submit(text)
        show_result(result, output, draft=agent.executor.draft)

        # -- plan is valid: the human decides -------------------------------
        if result.ready:
            if auto_approve:
                output("(--auto-approve: skipping the approval prompt)")
                decision = "approve"
            else:
                decision = ask_decision(input_fn, output)

            if decision == "approve":
                agent.commit()
                output("Plan approved and saved.\n")
                output(current_plan_text(owner, plan_date))
                output("")
                output(describe_owner(owner))
                output(DIVIDER)
                agent = None
                if one_shot:
                    return 0
            elif decision == "discard":
                output("Discarded. Nothing was saved.")
                output(DIVIDER)
                agent = None
                if one_shot:
                    return 1
            else:
                feedback = read_line("What should change? ", input_fn)
                if feedback is None or not feedback.strip():
                    output("No feedback given; discarded. Nothing was saved.")
                    output(DIVIDER)
                    agent = None
                    if one_shot:
                        return 1
                else:
                    pending = feedback
            continue

        # -- not ready: in one-shot mode we're done, otherwise let them reply -
        if one_shot:
            return 1
        # The agent gave up (it never found a valid plan). Its draft may hold edits the
        # owner never saw, so throw it away instead of building the next request on it.
        if result.status in (Status.UNRESOLVED, Status.MAX_ITERATIONS):
            output(
                "This attempt was discarded and nothing was saved. Your saved schedule "
                "is unchanged (type /plan to see it)."
            )
            output(DIVIDER)
            agent = None
            continue
        # Otherwise the agent is asking a question (or the model/API had a problem).
        # It keeps its conversation and draft, so whatever the user types next
        # continues it. /new throws the draft away and starts fresh.


def minutes_arg(text: str) -> int:
    """argparse type for an availability option: a whole number from 0 to 1440."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 0 or value > 1440:
        raise argparse.ArgumentTypeError("minutes must be between 0 and 1440")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Command-line options."""
    parser = argparse.ArgumentParser(description="PawPal+ planning agent")
    parser.add_argument("--request", help="handle one request, then exit")
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="save a valid plan without asking (for demos and scripted runs only)",
    )
    parser.add_argument("--owner", default="Owner", help="owner name (default: Owner)")
    for block in TIME_BLOCKS:
        parser.add_argument(
            f"--{block}", type=minutes_arg, default=60, metavar="MIN",
            help=f"minutes available in the {block} (default: 60)",
        )
    parser.add_argument(
        "--provider", choices=["gemini", "claude"],
        help="which LLM to use (default: LLM_PROVIDER from .env, else gemini)",
    )
    return parser


def main(argv: Optional[list] = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    setup_logging()

    constraints = {}
    for block in TIME_BLOCKS:
        constraints[block] = getattr(args, block)
    try:
        owner = Owner(name=args.owner, constraints=constraints)
        llm = create_client(args.provider)
    except (ValueError, LLMError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    if args.request is None:
        print("PawPal+ Planning Agent. Type /plan to see minutes used, /state for saved tasks,")
        print("/help for examples, /quit to leave.\n")
        print(describe_owner(owner))
        print()
    return run_session(
        owner, llm, date.today(),
        first_request=args.request, auto_approve=args.auto_approve,
    )


if __name__ == "__main__":
    sys.exit(main())
