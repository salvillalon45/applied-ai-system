"""Walk through one agent run, step by step, using a scripted (fake) Claude.

Run it from the project root:

    python demo_trace.py

No API key or network is needed. The "Claude" here is a MockLLMClient that replays a
fixed script, but everything else is the real code: the real PlanningAgent loop, the
real whitelisted tools, the real scheduler and the real verifier.

The scenario: the owner asks for two tasks that overlap. The verifier catches the
conflict, the (scripted) Claude fixes it, and the plan passes.
"""

from datetime import date

from agent.agent import PlanningAgent
from agent.llm import MockLLMClient, text_response, tool_use_response
from pawpal_system import Owner, Pet

REQUEST = "Rex needs a 30 minute walk at 08:00 and a 30 minute bath at 08:15."


class TracingMock(MockLLMClient):
    """A MockLLMClient that prints what the agent loop is doing on every pass."""

    def complete(self, system, messages, tools):
        """Print the loop state, then return the next scripted reply."""
        pass_number = len(self.calls) + 1
        print(f"\n{'=' * 70}\nLOOP PASS {pass_number}")

        # STEP 4 (CHECK): from pass 2 on, the last message holds the tool results
        # from the previous pass. This is what "Claude sees" before deciding again.
        last = messages[-1]
        if isinstance(last["content"], list):
            print("STEP 4 CHECK - results Claude now sees from the tools it called:")
            for block in last["content"]:
                label = "ERROR" if block["is_error"] else "ok"
                print(f"  [{label}] " + block["content"].replace("\n", "\n         "))
        else:
            print(f"(first pass: Claude receives the owner's request)")

        print(f"STEP 2 PLAN - sending {len(messages)} message(s) to Claude...")
        response = super().complete(system, messages, tools)

        if response.tool_calls:
            print("Claude replies: 'please run these tools':")
            for call in response.tool_calls:
                print(f"  STEP 3 ACT - {call.name}({call.input})")
        else:
            print(f"Claude replies with plain text (no tools): {response.text!r}")
            print("STEP 5 STOPPING RULES - agent now checks if the plan is really valid")
        return response


def main() -> None:
    # The owner's real data: one dog, 60 minutes free in the morning.
    owner = Owner(name="Sam", constraints={"morning": 60, "evening": 30})
    owner.add_pet(Pet(name="Rex", species="dog"))

    walk = {
        "pet_name": "Rex", "name": "walk", "duration": 30,
        "priority": "high", "preferred_time": "morning", "start_time": "08:00",
    }
    bath = dict(walk, name="bath", priority="medium", start_time="08:15")

    # What the fake Claude will do on each pass, in order.
    script = [
        tool_use_response(("get_state", {})),
        tool_use_response(("create_task", walk), ("create_task", bath)),
        tool_use_response(("generate_plan", {})),  # verifier will flag an overlap
        tool_use_response(
            ("edit_task", {"pet_name": "Rex", "task_name": "bath", "start_time": "09:00"})
        ),
        tool_use_response(("generate_plan", {})),  # now clean
        text_response("Moved the bath to 09:00 because it overlapped the 08:00 walk."),
    ]

    agent = PlanningAgent(owner, TracingMock(script), plan_date=date(2026, 11, 14))

    print(f"Owner's request: {REQUEST!r}")
    print(f"STEP 1 INPUT GUARDRAIL - request is non-empty and under the length limit")
    print(f"Real data before the run: Rex has {len(owner.pets[0].tasks)} task(s)")

    result = agent.submit(REQUEST)

    print(f"\n{'=' * 70}")
    print(f"STEP 6 ITERATION CAP - finished in {result.iterations} of "
          f"{agent.max_iterations} allowed passes")
    print(f"Status: {result.status.value}")
    print(f"Claude's summary: {result.message}")
    print(f"\nProposed plan:\n{result.plan_text}")
    print(f"\nVerifier: {result.report.summary()}")
    print(f"Real data still untouched: Rex has {len(owner.pets[0].tasks)} task(s)")

    # STEP 7: a human decides. Here we pretend they typed "y".
    print("\nSTEP 7 HUMAN IN THE LOOP - (pretending the user approves)")
    agent.commit()
    print(f"After commit(): Rex has {len(owner.pets[0].tasks)} task(s)")


if __name__ == "__main__":
    main()
