"""Tests for GeminiClient. A fake SDK client is injected, so no network or key is used."""

from datetime import date

import pytest
from google.genai import errors as genai_errors
from google.genai import types

from agent.agent import PlanningAgent, Status
from agent.llm import GeminiClient, LLMError, create_client, to_gemini_tools
from agent.tools import TOOL_SCHEMAS
from pawpal_system import Owner, Pet


# ---------------------------------------------------------------- test doubles


def reply(parts, finish=types.FinishReason.STOP):
    """Build a Gemini response holding one candidate."""
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=parts), finish_reason=finish
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10, candidates_token_count=5
        ),
    )


def call_part(name, args=None):
    return types.Part(function_call=types.FunctionCall(name=name, args=args or {}))


def text_part(text):
    return types.Part(text=text)


class FakeModels:
    """Stands in for client.models. Replays items; exceptions in the list are raised."""

    def __init__(self, items):
        self.items = list(items)
        self.requests = []

    def generate_content(self, model, contents, config):
        self.requests.append({"model": model, "contents": list(contents), "config": config})
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSDK:
    def __init__(self, items):
        self.models = FakeModels(items)


def make_client(items, **kwargs):
    sdk = FakeSDK(items)
    return GeminiClient(client=sdk, retry_delays=kwargs.pop("retry_delays", ()), **kwargs), sdk


def api_error(code):
    return genai_errors.ClientError(code, {"error": {"message": f"error {code}"}}, None)


# ------------------------------------------------------------- tool schemas


def test_every_tool_converts_to_a_gemini_declaration():
    declarations = to_gemini_tools(TOOL_SCHEMAS)[0].function_declarations
    names = []
    for declaration in declarations:
        names.append(declaration.name)
    assert names == [schema["name"] for schema in TOOL_SCHEMAS]


def test_tools_without_arguments_have_no_parameters_block():
    declarations = {}
    for declaration in to_gemini_tools(TOOL_SCHEMAS)[0].function_declarations:
        declarations[declaration.name] = declaration
    assert declarations["get_state"].parameters is None
    assert declarations["create_task"].parameters is not None
    assert declarations["generate_plan"].parameters is not None  # takes optional plan_date


def test_nullable_type_and_enum_are_converted():
    declarations = {}
    for declaration in to_gemini_tools(TOOL_SCHEMAS)[0].function_declarations:
        declarations[declaration.name] = declaration
    props = declarations["edit_task"].parameters.properties
    assert props["start_time"].nullable is True
    assert props["start_time"].type == types.Type.STRING
    assert props["priority"].enum == ["high", "medium", "low"]


# ------------------------------------------------------------ parsing replies


def test_function_call_is_parsed_with_a_generated_id():
    client, _ = make_client([reply([call_part("create_task", {"name": "walk"})])])
    response = client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)

    assert response.stop_reason == "tool_use"
    assert response.tool_calls[0].name == "create_task"
    assert response.tool_calls[0].input == {"name": "walk"}
    assert response.tool_calls[0].id.startswith("gemini_call_")
    assert response.input_tokens == 10 and response.output_tokens == 5


def test_plain_text_reply():
    client, _ = make_client([reply([text_part("All done.")])])
    response = client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert response.stop_reason == "end_turn"
    assert response.text == "All done."
    assert response.tool_calls == []


def test_thinking_parts_are_not_treated_as_the_answer():
    thought = types.Part(text="private reasoning", thought=True)
    client, _ = make_client([reply([thought, text_part("Final answer.")])])
    response = client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert response.text == "Final answer."


def test_max_tokens_finish_reason():
    client, _ = make_client([reply([text_part("cut")], finish=types.FinishReason.MAX_TOKENS)])
    response = client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert response.stop_reason == "max_tokens"


@pytest.mark.parametrize(
    "finish", [types.FinishReason.SAFETY, types.FinishReason.PROHIBITED_CONTENT]
)
def test_safety_blocks_become_refusals(finish):
    client, _ = make_client([reply([text_part("x")], finish=finish)])
    response = client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert response.stop_reason == "refusal"


def test_no_candidates_means_refusal():
    empty = types.GenerateContentResponse(candidates=[])
    client, _ = make_client([empty])
    response = client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert response.stop_reason == "refusal"


def test_malformed_function_call_is_an_llm_error():
    client, _ = make_client(
        [reply([text_part("x")], finish=types.FinishReason.MALFORMED_FUNCTION_CALL)]
    )
    with pytest.raises(LLMError, match="MALFORMED_FUNCTION_CALL"):
        client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)


def test_empty_reply_is_an_llm_error():
    client, _ = make_client([reply([])])
    with pytest.raises(LLMError, match="empty"):
        client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)


# ------------------------------------------------------------ sending history


