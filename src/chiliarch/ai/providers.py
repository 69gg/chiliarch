"""Incremental model streams with provider-native reasoning replay.

Readable reasoning is emitted to the UI. Signatures and encrypted reasoning stay
inside the persisted native transcript and are never turned into display text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import time
from asyncio import sleep
from collections.abc import AsyncGenerator, AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from email.utils import mktime_tz, parsedate_tz
from typing import Any

import anthropic
import httpx
import openai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from chiliarch.config import ModelSettings

JSON = dict[str, Any]
logger = logging.getLogger(__name__)
INITIAL_RETRY_DELAY = 0.5
MAX_RETRY_DELAY = 8.0
MAX_RETRY_AFTER_DELAY = 120.0
PERMANENT_ERROR_CODES = frozenset(
    {
        "authentication_error",
        "permission_error",
        "invalid_request_error",
        "validation_error",
        "not_found_error",
        "request_too_large",
        "context_length_exceeded",
        "invalid_api_key",
        "insufficient_quota",
        "unsupported_parameter",
        "content_filter",
    }
)


class ModelError(RuntimeError):
    """A provider failed or returned an incomplete/unusable model turn."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        headers: httpx.Headers | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable, self.headers = retryable, headers


def _stream_error_retryable(body: object) -> bool:
    if isinstance(body, dict):
        details = body.get("error", body)
        if isinstance(details, dict):
            codes = (details.get("code"), details.get("type"))
            return not any(
                isinstance(code, str) and code.lower() in PERMANENT_ERROR_CODES for code in codes
            )
    # Compatible gateways may omit a typed code. An API error still uses the
    # configured bounded budget, unlike malformed local/model output.
    return True


def _error_headers(error: Exception) -> httpx.Headers | None:
    if isinstance(error, (openai.APIStatusError, anthropic.APIStatusError)):
        return error.response.headers
    return error.headers if isinstance(error, ModelError) else None


def _retry_after(headers: httpx.Headers | None) -> float | None:
    if headers is None:
        return None
    for name, divisor in (("retry-after-ms", 1000), ("retry-after", 1)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            delay = float(value) / divisor
        except ValueError:
            continue
        if math.isfinite(delay):
            return delay
    try:
        date = parsedate_tz(headers.get("retry-after", ""))
        return mktime_tz(date) - time.time() if date is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def retryable_model_error(error: Exception) -> bool:
    """Classify request and stream errors without exposing provider error bodies."""
    if isinstance(error, (openai.APIResponseValidationError, anthropic.APIResponseValidationError)):
        return False
    if isinstance(error, ModelError) and not error.retryable:
        return False
    headers = _error_headers(error)
    retry_after = _retry_after(headers)
    if retry_after is not None and retry_after > MAX_RETRY_AFTER_DELAY:
        return False
    if headers is not None:
        directive = headers.get("x-should-retry")
        if directive == "true":
            return True
        if directive == "false":
            return False
    if isinstance(
        error, (openai.APIConnectionError, anthropic.APIConnectionError, httpx.TransportError)
    ):
        return True
    if isinstance(error, (openai.APIStatusError, anthropic.APIStatusError)):
        if error.status_code >= 400:
            return error.status_code in {408, 409, 429} or error.status_code >= 500
        return _stream_error_retryable(error.body)
    if isinstance(error, (openai.APIError, anthropic.APIError)):
        return _stream_error_retryable(error.body)
    return error.retryable if isinstance(error, ModelError) else False


def _retry_delay(error: Exception, attempt: int) -> float:
    retry_after = _retry_after(_error_headers(error))
    if retry_after is not None and 0 < retry_after <= MAX_RETRY_AFTER_DELAY:
        return retry_after
    return float(min(INITIAL_RETRY_DELAY * 2**attempt, MAX_RETRY_DELAY) * random.uniform(0.75, 1.0))


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: JSON

    def as_dict(self) -> JSON:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True)
class ModelEvent:
    type: str
    data: JSON


