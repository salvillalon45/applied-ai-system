"""Tests for the command-line session (main.py), using a scripted user and a mock LLM."""

from datetime import date

import pytest

from agent.llm import MockLLMClient, text_response, tool_use_response
from main import build_parser, describe_owner, main, run_session
from pawpal_system import Owner, Pet

PLAN_DATE = date(2026, 11, 14)


class ScriptedUser:
    """Feeds scripted answers to input() and records every prompt and printed line."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []
        self.printed = []

    def input(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def output(self, text):
        self.printed.append(text)

    @property
    def transcript(self):
        return "\n".join(self.printed)


@pytest.fixture
def owner():
    result = Owner(name="Sam", constraints={"morning": 60, "afternoon": 0, "evening": 30})
    result.add_pet(Pet(name="Rex", species="dog"))
    return result


def walk(**overrides):
    args = {
        "pet_name": "Rex", "name": "walk", "duration": 30,
        "priority": "high", "preferred_time": "morning",
    }
    args.update(overrides)
    return args


def good_plan_script(summary="Added a walk."):
    return [
        tool_use_response(("create_task", walk())),
        tool_use_response(("generate_plan", {})),
        text_response(summary),
    ]


def run(owner, script, answers, **kwargs):
    user = ScriptedUser(answers)
    code = run_session(
        owner, MockLLMClient(script), PLAN_DATE,
        input_fn=user.input, output=user.output, **kwargs,
    )
    return code, user


# --------------------------------------------------------------- approval


def test_approve_saves_the_plan(owner):
    code, user = run(owner, good_plan_script(), ["walk Rex", "y", "/quit"])
    assert code == 0
    assert owner.pets[0].tasks[0].name == "walk"
    assert "Plan approved and saved" in user.transcript


def test_discard_saves_nothing(owner):
    code, user = run(owner, good_plan_script(), ["walk Rex", "n", "/quit"])
    assert owner.pets[0].tasks == []
    assert "Nothing was saved" in user.transcript


def test_nothing_is_saved_before_the_user_answers(owner):
    # input ends right at the approval prompt (EOF) -> treated as discard
    run(owner, good_plan_script(), ["walk Rex"])
    assert owner.pets[0].tasks == []


def test_invalid_approval_answer_is_asked_again(owner):
    code, user = run(owner, good_plan_script(), ["walk Rex", "maybe", "y", "/quit"])
    assert "Please answer y, n or r" in user.transcript
    assert owner.pets[0].tasks[0].name == "walk"


def test_revise_sends_feedback_to_the_same_agent(owner):
    script = good_plan_script("Added a 30 minute walk.") + [
        tool_use_response(
            ("edit_task", {"pet_name": "Rex", "task_name": "walk", "duration": 20}),
            ("generate_plan", {}),
        ),
        text_response("Shortened to 20 minutes."),
    ]
    code, user = run(owner, script, ["walk Rex", "r", "make it 20 minutes", "y", "/quit"])
    assert owner.pets[0].tasks[0].duration == 20
    assert "What should change?" in "".join(user.prompts)


def test_revise_with_blank_feedback_discards(owner):
    code, user = run(owner, good_plan_script(), ["walk Rex", "r", "  ", "/quit"])
    assert owner.pets[0].tasks == []
    assert "No feedback given" in user.transcript


# ----------------------------------------------------- non-ready outcomes


def test_clarifying_question_is_shown_and_answer_continues_the_conversation(owner):
    script = [text_response("How long should the walk be?")] + good_plan_script()
    code, user = run(owner, script, ["walk Rex", "30 minutes", "y", "/quit"])
    assert "How long should the walk be?" in user.transcript
    assert "reply to the agent" in "".join(user.prompts)
    assert owner.pets[0].tasks[0].name == "walk"


def test_rejected_input_is_reported_and_not_sent_to_the_model(owner):
    llm = MockLLMClient([text_response("unused")])
    user = ScriptedUser(["x" * 2000, "/quit"])
    run_session(owner, llm, PLAN_DATE, input_fn=user.input, output=user.output)
    assert "too long" in user.transcript
    assert llm.calls == []


def test_blank_lines_are_ignored(owner):
    llm = MockLLMClient([])
    user = ScriptedUser(["", "   ", "/quit"])
    run_session(owner, llm, PLAN_DATE, input_fn=user.input, output=user.output)
    assert llm.calls == []


def test_new_command_starts_a_clean_draft(owner):
    script = [text_response("Which pet?")] + good_plan_script()
    code, user = run(owner, script, ["walk someone", "/new", "walk Rex", "y", "/quit"])
    assert "Started over" in user.transcript
    assert owner.pets[0].tasks[0].name == "walk"


# ------------------------------------------------------------ commands


def test_state_and_help_commands(owner):
    code, user = run(owner, [], ["/state", "/help", "/quit"])
    assert "Rex (dog)" in user.transcript
    assert "Commands:" in user.transcript


def test_ctrl_d_ends_the_session_cleanly(owner):
    code, user = run(owner, [], [])
    assert code == 0


def test_describe_owner_lists_tasks(owner):
    run(owner, good_plan_script(), ["walk Rex", "y", "/quit"])
    text = describe_owner(owner)
    assert "walk: 30 min, high priority, prefers morning, daily" in text
    assert "morning: 60" in text


# ------------------------------------------------------------- one-shot


def test_one_shot_with_auto_approve_saves_and_exits_zero(owner):
    code, user = run(
        owner, good_plan_script(), [], first_request="walk Rex", auto_approve=True
    )
    assert code == 0
    assert owner.pets[0].tasks[0].name == "walk"
    assert "--auto-approve" in user.transcript
    assert user.prompts == []  # never asked


def test_one_shot_still_asks_without_auto_approve(owner):
    code, user = run(owner, good_plan_script(), ["n"], first_request="walk Rex")
    assert code == 1
    assert owner.pets[0].tasks == []
    assert any("Approve this plan?" in prompt for prompt in user.prompts)


def test_one_shot_failure_exits_nonzero(owner):
    code, user = run(
        owner, [text_response("Which pet?")], [], first_request="walk", auto_approve=True
    )
    assert code == 1
    assert owner.pets[0].tasks == []


def test_one_shot_shows_verifier_errors_when_agent_cannot_fix_them(owner):
    conflict = tool_use_response(
        ("create_task", walk(name="a", start_time="08:00")),
        ("create_task", walk(name="b", start_time="08:10")),
        ("generate_plan", {}),
    )
    script = [conflict] + [text_response("I can't fix that.")] * 3
    code, user = run(owner, script, [], first_request="two walks", auto_approve=True)
    assert code == 1
    assert "could not produce a valid plan" in user.transcript
    assert "[ERROR] conflict" in user.transcript
    assert owner.pets[0].tasks == []


# ---------------------------------------------------------- arguments


def test_parser_defaults_and_overrides():
    args = build_parser().parse_args(["--morning", "45", "--request", "hi"])
    assert args.morning == 45
    assert args.evening == 60
    assert args.request == "hi"
    assert args.auto_approve is False


def test_main_reports_missing_key_without_crashing(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr("agent.llm.load_dotenv", lambda: None)
    monkeypatch.setattr("main.setup_logging", lambda: None)
    code = main(["--provider", "gemini", "--request", "walk Rex"])
    assert code == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["-5", "1441", "abc", "2.5"])
def test_parser_rejects_bad_availability(value, capsys):
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--morning", value])
    assert exit_info.value.code == 2  # argparse usage error, before any model is reached


# ------------------------------------------- the review screen shows what is saved


def test_review_screen_lists_frequency_and_preferred_time():
    from agent.agent import AgentResult, Status
    from main import show_result
    from pawpal_system import Frequency, Priority, Task

    draft = Owner(name="Sam", constraints={"morning": 60, "afternoon": 60, "evening": 60})
    pet = Pet(name="Ollie", species="dog")
    draft.add_pet(pet)
    pet.create_task(
        Task(
            name="Bath",
            description="bath",
            duration=30,
            priority=Priority.MEDIUM,
            frequency=Frequency.WEEKLY,
            preferred_time="evening",
        )
    )
    lines = []
    ready = AgentResult(status=Status.READY, message="ok", iterations=3, plan_text="plan")

    show_result(ready, lines.append, draft=draft)

    text = "\n".join(lines)
    assert "What approving will save" in text
    assert "weekly" in text
    assert "evening" in text


def test_review_screen_hides_the_draft_when_not_ready():
    from agent.agent import AgentResult, Status
    from main import show_result

    lines = []
    asking = AgentResult(status=Status.NEEDS_INPUT, message="Which time?", iterations=1)
    show_result(asking, lines.append, draft=Owner(name="Sam", constraints={}))
    assert "What approving will save" not in "\n".join(lines)


# ------------------------------------------------- /plan and what you see after saving


def test_plan_command_shows_minutes_used_from_saved_tasks(owner):
    first = good_plan_script()
    code, user = run(owner, first, ["walk Rex", "y", "/plan", "/quit"])
    # the /plan reply is the last block printed: 30 of the 60 morning minutes are used
    reply = ""
    for line in user.printed:
        if line.startswith("Plan for"):
            reply = line  # keep the last one: the /plan reply
    assert "Morning (30/60 min used)" in reply
    assert "Rex: walk" in reply


def test_plan_command_makes_no_model_call_and_changes_nothing(owner):
    llm = MockLLMClient([])
    user = ScriptedUser(["/plan", "/quit"])
    run_session(owner, llm, PLAN_DATE, input_fn=user.input, output=user.output)
    assert llm.calls == []
    assert owner.pets[0].tasks == []
    assert "Morning (0/60 min used)" in user.transcript


def test_approval_shows_the_current_plan_and_saved_tasks(owner):
    code, user = run(owner, good_plan_script(), ["walk Rex", "y", "/quit"])
    after = user.transcript.split("Plan approved and saved.")[1]
    assert "Morning (30/60 min used)" in after  # how much time is left
    assert "prefers morning" in after  # what was saved


def test_failed_attempt_is_discarded_so_the_next_request_starts_clean(owner):
    # Two overlapping walks the model never fixes -> UNRESOLVED, with edits in the draft.
    overlap = [
        tool_use_response(
            ("create_task", walk(name="a", start_time="08:00")),
            ("create_task", walk(name="b", start_time="08:10")),
            ("generate_plan", {}),
        )
    ]
    stubborn = [text_response("I can't fix that.")] * 3
    script = overlap + stubborn + good_plan_script()
    code, user = run(owner, script, ["two overlapping walks", "walk Rex", "y", "/quit"])

    assert "attempt was discarded" in user.transcript
    # the second request began from a fresh draft: only its own walk was saved
    names = []
    for task in owner.pets[0].tasks:
        names.append(task.name)
    assert names == ["walk"]
    # and the prompt went back to a new request, not "reply to the agent"
    assert user.prompts[1] == "You> "


# ------------------------------------------------- framing and reminders


def test_every_reply_is_framed_between_dividers(owner):
    from main import DIVIDER

    code, user = run(owner, [], ["/state", "/plan", "/help", "/quit"])
    # three command replies, each opened and closed by a divider
    assert user.printed.count(DIVIDER) == 6


def test_reminder_appears_after_a_reply_but_not_before_the_first_prompt(owner):
    from main import HINT

    code, user = run(owner, [], ["/state", "/quit"])
    hints = user.printed.count(HINT)
    assert hints == 1  # shown once, before the second You> prompt
    assert user.printed.index(HINT) > 0  # never before the first reply


def test_blank_lines_do_not_trigger_the_reminder(owner):
    from main import HINT

    code, user = run(owner, [], ["", "  ", "/quit"])
    assert HINT not in user.printed