def test_tool_results_are_sent_back_as_function_responses():
    client, sdk = make_client(
        [reply([call_part("get_state")]), reply([text_part("ok")])]
    )
    first = client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    call = first.tool_calls[0]

    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call.id, "content": "state...", "is_error": False}
            ],
        },
    ]
    client.complete("sys", history, TOOL_SCHEMAS)

    sent = sdk.models.requests[1]["contents"]
    assert [c.role for c in sent] == ["user", "model", "user"]
    assert sent[1] is first.content  # replayed untouched (keeps thought signatures)
    function_response = sent[2].parts[0].function_response
    assert function_response.name == "get_state"
    assert function_response.response == {"output": "state..."}


def test_tool_errors_use_the_error_key():
    client, sdk = make_client([reply([call_part("add_pet")]), reply([text_part("ok")])])
    first = client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": first.tool_calls[0].id,
                 "content": "bad input", "is_error": True}
            ],
        },
    ]
    client.complete("sys", history, TOOL_SCHEMAS)
    sent = sdk.models.requests[1]["contents"]
    assert sent[2].parts[0].function_response.response == {"error": "bad input"}


def test_unknown_tool_call_id_is_an_error():
    client, _ = make_client([reply([text_part("ok")])])
    history = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "nope", "content": "x", "is_error": False}
        ]}
    ]
    with pytest.raises(LLMError, match="unknown tool call id"):
        client.complete("sys", history, TOOL_SCHEMAS)


def test_automatic_function_calling_is_disabled_and_system_prompt_sent():
    client, sdk = make_client([reply([text_part("ok")])])
    client.complete("be helpful", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    config = sdk.models.requests[0]["config"]
    assert config.automatic_function_calling.disable is True
    assert config.system_instruction == "be helpful"


# ---------------------------------------------------------- errors and retry


def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("agent.llm.time.sleep", sleeps.append)
    client, sdk = make_client(
        [api_error(429), api_error(429), reply([text_part("finally")])],
        retry_delays=(1, 2, 3),
    )
    response = client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert response.text == "finally"
    assert sleeps == [1, 2]
    assert len(sdk.models.requests) == 3


def test_rate_limit_gives_up_with_a_clear_message(monkeypatch):
    monkeypatch.setattr("agent.llm.time.sleep", lambda seconds: None)
    client, _ = make_client([api_error(429)] * 3, retry_delays=(1, 1))
    with pytest.raises(LLMError, match="rate limited"):
        client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)


def test_bad_key_is_not_retried():
    client, sdk = make_client([api_error(403)], retry_delays=(1, 1))
    with pytest.raises(LLMError, match="key was rejected"):
        client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert len(sdk.models.requests) == 1


def test_missing_key_raises_a_helpful_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr("agent.llm.load_dotenv", lambda: None)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiClient()


def test_model_can_be_set_by_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    client, sdk = make_client([reply([text_part("ok")])])
    client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert sdk.models.requests[0]["model"] == "gemini-test-model"


def test_create_client_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr("agent.llm.load_dotenv", lambda: None)
    with pytest.raises(LLMError, match="unknown LLM_PROVIDER"):
        create_client("llama")


# ------------------------------------------------- the whole loop with Gemini


def test_planning_agent_runs_end_to_end_on_the_gemini_client():
    """The real agent loop + real tools, with only the Gemini SDK faked."""
    owner = Owner(name="Sam", constraints={"morning": 60})
    owner.add_pet(Pet(name="Rex", species="dog"))
    walk = {
        "pet_name": "Rex", "name": "walk", "duration": 30,
        "priority": "high", "preferred_time": "morning",
    }
    client, sdk = make_client(
        [
            reply([call_part("create_task", walk)]),
            reply([call_part("generate_plan")]),
            reply([text_part("Added a 30 minute walk.")]),
        ]
    )
    agent = PlanningAgent(owner, client, plan_date=date(2026, 11, 14))

    result = agent.submit("Rex needs a 30 minute walk in the morning")

    assert result.status == Status.READY
    assert result.iterations == 3
    # the 3rd request carried the full history: user, model, user(results), model, user(results)
    assert len(sdk.models.requests[2]["contents"]) == 5
    agent.commit()
    assert owner.pets[0].tasks[0].name == "walk"


# ------------------------------------------------------- slow or throttled calls are logged


def test_retries_are_logged_with_the_status_code(monkeypatch, caplog):
    monkeypatch.setattr("agent.llm.time.sleep", lambda seconds: None)
    client, _ = make_client(
        [api_error(503), reply([text_part("ok")])], retry_delays=(2, 6)
    )
    with caplog.at_level("WARNING", logger="pawpal.agent"):
        client.complete("sys", [{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert "returned 503" in caplog.text
    assert "retry 1 of 2" in caplog.text
