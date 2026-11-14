"""Reliability harness: run every case and print a results table.

    python -m eval.run_eval                      # offline, scripted mock model
    python -m eval.run_eval --live               # real model (uses your API key)
    python -m eval.run_eval --out eval/results.md

Every case is judged on the same rules ("invariants"), whatever the model does:
  1. It ends in one of the statuses the case allows.
  2. The owner's real data is untouched (nothing saved without approval).
  3. No pet the owner started with has disappeared from the draft.
  4. A READY plan passed the verifier.
  5. A READY plan still contains every duration the owner stated.
  6. The loop stayed under its iteration cap.
Exit code is 0 only if every case passes, so this can run in CI.
"""

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

from agent.agent import MAX_ITERATIONS, PlanningAgent, Status
from agent.llm import MockLLMClient, create_client
from eval.cases import PLAN_DATE, Case, build_cases, make_owner
from pawpal_system import Owner


@dataclass
class CaseOutcome:
    case: Case
    status: Optional[Status] = None
    iterations: int = 0
    failures: List[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.failures


def snapshot(owner: Owner) -> list:
    """A comparable picture of an owner's data (used to prove nothing changed)."""
    pets = []
    for pet in owner.pets:
        tasks = []
        for task in pet.tasks:
            tasks.append((task.name, task.duration, task.priority.name))
        pets.append((pet.name, pet.species, tasks))
    return [sorted(owner.constraints.items()), pets]


def check_invariants(case: Case, agent: PlanningAgent, result, before: list, live: bool) -> List[str]:
    """Return a list of broken rules (empty means the case passed)."""
    failures = []
    allowed = case.allowed(live)

    # 1. final status
    if result.status not in allowed:
        wanted = []
        for status in allowed:
            wanted.append(status.value)
        failures.append(f"status {result.status.value}, wanted {' or '.join(wanted)}")

    # 2. the real owner must be untouched (commit() was never called)
    if snapshot(agent.owner) != before:
        failures.append("the owner's real data changed without approval")

    # 3. no starting pet may vanish from the draft
    draft_names = []
    for pet in agent.executor.draft.pets:
        draft_names.append(pet.name)
    for pet in agent.owner.pets:
        if pet.name not in draft_names:
            failures.append(f"pet {pet.name} was removed from the draft")

    if result.status == Status.READY:
        # 4. READY means the verifier passed
        if result.report is None or not result.report.passed:
            failures.append("READY but the verifier did not pass")

        # 5. stated durations must survive
        if case.expect_durations:
            remaining = []
            for pet in agent.executor.draft.pets:
                for task in pet.tasks:
                    remaining.append(task.duration)
            for duration in case.expect_durations:
                if duration in remaining:
                    remaining.remove(duration)
                else:
                    failures.append(f"a stated {duration} minute duration is missing from the plan")

    # 6. iteration cap
    cap = case.max_iterations or MAX_ITERATIONS
    if result.iterations > cap:
        failures.append(f"used {result.iterations} iterations, cap is {cap}")

    return failures


def run_case(case: Case, live: bool, provider: Optional[str]) -> CaseOutcome:
    """Run one case end to end. A crash counts as a failure, not a stop."""
    outcome = CaseOutcome(case=case)
    owner = make_owner(**(case.constraints or {}))
    before = snapshot(owner)
    started = time.time()
    try:
        if live:
            llm = create_client(provider)
        else:
            llm = MockLLMClient(case.script(), repeat_last=case.repeat_last)
        agent = PlanningAgent(
            owner,
            llm,
            plan_date=PLAN_DATE,
            max_iterations=case.max_iterations or MAX_ITERATIONS,
        )
        result = agent.submit(case.request)
        outcome.status = result.status
        outcome.iterations = result.iterations
        outcome.failures = check_invariants(case, agent, result, before, live)
    except Exception as error:  # the harness must survive any single failure
        outcome.failures.append(f"crashed: {type(error).__name__}: {error}")
    outcome.seconds = time.time() - started
    return outcome


def format_table(outcomes: List[CaseOutcome], live: bool) -> str:
    """The markdown table that goes into the README and model card."""
    mode = "live model" if live else "offline mock"
    lines = [
        f"Mode: {mode}",
        "",
        "| Case | What it checks | Result | Status | Calls | Notes |",
        "|---|---|---|---|---|---|",
    ]
    for outcome in outcomes:
        verdict = "PASS" if outcome.passed else "FAIL"
        status = outcome.status.value if outcome.status else "-"
        notes = "; ".join(outcome.failures)
        lines.append(
            f"| {outcome.case.id} | {outcome.case.description} | {verdict} "
            f"| {status} | {outcome.iterations} | {notes} |"
        )
    passed = 0
    for outcome in outcomes:
        if outcome.passed:
            passed += 1
    lines.append("")
    lines.append(f"**{passed}/{len(outcomes)} cases passed** ({mode}).")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PawPal+ agent reliability harness")
    parser.add_argument("--live", action="store_true", help="use the real model")
    parser.add_argument("--provider", help="gemini or claude (live mode only)")
    parser.add_argument("--case", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--out", help="also write the results table to this file")
    parser.add_argument(
        "--delay", type=float, default=2.0, help="seconds between live cases (rate limits)"
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    cases = []
    for case in build_cases():
        if args.case and case.id not in args.case:
            continue
        if args.live and case.offline_only:
            continue  # needs a scripted failure the real model won't produce on demand
        cases.append(case)
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 2

    outcomes = []
    for index, case in enumerate(cases):
        if args.live and index > 0:
            time.sleep(args.delay)
        outcome = run_case(case, args.live, args.provider)
        print(f"{'PASS' if outcome.passed else 'FAIL'}  {case.id}", file=sys.stderr)
        outcomes.append(outcome)

    table = format_table(outcomes, args.live)
    print(table)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(table + "\n")

    for outcome in outcomes:
        if not outcome.passed:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
