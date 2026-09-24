import json
import uuid
from typing import Any, List, Optional, Sequence, Union, Dict

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_core.runnables import Runnable
# NOTE: intentionally no top-level `from llama_cpp import Llama` here - this
# module is imported unconditionally by explorer_agent.llm_providers, and the
# actual Llama instance is constructed lazily there (_get_local_llm), only
# when "local" is part of the configured provider chain. `llm: Any` below avoids
# needing the import just for a type hint.


def _messages_to_openai_format(messages: List[BaseMessage]) -> List[dict]:
    """
    llama-cpp-python's create_chat_completion expects OpenAI-style role
    names (system/user/assistant) - NOT Gemma's native user/model roles.
    The library applies the GGUF's embedded chat template internally,
    so we don't need our own role-merging/alternation logic here (unlike
    the transformers-based GemmaChatModel) - llama.cpp handles that.
    """
    role_map = {"system": "system", "human": "user", "ai": "assistant", "tool": "user"}
    formatted = []
    for m in messages:
        role = role_map.get(m.type, "user")
        content = m.content if isinstance(m.content, str) else str(m.content)
        formatted.append({"role": role, "content": content})
    return formatted


def _usage_metadata(response: dict) -> Optional[dict]:
    """llama.cpp's token counts in LangChain's usage_metadata shape (see llm_usage.py)."""
    usage = response.get("usage") or {}
    if not usage:
        return None
    return {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0)}


class QwenCoderGGUFChatModel(BaseChatModel):
    """
    BaseChatModel wrapper around a GGUF Qwen2.5-Coder model via llama-cpp-python.

    Uses llama.cpp's GRAMMAR-CONSTRAINED JSON decoding (response_format
    with a schema) for tool-calling emulation - more reliable than prompt-
    based JSON coaxing, because output tokens are constrained at the
    decoding level to match the schema, not just requested via instruction.
    """

    llm: Any
    max_tokens: int = 512              # plain chat responses
    max_tokens_tool_call: int = 3072   # structured/tool-call responses need much more room

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return "qwen_coder_gguf_chat"

    def bind_tools(
        self,
        tools: Sequence[Union[Dict[str, Any], type, BaseTool]],
        *,
        tool_choice: Optional[Union[str, bool]] = None,
        **kwargs: Any,
    ) -> Runnable:
        formatted_tools = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted_tools, tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResult:
        tools = kwargs.get("tools")
        if tools:
            return self._generate_tool_call(messages, tools)

        formatted_messages = _messages_to_openai_format(messages)
        response = self.llm.create_chat_completion(messages=formatted_messages, max_tokens=self.max_tokens)
        text = response["choices"][0]["message"]["content"]
        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content=text, usage_metadata=_usage_metadata(response)))])

    def _generate_tool_call(self, messages: List[BaseMessage], tools: List[dict]) -> ChatResult:
        if len(tools) != 1:
            raise NotImplementedError(
                f"QwenCoderGGUFChatModel supports exactly ONE bound schema/tool at a time. "
                f"Got {len(tools)} tools: {[t['function']['name'] for t in tools]}"
            )

        fn = tools[0]["function"]
        tool_name = fn["name"]
        json_schema = fn.get("parameters", {})

        instruction = (
            f"You must respond with a single JSON object matching the required schema for '{tool_name}'. "
            "Respond with ONLY the JSON object - no extra text.\n\n"
            "IMPORTANT: keep any 'code' field SHORT and structural (length/null/regex/dtype checks) "
            "rather than enumerating long lists of valid values."
        )

        formatted_messages = _messages_to_openai_format(
            [SystemMessage(content=instruction)] + list(messages)
        )

        response = self.llm.create_chat_completion(
            messages=formatted_messages,
            max_tokens=self.max_tokens_tool_call,   # <-- the actual fix
            response_format={"type": "json_object", "schema": json_schema},
        )

        raw_text = response["choices"][0]["message"]["content"]
        finish_reason = response["choices"][0].get("finish_reason")

        if finish_reason == "length":
            raise ValueError(
                f"Output TRUNCATED - hit max_tokens_tool_call={self.max_tokens_tool_call} "
                f"before the model finished. Increase max_tokens_tool_call, increase n_ctx "
                f"on the Llama instance, or reduce MAX_TOTAL_CHECKS_PER_TABLE.\n"
                f"Raw (partial, {len(raw_text)} chars):\n{raw_text[:500]}"
            )

        try:
            # strict=False: the grammar lets the model emit literal newlines/tabs
            # inside strings (common in multi-line `code` fields), which strict
            # json.loads rejects as "Invalid control character".
            args = json.loads(raw_text, strict=False)
        except json.JSONDecodeError as e:
            # OutputParserException => llm_providers retries on the same model.
            raise OutputParserException(f"Failed to parse JSON: {e}\nRaw: {raw_text[:500]}") from e

        tool_call = {"name": tool_name, "args": args, "id": str(uuid.uuid4()), "type": "tool_call"}
        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content="", tool_calls=[tool_call], usage_metadata=_usage_metadata(response)))])
