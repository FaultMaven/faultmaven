"""LLM Client Wrapper for Agent Orchestration (TASK-015)

This module provides a unified interface for interacting with LLM providers
(Anthropic Claude and OpenAI GPT) with support for streaming responses
and tool/function calling.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from faultmaven.exceptions import LLMException, ServiceError
from faultmaven.modules.agent.domain.events.execution_events import (
    AgentContext,
    LLMEvent,
    LLMEventType,
    Message,
    MessageRole,
    Tool,
    ToolCall,
)

logger = logging.getLogger(__name__)


class LLMProvider(str, Enum):
    """Supported LLM providers."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients."""

    @abstractmethod
    async def stream_completion(
        self,
        messages: List[Message],
        system: Optional[str] = None,
        tools: Optional[List[Tool]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncGenerator[LLMEvent, None]:
        """Stream completion from the LLM.

        Args:
            messages: Conversation history
            system: System prompt
            tools: Available tools for function calling
            max_tokens: Maximum tokens in response
            temperature: Response randomness (0.0-2.0)

        Yields:
            LLMEvent with response content
        """
        pass

    @abstractmethod
    async def count_tokens(self, messages: List[Message]) -> int:
        """Estimate token count for messages.

        Args:
            messages: Messages to count tokens for

        Returns:
            Estimated token count
        """
        pass

    @abstractmethod
    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the model.

        Returns:
            Dictionary with model name, provider, etc.
        """
        pass


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic Claude API with streaming support."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        timeout: int = 120,
    ):
        """Initialize Anthropic client.

        Args:
            api_key: Anthropic API key
            model: Model to use
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client = None

    def _get_client(self) -> Any:
        """Lazy initialization of the Anthropic client."""
        if self._client is None:
            try:
                import anthropic

                self._client = anthropic.AsyncAnthropic(
                    api_key=self.api_key,
                    timeout=self.timeout,
                )
            except ImportError:
                raise LLMException("anthropic package not installed")
        return self._client

    async def stream_completion(
        self,
        messages: List[Message],
        system: Optional[str] = None,
        tools: Optional[List[Tool]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncGenerator[LLMEvent, None]:
        """Stream completion from Claude.

        Yields:
            LLMEvent with text chunks, tool calls, or completion
        """
        client = self._get_client()

        # Convert messages to Anthropic format
        anthropic_messages = self._convert_messages(messages)

        # Convert tools to Anthropic format
        anthropic_tools = None
        if tools:
            anthropic_tools = [self._convert_tool(t) for t in tools]

        try:
            # Create streaming request
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": anthropic_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }

            if system:
                kwargs["system"] = system

            if anthropic_tools:
                kwargs["tools"] = anthropic_tools

            async with client.messages.stream(**kwargs) as stream:
                current_tool_call: Optional[Dict[str, Any]] = None
                accumulated_text = ""
                index = 0

                async for event in stream:
                    event_type = event.type

                    if event_type == "content_block_start":
                        content_block = event.content_block
                        if content_block.type == "tool_use":
                            current_tool_call = {
                                "id": content_block.id,
                                "name": content_block.name,
                                "input": "",
                            }
                        elif content_block.type == "text":
                            accumulated_text = ""

                    elif event_type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            accumulated_text += delta.text
                            yield LLMEvent(
                                event_type=LLMEventType.TEXT_CHUNK,
                                content=delta.text,
                                index=index,
                            )
                            index += 1
                        elif hasattr(delta, "partial_json"):
                            if current_tool_call:
                                current_tool_call["input"] += delta.partial_json

                    elif event_type == "content_block_stop":
                        if current_tool_call:
                            # Parse the accumulated JSON input
                            import json

                            try:
                                input_dict = json.loads(current_tool_call["input"])
                            except json.JSONDecodeError:
                                input_dict = {}

                            tool_call = ToolCall(
                                id=current_tool_call["id"],
                                name=current_tool_call["name"],
                                arguments=input_dict,
                            )
                            yield LLMEvent(
                                event_type=LLMEventType.TOOL_USE,
                                content=tool_call,
                                index=index,
                            )
                            index += 1
                            current_tool_call = None

                    elif event_type == "message_stop":
                        # Get final message for usage stats
                        final_message = await stream.get_final_message()
                        yield LLMEvent(
                            event_type=LLMEventType.COMPLETION,
                            content=accumulated_text,
                            metadata={
                                "input_tokens": final_message.usage.input_tokens,
                                "output_tokens": final_message.usage.output_tokens,
                                "stop_reason": final_message.stop_reason,
                            },
                            index=index,
                        )

        except Exception as e:
            logger.exception(f"Anthropic API error: {e}")
            yield LLMEvent(
                event_type=LLMEventType.ERROR,
                content=str(e),
                metadata={"error_type": type(e).__name__},
            )

    def _convert_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """Convert messages to Anthropic format."""
        result = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                # System messages are handled separately
                continue

            anthropic_msg: Dict[str, Any] = {"role": msg.role.value}

            if msg.role == MessageRole.TOOL:
                # Convert tool result to Anthropic format
                anthropic_msg["role"] = "user"
                anthropic_msg["content"] = [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                ]
            elif msg.tool_calls:
                # Assistant message with tool calls
                content_blocks = []
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                anthropic_msg["content"] = content_blocks
            else:
                anthropic_msg["content"] = msg.content

            result.append(anthropic_msg)

        return result

    def _convert_tool(self, tool: Tool) -> Dict[str, Any]:
        """Convert Tool to Anthropic format."""
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }

    async def count_tokens(self, messages: List[Message]) -> int:
        """Estimate token count using simple heuristic.

        For accurate counting, we'd need to use the Anthropic tokenizer,
        but a rough estimate is often sufficient.
        """
        total_chars = 0
        for msg in messages:
            total_chars += len(msg.content)
        # Rough estimate: ~4 characters per token for English
        return total_chars // 4

    def get_model_info(self) -> Dict[str, Any]:
        """Get model information."""
        return {
            "provider": LLMProvider.ANTHROPIC.value,
            "model": self.model,
            "timeout": self.timeout,
        }


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI GPT API with streaming support."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        timeout: int = 120,
    ):
        """Initialize OpenAI client.

        Args:
            api_key: OpenAI API key
            model: Model to use
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client = None

    def _get_client(self) -> Any:
        """Lazy initialization of the OpenAI client."""
        if self._client is None:
            try:
                import openai

                self._client = openai.AsyncOpenAI(
                    api_key=self.api_key,
                    timeout=self.timeout,
                )
            except ImportError:
                raise LLMException("openai package not installed")
        return self._client

    async def stream_completion(
        self,
        messages: List[Message],
        system: Optional[str] = None,
        tools: Optional[List[Tool]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncGenerator[LLMEvent, None]:
        """Stream completion from GPT.

        Yields:
            LLMEvent with text chunks, tool calls, or completion
        """
        client = self._get_client()

        # Convert messages to OpenAI format
        openai_messages = self._convert_messages(messages, system)

        # Convert tools to OpenAI format
        openai_tools = None
        if tools:
            openai_tools = [self._convert_tool(t) for t in tools]

        try:
            # Create streaming request
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": openai_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": True,
                "stream_options": {"include_usage": True},
            }

            if openai_tools:
                kwargs["tools"] = openai_tools

            stream = await client.chat.completions.create(**kwargs)

            accumulated_text = ""
            tool_calls: Dict[int, Dict[str, Any]] = {}
            index = 0

            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None

                if delta:
                    # Handle text content
                    if delta.content:
                        accumulated_text += delta.content
                        yield LLMEvent(
                            event_type=LLMEventType.TEXT_CHUNK,
                            content=delta.content,
                            index=index,
                        )
                        index += 1

                    # Handle tool calls
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            tc_index = tc.index
                            if tc_index not in tool_calls:
                                tool_calls[tc_index] = {
                                    "id": tc.id or "",
                                    "name": "",
                                    "arguments": "",
                                }
                            if tc.id:
                                tool_calls[tc_index]["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    tool_calls[tc_index]["name"] = tc.function.name
                                if tc.function.arguments:
                                    tool_calls[tc_index][
                                        "arguments"
                                    ] += tc.function.arguments

                # Check for usage stats (final chunk)
                if chunk.usage:
                    # Emit tool calls if any
                    import json

                    for tc_data in tool_calls.values():
                        try:
                            args = json.loads(tc_data["arguments"])
                        except json.JSONDecodeError:
                            args = {}

                        tool_call = ToolCall(
                            id=tc_data["id"],
                            name=tc_data["name"],
                            arguments=args,
                        )
                        yield LLMEvent(
                            event_type=LLMEventType.TOOL_USE,
                            content=tool_call,
                            index=index,
                        )
                        index += 1

                    # Emit completion event
                    yield LLMEvent(
                        event_type=LLMEventType.COMPLETION,
                        content=accumulated_text,
                        metadata={
                            "input_tokens": chunk.usage.prompt_tokens,
                            "output_tokens": chunk.usage.completion_tokens,
                            "stop_reason": (
                                chunk.choices[0].finish_reason
                                if chunk.choices
                                else None
                            ),
                        },
                        index=index,
                    )

        except Exception as e:
            logger.exception(f"OpenAI API error: {e}")
            yield LLMEvent(
                event_type=LLMEventType.ERROR,
                content=str(e),
                metadata={"error_type": type(e).__name__},
            )

    def _convert_messages(
        self,
        messages: List[Message],
        system: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Convert messages to OpenAI format."""
        result = []

        # Add system message first if provided
        if system:
            result.append({"role": "system", "content": system})

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                result.append({"role": "system", "content": msg.content})
            elif msg.role == MessageRole.TOOL:
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )
            elif msg.tool_calls:
                openai_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": (
                                    __import__("json").dumps(tc.arguments)
                                    if isinstance(tc.arguments, dict)
                                    else tc.arguments
                                ),
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
                result.append(openai_msg)
            else:
                result.append({"role": msg.role.value, "content": msg.content})

        return result

    def _convert_tool(self, tool: Tool) -> Dict[str, Any]:
        """Convert Tool to OpenAI format."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    async def count_tokens(self, messages: List[Message]) -> int:
        """Estimate token count using tiktoken if available."""
        try:
            import tiktoken

            try:
                encoding = tiktoken.encoding_for_model(self.model)
            except KeyError:
                # Models newer than the installed tiktoken's static name map
                # (e.g. gpt-5.x) aren't resolvable by name and raise KeyError.
                # Fall back to the current OpenAI base encoding — o200k_base is
                # what gpt-4o and the gpt-5 family use — so token counting stays
                # robust for ANY configured OPENAI_MODEL, not just the default.
                encoding = tiktoken.get_encoding("o200k_base")
            total = 0
            for msg in messages:
                total += len(encoding.encode(msg.content))
            return total
        except ImportError:
            # Fallback to simple heuristic
            total_chars = sum(len(msg.content) for msg in messages)
            return total_chars // 4

    def get_model_info(self) -> Dict[str, Any]:
        """Get model information."""
        return {
            "provider": LLMProvider.OPENAI.value,
            "model": self.model,
            "timeout": self.timeout,
        }


class LLMClient:
    """Unified LLM client supporting multiple providers.

    This class provides a consistent interface for interacting with
    different LLM providers (Anthropic, OpenAI) with automatic
    provider selection and failover support.
    """

    def __init__(
        self,
        provider: Union[str, LLMProvider] = LLMProvider.ANTHROPIC,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 120,
    ):
        """Initialize LLM client.

        Args:
            provider: LLM provider to use
            model: Model name (uses default if not specified)
            api_key: API key (uses environment if not specified)
            timeout: Request timeout in seconds
        """
        if isinstance(provider, str):
            provider = LLMProvider(provider)

        self.provider = provider
        self.timeout = timeout

        # Initialize appropriate client
        if provider == LLMProvider.ANTHROPIC:
            self.model = model or "claude-sonnet-4-20250514"
            api_key = api_key or self._get_api_key("ANTHROPIC_API_KEY")
            self._client = AnthropicClient(api_key, self.model, timeout)
        elif provider == LLMProvider.OPENAI:
            self.model = model or "gpt-4o"
            api_key = api_key or self._get_api_key("OPENAI_API_KEY")
            self._client = OpenAIClient(api_key, self.model, timeout)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def _get_api_key(self, env_var: str) -> str:
        """Get API key from environment."""
        import os

        key = os.environ.get(env_var)
        if not key:
            raise LLMException(f"{env_var} environment variable not set")
        return key

    async def stream_completion(
        self,
        messages: List[Message],
        system: Optional[str] = None,
        tools: Optional[List[Tool]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncGenerator[LLMEvent, None]:
        """Stream completion from the LLM.

        Args:
            messages: Conversation history
            system: System prompt
            tools: Available tools for function calling
            max_tokens: Maximum tokens in response
            temperature: Response randomness

        Yields:
            LLMEvent with response content
        """
        async for event in self._client.stream_completion(
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            yield event

    async def complete(
        self,
        messages: List[Message],
        system: Optional[str] = None,
        tools: Optional[List[Tool]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> Dict[str, Any]:
        """Get complete response (non-streaming).

        Args:
            messages: Conversation history
            system: System prompt
            tools: Available tools
            max_tokens: Maximum tokens
            temperature: Response randomness

        Returns:
            Dictionary with response content, tool_calls, and usage
        """
        content = ""
        tool_calls: List[ToolCall] = []
        usage: Dict[str, Any] = {}

        async for event in self.stream_completion(
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            if event.event_type == LLMEventType.TEXT_CHUNK:
                content += event.content
            elif event.event_type == LLMEventType.TOOL_USE:
                tool_calls.append(event.content)
            elif event.event_type == LLMEventType.COMPLETION:
                usage = event.metadata or {}
            elif event.event_type == LLMEventType.ERROR:
                raise LLMException(f"LLM error: {event.content}")

        return {
            "content": content,
            "tool_calls": tool_calls,
            "usage": usage,
        }

    async def count_tokens(self, messages: List[Message]) -> int:
        """Estimate token count for messages."""
        return await self._client.count_tokens(messages)

    def get_model_info(self) -> Dict[str, Any]:
        """Get model information."""
        return self._client.get_model_info()


def create_llm_client(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 120,
) -> LLMClient:
    """Factory function to create an LLM client.

    Args:
        provider: Provider name ("anthropic" or "openai")
        model: Model name
        api_key: API key
        timeout: Request timeout

    Returns:
        Configured LLMClient instance
    """
    import os

    # Default to Anthropic if not specified
    if provider is None:
        provider = os.environ.get("LLM_PROVIDER", "anthropic")

    return LLMClient(
        provider=provider,
        model=model,
        api_key=api_key,
        timeout=timeout,
    )
