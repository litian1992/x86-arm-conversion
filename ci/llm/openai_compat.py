"""OpenAI-compatible chat completions provider (OpenAI, Azure, Ollama, custom APIs)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from llm.base import LLMResponse, Message, ToolCall

# Status codes that are usually safe to retry (rate limit / transient).
_RETRYABLE_STATUS = {429, 502, 503, 504}


class OpenAICompatProvider:
    """Works with any API that implements the OpenAI chat completions schema."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        headers: dict[str, str] | None = None,
        temperature: float = 0.2,
        timeout: float = 120.0,
        max_retries: int = 5,
        retry_backoff: float = 5.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.extra_headers = headers or {}
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.5, retry_backoff)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _serialize_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                serialized.append(
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
            elif message.role == "tool":
                serialized.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content or "",
                    }
                )
            else:
                entry: dict[str, Any] = {"role": message.role, "content": message.content or ""}
                if message.name:
                    entry["name"] = message.name
                serialized.append(entry)
        return serialized

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 1.0)
            except ValueError:
                pass
        # Exponential backoff: 5s, 10s, 20s, ...
        return self.retry_backoff * (2 ** attempt)

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize_messages(messages),
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools

        url = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response: httpx.Response | None = None
            for attempt in range(self.max_retries + 1):
                response = await client.post(url, headers=self._headers(), json=payload)
                if response.status_code not in _RETRYABLE_STATUS or attempt >= self.max_retries:
                    break
                delay = self._retry_delay(response, attempt)
                print(
                    f"LLM {response.status_code} from {url}; "
                    f"retry {attempt + 1}/{self.max_retries} in {delay:.1f}s",
                    flush=True,
                )
                await asyncio.sleep(delay)

            assert response is not None
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                body = (response.text or "")[:500]
                raise httpx.HTTPStatusError(
                    f"{exc} | body: {body}",
                    request=exc.request,
                    response=exc.response,
                ) from None
            data = response.json()

        choice = data["choices"][0]
        message = choice.get("message") or {}
        tool_calls: list[ToolCall] = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                arguments = {"raw": raw_args}
            tool_calls.append(
                ToolCall(
                    id=item.get("id") or function.get("name", ""),
                    name=function.get("name", ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )

        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason") or "stop",
            raw=data,
        )