@dataclass
class ModelTurn:
    """Completed turn; populated while events are yielded, never before."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    native: list[JSON] = field(default_factory=list)
    usage: JSON = field(default_factory=dict)

    def message(self, api_mode: str, model: str | None = None, base_url: str | None = None) -> JSON:
        return {
            "role": "assistant",
            "content": self.content,
            "reasoning": self.reasoning,
            "tool_calls": [call.as_dict() for call in self.tool_calls],
            "native": {
                "api_mode": api_mode,
                "model": model,
                "base_url": base_url,
                "items": deepcopy(self.native),
            },
        }


def _dump(value: Any) -> JSON:
    if isinstance(value, dict):
        return value
    result: JSON = value.model_dump(mode="json", exclude_none=True)
    return result


def _key(config: ModelSettings) -> str:
    return config.api_key.get_secret_value()


def _tool(call_id: Any, name: Any, raw: Any) -> ToolCall:
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        raise ModelError("The provider returned a tool call without an id or name")
    try:
        arguments = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError) as exc:
        raise ModelError(f"Invalid complete arguments for tool {name}") from exc
    if not isinstance(arguments, dict):
        raise ModelError(f"Arguments for tool {name} must be a JSON object")
    return ToolCall(call_id, name, arguments)


def _usage(value: JSON) -> JSON:
    input_tokens = value.get("input_tokens", value.get("prompt_tokens", 0)) or 0
    # Anthropic's input_tokens excludes cache hits and cache creation. OpenAI's
    # cached_tokens is a subset of input_tokens and must not be added again.
    input_tokens += (value.get("cache_creation_input_tokens") or 0) + (
        value.get("cache_read_input_tokens") or 0
    )
    output_tokens = value.get("output_tokens", value.get("completion_tokens", 0)) or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": value.get("total_tokens", input_tokens + output_tokens),
        "details": value,
    }


def _function(schema: JSON) -> JSON:
    function = schema.get("function", schema)
    result = {
        key: deepcopy(function[key])
        for key in ("name", "description", "parameters", "strict")
        if key in function
    }
    result.setdefault("strict", False)
    return result


def _reasoning_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_reasoning_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "thinking", "content"):
            if key in value:
                return _reasoning_text(value[key])
    return ""


def _merge(current: Any, incoming: Any) -> Any:
    if isinstance(current, str) and isinstance(incoming, str):
        return current + incoming
    if isinstance(current, list) and isinstance(incoming, list):
        return current + deepcopy(incoming)
    if isinstance(current, dict) and isinstance(incoming, dict):
        result = deepcopy(current)
        for key, value in incoming.items():
            result[key] = _merge(result.get(key), value)
        return result
    return deepcopy(incoming)


class Provider:
    """One configured provider snapshot and one HTTP client per model run.

    ``http_client`` is injectable for network-isolated behavioral tests. HTTP
    and stream errors share one retry budget. Once readable output is emitted,
    the request is never automatically repeated.
    """

    def __init__(self, config: ModelSettings, http_client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.api_mode: str = config.api_mode
        kwargs: JSON = {
            "api_key": _key(config) or "unconfigured",
            "timeout": config.timeout,
            "max_retries": 0,
            "default_headers": config.headers,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client: Any = (
            AsyncAnthropic(**kwargs) if self.api_mode == "anthropic" else AsyncOpenAI(**kwargs)
        )

    async def close(self) -> None:
        await self.client.close()

    def _native_matches(self, native: JSON) -> bool:
        return (
            native.get("api_mode") == self.api_mode
            and native.get("model") in {None, self.config.model}
            and native.get("base_url") in {None, self.config.base_url}
        )

    def _params(self) -> JSON:
        params: JSON = deepcopy(self.config.request_params)
        forbidden = {"model", "messages", "input", "tools", "stream", "instructions", "system"}
        if forbidden.intersection(params):
            raise ModelError("request_params cannot replace model, transcript, tools, or stream")
        if self.config.temperature is not None:
            params.setdefault("temperature", self.config.temperature)
        if self.config.reasoning_effort:
            if self.api_mode == "responses":
                params.setdefault("reasoning", {"effort": self.config.reasoning_effort})
            elif self.api_mode == "chat_completions":
                params.setdefault("reasoning_effort", self.config.reasoning_effort)
        if not self.config.enable_thinking_param_enabled:
            params.pop("enable_thinking", None)
        elif self.api_mode == "chat_completions" and self.config.reasoning_enabled:
            params.setdefault("enable_thinking", True)
        if self.api_mode == "anthropic" and self.config.thinking_enabled:
            if self.config.thinking_budget_tokens is None:
                params.setdefault("thinking", {"type": "adaptive"})
            else:
                params.setdefault(
                    "thinking",
                    {
                        "type": "enabled",
                        "budget_tokens": self.config.thinking_budget_tokens,
                    },
                )
        return params

    def context_payload(self, messages: list[JSON], tools: list[JSON], system: str) -> JSON:
        """Model-visible replay for estimation; never include authentication."""
        if self.api_mode == "chat_completions":
            history = self._chat_history(messages, system)
            schemas = [{"type": "function", "function": _function(tool)} for tool in tools]
        elif self.api_mode == "responses":
            history = self._responses_history(messages)
            schemas = [{"type": "function", **_function(tool)} for tool in tools]
        else:
            history = self._anthropic_history(messages)
            schemas = [
                {
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "input_schema": function.get("parameters", {}),
                }
                for function in map(_function, tools)
            ]
        return {
            "system": system if self.api_mode != "chat_completions" else "",
            "history": history,
            "tools": schemas,
        }

    def estimate_tokens(self, messages: list[JSON], tools: list[JSON], system: str) -> int:
        """UTF-8 byte estimate, used only until the provider reports usage."""
        rendered = json.dumps(self.context_payload(messages, tools, system), ensure_ascii=False)
        return (len(rendered.encode("utf-8")) + 3) // 4

    def context_key(self, tools: list[JSON], system: str) -> str:
        prefix = {
            "model": self.config.model,
            "base_url": self.config.base_url,
            "api_mode": self.api_mode,
            "reasoning_replay": self.config.reasoning_replay,
            "system_prompt_as_user": self.config.system_prompt_as_user,
            **self.context_payload([], tools, system),
        }
        return hashlib.sha256(json.dumps(prefix, sort_keys=True).encode()).hexdigest()

    async def stream(
        self,
        messages: list[JSON],
        tools: list[JSON],
        system_prompt: str,
        turn: ModelTurn,
    ) -> AsyncIterator[ModelEvent]:
        handlers = {
            "chat_completions": self._chat,
            "responses": self._responses,
            "anthropic": self._anthropic,
        }
        handler = handlers.get(self.api_mode)
        if handler is None:
            raise ModelError(f"Unsupported model API: {self.api_mode}")
        for attempt in range(self.config.retries + 1):
            candidate = ModelTurn()
            visible = False
            usage: ModelEvent | None = None
            iterator = handler(messages, tools, system_prompt, candidate)
            try:
                try:
                    async for event in iterator:
                        if event.type == "usage":
                            usage = event
                            continue
                        visible |= event.type in {"text_delta", "reasoning_delta"} and bool(
                            event.data.get("delta")
                        )
                        turn.content, turn.reasoning = candidate.content, candidate.reasoning
                        yield event
                finally:
                    await iterator.aclose()
            except Exception as exc:
                if visible or attempt >= self.config.retries or not retryable_model_error(exc):
                    raise
                delay = _retry_delay(exc, attempt)
                logger.warning(
                    "Model request retry scheduled",
                    extra={
                        "event": "model.request_retry",
                        "api_mode": self.api_mode,
                        "attempt": attempt + 2,
                        "max_attempts": self.config.retries + 1,
                        "delay_seconds": delay,
                        "error_type": type(exc).__name__,
                    },
                )
                await sleep(delay)
                continue
            turn.content, turn.reasoning = candidate.content, candidate.reasoning
            turn.tool_calls, turn.native, turn.usage = (
                candidate.tool_calls,
                candidate.native,
                candidate.usage,
            )
            if usage is not None:
                yield usage
            return

    def _chat_history(self, messages: list[JSON], system: str) -> list[JSON]:
        history: list[JSON] = [
            {
                "role": "user" if self.config.system_prompt_as_user else "system",
                "content": system,
            }
        ]
        for message in messages:
            native = message.get("native", {})
            if self._native_matches(native) and native.get("items"):
                item = deepcopy(native["items"][0])
                if not self.config.reasoning_replay:
                    for field_name in (
                        "reasoning_content",
                        "reasoning",
                        "thinking",
                        "reasoning_details",
                    ):
                        item.pop(field_name, None)
                history.append(item)
            elif message["role"] == "tool":
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": message["tool_call_id"],
                        "content": message["content"],
                    }
                )
            else:
                item = {"role": message["role"], "content": message["content"] or ""}
                if message.get("tool_calls"):
                    item["tool_calls"] = [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                            },
                        }
                        for call in message["tool_calls"]
                    ]
                history.append(item)
        return history

    async def _chat(
        self,
        messages: list[JSON],
        tools: list[JSON],
        system: str,
        turn: ModelTurn,
    ) -> AsyncGenerator[ModelEvent, None]:
        kwargs: JSON = {
            "model": self.config.model,
            "messages": self._chat_history(messages, system),
            "stream": True,
            "extra_body": self._params(),
        }
        if tools:
            kwargs["tools"] = [{"type": "function", "function": _function(tool)} for tool in tools]
        if self.config.tool_choice and tools:
            kwargs["tool_choice"] = self.config.tool_choice
        if self.config.include_usage:
            kwargs["stream_options"] = {"include_usage": True}
        calls: dict[int, JSON] = {}
        reasoning: JSON = {}
        finish: str | None = None
        stream = await self.client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                data = _dump(chunk)
                if data.get("usage"):
                    turn.usage = _usage(data["usage"])
                    yield ModelEvent("usage", turn.usage)
                for choice in data.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta", {})
                    for name in ("reasoning_content", "reasoning", "thinking", "reasoning_details"):
                        if name in delta and delta[name] is not None:
                            reasoning[name] = _merge(reasoning.get(name), delta[name])
                            text = _reasoning_text(delta[name])
                            if text:
                                turn.reasoning += text
                                yield ModelEvent("reasoning_delta", {"delta": text})
                    text = _reasoning_text(delta.get("content"))
                    if not text:
                        text = _reasoning_text(delta.get("refusal"))
                    if text:
                        turn.content += text
                        yield ModelEvent("text_delta", {"delta": text})
                    for incoming in delta.get("tool_calls") or []:
                        index = incoming.get("index", 0)
                        call = calls.setdefault(
                            index,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if incoming.get("id"):
                            call["id"] += incoming["id"]
                        function = incoming.get("function") or {}
                        for name in ("name", "arguments"):
                            if function.get(name) is not None:
                                call["function"][name] += function[name]
                    if choice.get("finish_reason") is not None:
                        finish = choice["finish_reason"]
        except (openai.APIError, httpx.TransportError) as exc:
            raise ModelError(
                f"Chat stream failed ({type(exc).__name__})",
                retryable=retryable_model_error(exc),
                headers=stream.response.headers,
            ) from exc
        finally:
            await stream.close()
        if finish not in {"stop", "tool_calls"}:
            raise ModelError(f"Model turn did not complete safely (finish_reason={finish})")
        native: JSON = {"role": "assistant", "content": turn.content or None, **reasoning}
        if calls:
            native["tool_calls"] = [calls[index] for index in sorted(calls)]
            turn.tool_calls = [
                _tool(call["id"], call["function"]["name"], call["function"]["arguments"])
                for call in native["tool_calls"]
            ]
        turn.native = [native]

    def _responses_history(self, messages: list[JSON]) -> list[JSON]:
        history: list[JSON] = []
        for message in messages:
            native = message.get("native", {})
            if self._native_matches(native) and native.get("items"):
                for item in native["items"]:
                    if item.get("type") == "reasoning" and not self.config.reasoning_replay:
                        continue
                    history.append(deepcopy(item))
            elif message["role"] == "tool":
                history.append(
                    {
                        "type": "function_call_output",
                        "call_id": message["tool_call_id"],
                        "output": message["content"],
                    }
                )
            elif message.get("tool_calls"):
                if message.get("content"):
                    history.append({"role": "assistant", "content": message["content"]})
                history.extend(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                    }
                    for call in message["tool_calls"]
                )
            else:
                history.append({"role": message["role"], "content": message["content"] or ""})
        return history

    async def _responses(
        self,
        messages: list[JSON],
        tools: list[JSON],
        system: str,
        turn: ModelTurn,
    ) -> AsyncGenerator[ModelEvent, None]:
        inputs = self._responses_history(messages)
        if self.config.system_prompt_as_user:
            inputs.insert(0, {"role": "user", "content": system})
        kwargs: JSON = {
            "model": self.config.model,
            "input": inputs,
            "stream": True,
            "store": False,
            "extra_body": self._params(),
        }
        if not self.config.system_prompt_as_user:
            kwargs["instructions"] = system
        if self.config.reasoning_replay:
            kwargs["include"] = ["reasoning.encrypted_content"]
        if tools:
            kwargs["tools"] = [{"type": "function", **_function(tool)} for tool in tools]
        if self.config.tool_choice and tools:
            kwargs["tool_choice"] = self.config.tool_choice
        done = False
        items: dict[int, JSON] = {}
        stream = await self.client.responses.create(**kwargs)
        try:
            async for event in stream:
                data = _dump(event)
                kind = data["type"]
                if kind in {"response.output_text.delta", "response.refusal.delta"}:
                    delta = data.get("delta", "")
                    turn.content += delta
                    yield ModelEvent("text_delta", {"delta": delta})
                elif kind in {
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                }:
                    delta = data.get("delta", "")
                    turn.reasoning += delta
                    yield ModelEvent("reasoning_delta", {"delta": delta})
                elif kind == "response.output_item.done":
                    items[data.get("output_index", len(items))] = data["item"]
                elif kind == "response.completed":
                    response = data["response"]
                    if response.get("status") not in {None, "completed"}:
                        raise ModelError("Responses returned a non-completed final response")
                    if response.get("output"):
                        turn.native = deepcopy(response["output"])
                    if response.get("usage"):
                        turn.usage = _usage(response["usage"])
                        yield ModelEvent("usage", turn.usage)
                    done = True
                elif kind in {"response.failed", "response.incomplete", "error"}:
                    failure = data.get("response", data)
                    raise ModelError(
                        f"Responses stream failed ({kind})",
                        retryable=kind != "response.incomplete"
                        and _stream_error_retryable(failure),
                        headers=stream.response.headers,
                    )
        except (openai.APIError, httpx.TransportError) as exc:
            raise ModelError(
                f"Responses stream failed ({type(exc).__name__})",
                retryable=retryable_model_error(exc),
                headers=stream.response.headers,
            ) from exc
        finally:
            await stream.close()
        if not done:
            raise ModelError("Responses stream ended without response.completed")
        if not turn.native:
            turn.native = [items[index] for index in sorted(items)]
        for item in turn.native:
            if item.get("type") == "function_call":
                if item.get("status") not in {None, "completed"}:
                    raise ModelError("Responses returned an incomplete function call")
                turn.tool_calls.append(
                    _tool(item.get("call_id"), item.get("name"), item.get("arguments"))
                )
        if not turn.reasoning:
            turn.reasoning = "".join(
                part.get("text", "")
                for item in turn.native
                if item.get("type") == "reasoning"
                for part in item.get("summary", [])
            )
            if turn.reasoning:
                yield ModelEvent("reasoning_delta", {"delta": turn.reasoning})
        if not turn.content:
            turn.content = "".join(
                block.get("text", "")
                for item in turn.native
                if item.get("type") == "message"
                for block in item.get("content", [])
                if block.get("type") == "output_text"
            )
            if turn.content:
                yield ModelEvent("text_delta", {"delta": turn.content})

    def _anthropic_history(self, messages: list[JSON]) -> list[JSON]:
        history: list[JSON] = []
        for message in messages:
            native = message.get("native", {})
            if self._native_matches(native) and native.get("items"):
                content = deepcopy(native["items"])
                if not self.config.reasoning_replay:
                    content = [
                        block
                        for block in content
                        if block.get("type") not in {"thinking", "redacted_thinking"}
                    ]
                role = "assistant"
            elif message["role"] == "tool":
                content = [
                    {
                        "type": "tool_result",
                        "tool_use_id": message["tool_call_id"],
                        "content": message["content"],
                        "is_error": message.get("is_error", False),
                    }
                ]
                role = "user"
            else:
                content = []
                if message.get("content"):
                    content.append({"type": "text", "text": message["content"]})
                if message.get("tool_calls"):
                    content.extend(
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["name"],
                            "input": call["arguments"],
                        }
                        for call in message["tool_calls"]
                    )
                role = message["role"]
            if not content:
                continue
            if history and history[-1]["role"] == role:
                history[-1]["content"].extend(content)
            else:
                history.append({"role": role, "content": content})
        return history

    async def _anthropic(
        self,
        messages: list[JSON],
        tools: list[JSON],
        system: str,
        turn: ModelTurn,
    ) -> AsyncGenerator[ModelEvent, None]:
        params = self._params()
        legacy_max_tokens = params.pop("max_tokens", None)
        max_tokens = self.config.anthropic_max_tokens
        if max_tokens is None:
            max_tokens = legacy_max_tokens
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ModelError(
                "Anthropic Messages requires a positive integer output limit. "
                "Set model.anthropic_max_tokens in the configuration or the WebUI "
                "Anthropic output limit field before sending a message."
            )
        history = self._anthropic_history(messages)
        if self.config.system_prompt_as_user:
            history.insert(0, {"role": "user", "content": [{"type": "text", "text": system}]})
        kwargs: JSON = {
            "model": self.config.model,
            "messages": history,
            "stream": True,
            "max_tokens": max_tokens,
            "extra_body": params,
        }
        if not self.config.system_prompt_as_user:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "input_schema": function.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                }
                for function in map(_function, tools)
            ]
        if self.config.tool_choice and tools:
            kwargs["tool_choice"] = {
                "type": {"required": "any"}.get(self.config.tool_choice, self.config.tool_choice)
            }
        blocks: dict[int, JSON] = {}
        arguments: dict[int, str] = {}
        finished_blocks: set[int] = set()
        done = False
        stop_reason: str | None = None
        native_usage: JSON = {}
        stream = await self.client.messages.create(**kwargs)
        try:
            async for event in stream:
                data = _dump(event)
                kind = data["type"]
                if kind == "message_start":
                    native_usage.update(data.get("message", {}).get("usage", {}))
                elif kind == "content_block_start":
                    block = deepcopy(data["content_block"])
                    blocks[data["index"]] = block
                    if block["type"] == "text" and block.get("text"):
                        turn.content += block["text"]
                        yield ModelEvent("text_delta", {"delta": block["text"]})
                    elif block["type"] == "thinking" and block.get("thinking"):
                        turn.reasoning += block["thinking"]
                        yield ModelEvent("reasoning_delta", {"delta": block["thinking"]})
                elif kind == "content_block_delta":
                    index = data["index"]
                    block = blocks[index]
                    delta = data["delta"]
                    delta_type = delta["type"]
                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        block["text"] = block.get("text", "") + text
                        turn.content += text
                        yield ModelEvent("text_delta", {"delta": text})
                    elif delta_type == "thinking_delta":
                        text = delta.get("thinking", "")
                        block["thinking"] = block.get("thinking", "") + text
                        turn.reasoning += text
                        yield ModelEvent("reasoning_delta", {"delta": text})
                    elif delta_type == "signature_delta":
                        block["signature"] = delta["signature"]
                    elif delta_type == "input_json_delta":
                        arguments[index] = arguments.get(index, "") + delta.get("partial_json", "")
                elif kind == "content_block_stop":
                    index = data["index"]
                    finished_blocks.add(index)
                    if index in arguments:
                        block = blocks[index]
                        call = _tool(block.get("id"), block.get("name"), arguments[index])
                        block["input"] = call.arguments
                elif kind == "message_delta":
                    native_usage.update(data.get("usage", {}))
                    stop_reason = data.get("delta", {}).get("stop_reason", stop_reason)
                elif kind == "message_stop":
                    done = True
                elif kind == "error":
                    raise ModelError(
                        "Anthropic returned a streaming error",
                        retryable=_stream_error_retryable(data),
                        headers=stream.response.headers,
                    )
        except (anthropic.APIError, httpx.TransportError) as exc:
            raise ModelError(
                f"Anthropic stream failed ({type(exc).__name__})",
                retryable=retryable_model_error(exc),
                headers=stream.response.headers,
            ) from exc
        finally:
            await stream.close()
        if not done or stop_reason not in {"end_turn", "tool_use", "stop_sequence"}:
            raise ModelError(f"Anthropic turn did not complete safely (stop_reason={stop_reason})")
        if set(blocks) != finished_blocks:
            raise ModelError("Anthropic stream ended before all content blocks completed")
        turn.native = [blocks[index] for index in sorted(blocks)]
        turn.tool_calls = [
            _tool(block.get("id"), block.get("name"), block.get("input", {}))
            for block in turn.native
            if block.get("type") == "tool_use"
        ]
        turn.usage = _usage(native_usage)
        yield ModelEvent("usage", turn.usage)
