"""Tests for the agent loop (agent/agent.py), driven by a scripted mock LLM."""

from datetime import date

import pytest

from agent.agent import MAX_NUDGES, MAX_REQUEST_CHARS, PlanningAgent, Status
from agent.llm import LLMError, MockLLMClient, LLMResponse, text_response, tool_use_response
from pawpal_system import Owner, Pet

PLAN_DATE = date(2026, 11, 12)


@pytest.fixture
def owner():
    result = Owner(name="Sam", constraints={"morning": 60, "evening": 30})
    result.add_pet(Pet(name="Rex", species="dog"))
    return result


def make_agent(owner, script, **kwargs):
    llm = MockLLMClient(script, **kwargs.pop("llm_kwargs", {}))
    return PlanningAgent(owner, llm, plan_date=PLAN_DATE, **kwargs), llm


def walk(**overrides):
    args = {
        "pet_name": "Rex",
        "name": "walk",
        "duration": 30,
        "priority": "high",
        "preferred_time": "morning",
    }
    args.update(overrides)
    return args


# ------------------------------------------------------------- happy path


def test_happy_path_reaches_ready_without_touching_owner(owner):
    agent, llm = make_agent(
        owner,
        [
            tool_use_response(("get_state", {})),
            tool_use_response(("create_task", walk())),
            tool_use_response(("generate_plan", {})),
            text_response("Added a 30 minute morning walk for Rex."),
        ],
    )
    result = agent.submit("Rex needs a 30 minute walk every morning")

    assert result.status == Status.READY
    assert result.ready
    assert result.iterations == 4
    assert "walk" in result.plan_text
    assert result.report.passed
    assert owner.pets[0].tasks == []  # nothing committed yet


def test_commit_applies_draft_to_owner(owner):
    agent, _ = make_agent(
        owner,
        [
            tool_use_response(("create_task", walk()), ("generate_plan", {})),
            text_response("Done."),
        ],
    )
    assert agent.submit("walk Rex").ready
    agent.commit()
    assert owner.pets[0].tasks[0].name == "walk"

# Explain
def test_commit_refused_when_not_ready(owner):
    agent, _ = make_agent(owner, [text_response("Which pet?")])
    agent.submit("walk someone")
    with pytest.raises(ValueError):
        agent.commit()


def test_tool_results_are_sent_in_one_user_message(owner):
    agent, llm = make_agent(
        owner,
        [
            tool_use_response(
                ("create_task", walk()),
                ("create_task", walk(name="feed", preferred_time="evening")),
            ),
            tool_use_response(("generate_plan", {})),
            text_response("Done."),
        ],
    )
    agent.submit("walk and feed Rex")
    second_call_messages = llm.calls[1]["messages"]
    tool_result_message = second_call_messages[-1]
    assert tool_result_message["role"] == "user"
    assert len(tool_result_message["content"]) == 2
    assert tool_result_message["content"][0]["type"] == "tool_result"


# ------------------------------------------------------------ self-checking


def test_verifier_error_is_fed_back_and_agent_fixes_it(owner):
    overlap_a = walk(name="walk", start_time="08:00")
    overlap_b = walk(name="bath", start_time="08:15", duration=30)
    agent, llm = make_agent(
        owner,
        # Explain, what does this array represent?
        [
            tool_use_response(("create_task", overlap_a), ("create_task", overlap_b)),
            tool_use_response(("generate_plan", {})),  # verifier: conflict error
            tool_use_response(
                ("edit_task", {"pet_name": "Rex", "task_name": "bath", "start_time": "09:00"})
            ),
            tool_use_response(("generate_plan", {})),  # now clean
            text_response("Moved the bath to 09:00 to avoid a conflict."),
        ],
    )
    result = agent.submit("walk at 8 and a bath at 8:15")

    assert result.status == Status.READY
    # Claude was shown the verifier's error text on the turn after generate_plan
    feedback = llm.calls[2]["messages"][-1]["content"][0]["content"]
    assert "ERROR" in feedback


def test_agent_is_nudged_when_it_stops_on_an_unplanned_change(owner):
    agent, llm = make_agent(
        owner,
        [
            tool_use_response(("create_task", walk())),
            text_response("All set!"),  # premature: never generated a plan
            tool_use_response(("generate_plan", {})),
            text_response("Plan generated."),
        ],
    )
    result = agent.submit("walk Rex")
    assert result.status == Status.READY
    nudge = llm.calls[2]["messages"][-1]["content"]
    assert "generate_plan" in nudge


