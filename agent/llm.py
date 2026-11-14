"""LLM client interface for the PawPal+ agent.

The agent loop only talks to LLMClient, never to a provider SDK. That gives us:

  - GeminiClient: Google's Gemini API, using the google-genai SDK.
  - ClaudeClient: Anthropic's Claude API, using the Anthropic SDK.
  - MockLLMClient: scripted responses, so tests and the eval harness run offline,
    deterministically and for free.

create_client() picks one from the LLM_PROVIDER env var ("gemini" or "claude").

Switching providers means writing one more LLMClient subclass. The agent keeps its
messages and tool schemas in Anthropic's shape, and each client translates them to
its own provider's format.
"""

import itertools
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import anthropic
import httpx2  # the HTTP library the current Anthropic and Gemini SDKs are built on
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

DEFAULT_MODEL = "claude-opus-5-5"  # override with the PAWPAL_MODEL env var
DEFAULT_EFFORT = "medium"  # low | medium | high | xhigh | max
MAX_TOKENS = 16000

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"  # override with the GEMINI_MODEL env var
RETRY_DELAYS = (2, 6, 15)  # seconds to wait before each retry on 429 / 5xx


class LLMError(Exception):
    """The LLM could not be reached or returned something unusable."""


@dataclass
class ToolCall:
    """One tool call requested by the model."""
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class LLMResponse:
    """A provider-neutral view of one model reply."""
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn, tool_use, max_tokens, refusal, ...
    # The raw assistant content, replayed unchanged in the next request.
    content: Any = None
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(ABC):
    """Anything that can take a conversation plus tool schemas and reply."""

    # Explain
    @abstractmethod
    def complete(
        self, system: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> LLMResponse:
        """Return the model's next reply. Raises LLMError on failure."""


class ClaudeClient(LLMClient):
    """Calls the Claude API through the official Anthropic SDK."""

    def __init__(self, model: Optional[str] = None, effort: str = DEFAULT_EFFORT):
        """Load the API key from .env / the environment and build the SDK client."""
        load_dotenv()
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        self.model = model or os.environ.get("PAWPAL_MODEL") or DEFAULT_MODEL
        self.effort = effort
        self._client = anthropic.Anthropic()  # SDK retries 429/5xx twice by default

    def complete(
        self, system: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> LLMResponse:
        """Send one request and convert the reply into an LLMResponse."""
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                tools=tools,
                output_config={"effort": self.effort},
            )
        except anthropic.AuthenticationError as error:
            raise LLMError("the API key was rejected; check ANTHROPIC_API_KEY") from error
        except anthropic.RateLimitError as error:
            raise LLMError("rate limited by the API; try again shortly") from error
        except anthropic.APIConnectionError as error:
            raise LLMError("could not reach the API; check your connection") from error
        except anthropic.APIStatusError as error:
            raise LLMError(f"API error {error.status_code}: {error.message}") from error

        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        return LLMResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            content=response.content,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def _convert_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one of our JSON-schema fragments into the subset Gemini accepts.

    Differences handled here: type names are upper-case, a ["string", "null"] type
    becomes type STRING plus nullable, and keywords Gemini doesn't take (such as
    maxLength) are dropped. The executor still enforces those limits itself.
    """
    result: Dict[str, Any] = {}

    type_spec = schema.get("type")
    if isinstance(type_spec, list):
        real_types = []
        for name in type_spec:
            if name == "null":
                result["nullable"] = True
            else:
                real_types.append(name)
        type_spec = real_types[0]
    if type_spec:
        result["type"] = type_spec.upper()

    for key in ("description", "enum"):
        if key in schema:
            result[key] = schema[key]

    if "properties" in schema:
        result["properties"] = {}
        for name, sub_schema in schema["properties"].items():
            result["properties"][name] = _convert_schema(sub_schema)
    if schema.get("required"):
        result["required"] = list(schema["required"])
    return result


def to_gemini_tools(tools: List[Dict[str, Any]]) -> List[Any]:
    """Convert our tool schemas into a Gemini Tool holding function declarations."""
    declarations = []
    for tool in tools:
        fields: Dict[str, Any] = {"name": tool["name"], "description": tool["description"]}
        # Gemini rejects an OBJECT with no properties, so tools with no arguments
        # (get_state, generate_plan) are declared without a parameters block.
        if tool["input_schema"].get("properties"):
            fields["parameters"] = _convert_schema(tool["input_schema"])
        declarations.append(genai_types.FunctionDeclaration(**fields))
    return [genai_types.Tool(function_declarations=declarations)]


class GeminiClient(LLMClient):
    """Calls the Gemini API through the google-genai SDK."""

    def __init__(
        self,
        model: Optional[str] = None,
        client: Any = None,
        retry_delays: Sequence[float] = RETRY_DELAYS,
    ):
        """Load the key from .env / the environment. `client` is for tests only."""
        load_dotenv()
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        self.retry_delays = tuple(retry_delays)
        self._call_names: Dict[str, str] = {}  # tool call id -> tool name
        self._ids = itertools.count(1)

        if client is not None:
            self._client = client
            return
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise LLMError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        self._client = genai.Client(api_key=api_key)

    def complete(
        self, system: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> LLMResponse:
        """Send one request and convert the reply into an LLMResponse."""
        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            tools=to_gemini_tools(tools),
            max_output_tokens=MAX_TOKENS,
            # We run the tool loop ourselves; don't let the SDK call tools for us.
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = self._to_contents(messages)
        response = self._generate_with_retry(contents, config)
        return self._to_llm_response(response)

    # -- request side -----------------------------------------------------

    def _to_contents(self, messages: List[Dict[str, Any]]) -> List[Any]:
        """Translate the agent's messages into Gemini Content objects."""
        contents = []
        for message in messages:
            content = message["content"]

            if message["role"] == "assistant":
                if isinstance(content, genai_types.Content):
                    # Replay Gemini's own reply untouched. This keeps the thought
                    # signatures that Gemini 3 requires on function-calling turns.
                    contents.append(content)
                elif isinstance(content, str):
                    contents.append(
                        genai_types.Content(role="model", parts=[genai_types.Part(text=content)])
                    )
                else:
                    raise LLMError("unexpected assistant message in the conversation")
                continue

            if isinstance(content, str):
                parts = [genai_types.Part(text=content)]
            else:
                parts = []
                for block in content:  # a list of tool_result blocks
                    name = self._call_names.get(block["tool_use_id"])
                    if name is None:
                        raise LLMError(f"unknown tool call id {block['tool_use_id']!r}")
                    key = "error" if block.get("is_error") else "output"
                    parts.append(
                        genai_types.Part.from_function_response(
                            name=name, response={key: block["content"]}
                        )
                    )
            contents.append(genai_types.Content(role="user", parts=parts))
        return contents

    def _generate_with_retry(self, contents: List[Any], config: Any) -> Any:
        """Call the API, waiting and retrying on rate limits and server errors."""
        attempt = 0
        while True:
            try:
                return self._client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except genai_errors.APIError as error:
                retryable = error.code in (429, 500, 503)
                if retryable and attempt < len(self.retry_delays):
                    time.sleep(self.retry_delays[attempt])
                    attempt += 1
                    continue
                if error.code == 429:
                    raise LLMError(
                        "rate limited by the Gemini API (free-tier limits are low); wait a minute"
                    ) from error
                if error.code in (401, 403):
                    raise LLMError("the Gemini API key was rejected") from error
                raise LLMError(f"Gemini API error {error.code}: {error.message}") from error
            except httpx2.HTTPError as error:
                raise LLMError("could not reach the Gemini API; check your connection") from error

    # -- response side ----------------------------------------------------

    def _to_llm_response(self, response: Any) -> LLMResponse:
        """Convert a Gemini reply into an LLMResponse."""
        usage = response.usage_metadata
        input_tokens = (usage.prompt_token_count or 0) if usage else 0
        output_tokens = (usage.candidates_token_count or 0) if usage else 0

        if not response.candidates:
            # No candidate usually means the prompt itself was blocked.
            return LLMResponse(
                stop_reason="refusal", content=None,
                input_tokens=input_tokens, output_tokens=output_tokens,
            )
        candidate = response.candidates[0]
        reason = candidate.finish_reason.name if candidate.finish_reason else "STOP"

        if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"):
            return LLMResponse(
                stop_reason="refusal", content=None,
                input_tokens=input_tokens, output_tokens=output_tokens,
            )
        if reason not in ("STOP", "MAX_TOKENS"):
            raise LLMError(f"Gemini stopped unexpectedly ({reason})")

        text_parts = []
        tool_calls = []
        parts = []
        if candidate.content and candidate.content.parts:
            parts = candidate.content.parts
        for part in parts:
            if part.function_call:
                call_id = part.function_call.id or f"gemini_call_{next(self._ids)}"
                self._call_names[call_id] = part.function_call.name
                tool_calls.append(
                    ToolCall(
                        id=call_id,
                        name=part.function_call.name,
                        input=dict(part.function_call.args or {}),
                    )
                )
            elif part.text and not part.thought:
                text_parts.append(part.text)

        if not parts:
            raise LLMError("Gemini returned an empty reply")

        stop_reason = "end_turn"
        if reason == "MAX_TOKENS":
            stop_reason = "max_tokens"
        elif tool_calls:
            stop_reason = "tool_use"
        return LLMResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            content=candidate.content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def create_client(provider: Optional[str] = None) -> LLMClient:
    """Build the real LLM client named by `provider` or the LLM_PROVIDER env var."""
    load_dotenv()
    name = (provider or os.environ.get("LLM_PROVIDER") or "gemini").lower()
    if name == "gemini":
        return GeminiClient()
    if name == "claude":
        return ClaudeClient()
    raise LLMError(f"unknown LLM_PROVIDER {name!r}; use 'gemini' or 'claude'")


