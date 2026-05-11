"""Async Ollama client with tool calling support."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from core.config import AppConfig


_ollama_lock = asyncio.Lock()


class OllamaFunction(BaseModel):
    """Function payload returned by Ollama tool calls."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class OllamaToolCall(BaseModel):
    """Structured tool call extracted from an Ollama response."""

    id: str | None = None
    type: Literal["function"] = "function"
    function: OllamaFunction


class ChatMessage(BaseModel):
    """Single chat message exchanged with Ollama."""

    model_config = ConfigDict(populate_by_name=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = Field(default=None, alias="tool_call_id")
    tool_calls: list[OllamaToolCall] = Field(default_factory=list)

    def to_ollama_dict(self) -> dict[str, Any]:
        """Serialize the message in the subset of fields Ollama expects."""

        payload: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            payload["content"] = self.content
        if self.name is not None:
            payload["name"] = self.name
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            payload["tool_calls"] = [tool_call.model_dump(exclude_none=True) for tool_call in self.tool_calls]
        return payload


class ChatResponse(BaseModel):
    """Normalized Ollama chat response."""

    model: str
    created_at: str | None = None
    message: ChatMessage
    done: bool
    done_reason: str | None = None
    total_duration: int | None = None
    load_duration: int | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None


class ModelInfo(BaseModel):
    """Minimal model information returned by Ollama."""

    name: str


class OllamaLLMClient:
    """Async HTTP client for Ollama chat requests."""

    def __init__(self, config: AppConfig, timeout: float = 120.0) -> None:
        """Initialize the client with application settings."""

        self._config = config
        self._client = httpx.AsyncClient(
            base_url=str(config.ollama_base_url).rstrip("/"),
            timeout=timeout,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""

        await self._client.aclose()

    async def health_check(self) -> bool:
        """Return whether Ollama is reachable."""

        try:
            response = await self._client.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("Ollama health check failed")
            return False
        return True

    async def list_models(self) -> list[ModelInfo]:
        """List locally available Ollama models."""

        response = await self._client.get("/api/tags")
        response.raise_for_status()
        payload = response.json()
        return [ModelInfo.model_validate(item) for item in payload.get("models", [])]

    async def ensure_models_available(self, models: list[str]) -> dict[str, bool]:
        """Return availability status for the requested models."""

        available_models = {item.name for item in await self.list_models()}
        return {model_name: model_name in available_models for model_name in models}

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        options: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """Send a chat request to Ollama and return a normalized response."""

        async with _ollama_lock:
            selected_model = model or self._config.ollama_primary_model
            payload: dict[str, Any] = {
                "model": selected_model,
                "messages": [message.to_ollama_dict() for message in messages],
                "stream": stream,
            }
            if tools:
                payload["tools"] = tools
            if options:
                payload["options"] = options

            logger.debug("Sending chat request to Ollama: {}", json.dumps(payload, default=str))
            response = await self._client.post("/api/chat", json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                logger.error(
                    "Ollama chat request failed with status {} and body: {}",
                    response.status_code,
                    response.text,
                )
                raise
            raw_response = response.json()
            return self._normalize_response(raw_response)

    async def generate_tool_response(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]],
        model: str | None = None,
    ) -> ChatResponse:
        """Convenience wrapper for prompting Ollama with tool support enabled."""

        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        ]
        return await self.chat(messages, model=model, tools=tools)

    def _normalize_response(self, payload: dict[str, Any]) -> ChatResponse:
        """Convert Ollama's raw response payload into typed models."""

        message_payload = payload.get("message", {})
        tool_calls = [self._normalize_tool_call(item) for item in message_payload.get("tool_calls", [])]
        message_payload["tool_calls"] = [tool_call.model_dump(exclude_none=True) for tool_call in tool_calls]
        payload["message"] = message_payload

        chat_response = ChatResponse.model_validate(payload)
        logger.debug(
            "Received Ollama response: model='{}', done={}, tool_calls={}",
            chat_response.model,
            chat_response.done,
            len(chat_response.message.tool_calls),
        )
        return chat_response

    def _normalize_tool_call(self, payload: dict[str, Any]) -> OllamaToolCall:
        """Normalize tool call argument shapes returned by Ollama."""

        function_payload = payload.get("function", {})
        arguments = function_payload.get("arguments", {})
        if isinstance(arguments, str):
            arguments = {"raw": arguments}

        return OllamaToolCall(
            id=payload.get("id"),
            function=OllamaFunction(
                name=function_payload.get("name", ""),
                arguments=arguments,
            ),
        )