def test_agent_gives_up_after_max_nudges(owner):
    overlap = [
        tool_use_response(
            ("create_task", walk(name="a", start_time="08:00")),
            ("create_task", walk(name="b", start_time="08:10")),
            ("generate_plan", {}),
        )
    ]
    stubborn = [text_response("I can't fix that.")] * (MAX_NUDGES + 1)
    agent, _ = make_agent(owner, overlap + stubborn, max_iterations=10)

    result = agent.submit("two overlapping walks")
    assert result.status == Status.UNRESOLVED
    assert "can't fix" in result.message
    assert not result.report.passed


# --------------------------------------------------------------- guardrails


def test_iteration_cap_stops_a_runaway_loop(owner):
    forever = tool_use_response(("get_state", {}))
    agent, llm = make_agent(owner, [forever], max_iterations=3, llm_kwargs={"repeat_last": True})

    result = agent.submit("do something")
    assert result.status == Status.MAX_ITERATIONS
    assert result.iterations == 3
    assert len(llm.calls) == 3


def test_unknown_tool_is_reported_as_error_and_loop_continues(owner):
    agent, llm = make_agent(
        owner,
        [
            tool_use_response(("delete_all_pets", {})),
            text_response("Sorry, I can't do that."),
        ],
    )
    result = agent.submit("delete everything")

    tool_result = llm.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "unknown tool" in tool_result["content"]
    assert result.status == Status.NEEDS_INPUT  # nothing was changed


@pytest.mark.parametrize("text", ["", "   ", None, "x" * (MAX_REQUEST_CHARS + 1)])
def test_bad_input_is_rejected_before_any_llm_call(owner, text):
    agent, llm = make_agent(owner, [text_response("never used")])
    result = agent.submit(text)
    assert result.status == Status.REJECTED_INPUT
    assert llm.calls == []


def test_clarifying_question_is_needs_input(owner):
    agent, _ = make_agent(owner, [text_response("How long should the walk be?")])
    result = agent.submit("walk Rex")
    assert result.status == Status.NEEDS_INPUT
    assert "How long" in result.message


def test_api_failure_is_reported_not_raised(owner):
    agent, _ = make_agent(owner, [LLMError("could not reach the API")])
    result = agent.submit("walk Rex")
    assert result.status == Status.LLM_ERROR
    assert "could not reach" in result.message


def test_model_refusal_is_reported(owner):
    refusal = LLMResponse(stop_reason="refusal", content=[])
    agent, _ = make_agent(owner, [refusal])
    assert agent.submit("walk Rex").status == Status.REFUSED


def test_truncated_reply_is_an_error(owner):
    cut_off = LLMResponse(stop_reason="max_tokens", content=[])
    agent, _ = make_agent(owner, [cut_off])
    assert agent.submit("walk Rex").status == Status.LLM_ERROR


# ----------------------------------------------------------- human feedback


def test_revision_reuses_conversation_and_draft(owner):
    agent, llm = make_agent(
        owner,
        [
            tool_use_response(("create_task", walk()), ("generate_plan", {})),
            text_response("Added the walk."),
            tool_use_response(
                ("edit_task", {"pet_name": "Rex", "task_name": "walk", "duration": 20}),
                ("generate_plan", {}),
            ),
            text_response("Shortened the walk to 20 minutes."),
        ],
    )
    assert agent.submit("walk Rex for 30 minutes").ready
    result = agent.submit("actually make it 20 minutes")

    assert result.ready
    assert agent.executor.draft.pets[0].tasks[0].duration == 20
    # the second request saw the first exchange
    assert len(llm.calls[2]["messages"]) > len(llm.calls[0]["messages"])


def test_revision_with_no_changes_is_needs_input_not_ready(owner):
    agent, _ = make_agent(
        owner,
        [
            tool_use_response(("create_task", walk()), ("generate_plan", {})),
            text_response("Added the walk."),
            text_response("What time would you like instead?"),
        ],
    )
    assert agent.submit("walk Rex").ready
    result = agent.submit("move it")
    assert result.status == Status.NEEDS_INPUT