# ---------------------------------------------------------------------------
# Mock client for tests and the offline eval harness
# ---------------------------------------------------------------------------

_call_ids = itertools.count(1)


def tool_use_response(*calls: Sequence[Any]) -> LLMResponse:
    """Build a scripted reply that requests tools. Each call is (name, input_dict)."""
    tool_calls = []
    content = []
    for name, tool_input in calls:
        call_id = f"toolu_mock_{next(_call_ids)}"
        tool_calls.append(ToolCall(id=call_id, name=name, input=tool_input))
        content.append({"type": "tool_use", "id": call_id, "name": name, "input": tool_input})
    return LLMResponse(tool_calls=tool_calls, stop_reason="tool_use", content=content)


def text_response(text: str, stop_reason: str = "end_turn") -> LLMResponse:
    """Build a scripted reply that is plain text (no tool calls)."""
    return LLMResponse(
        text=text,
        stop_reason=stop_reason,
        content=[{"type": "text", "text": text}],
    )


class MockLLMClient(LLMClient):
    """Replays a fixed script of responses, one per complete() call.

    An item in the script can be an LLMResponse, or an LLMError instance to raise.
    After the script runs out, repeat_last=True keeps returning the final item
    (useful for simulating an agent stuck in a loop).
    """

    def __init__(self, script: List[Any], repeat_last: bool = False):
        self.script = list(script)
        self.repeat_last = repeat_last
        self.calls: List[Dict[str, Any]] = []  # what the agent sent, for assertions

    def complete(
        self, system: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> LLMResponse:
        """Return the next scripted reply."""
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        index = len(self.calls) - 1
        if index >= len(self.script):
            if self.repeat_last and self.script:
                index = len(self.script) - 1
            else:
                raise LLMError("mock script exhausted")
        item = self.script[index]
        if isinstance(item, Exception):
            raise item
        return item
