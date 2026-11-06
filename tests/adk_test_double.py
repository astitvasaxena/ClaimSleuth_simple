"""TEST DOUBLE for offline ADK tests — not an LLM.

`ScriptedLlm` is a `google.adk` BaseLlm that replays a fixed list of model turns (function calls or
text). Everything around it is real: ADK Runner, sessions, callbacks, set_model_response validation,
McpToolset over Streamable HTTP and the MCP server process. Results produced with it are labelled
model="test-double" and are never presented as LLM output.
"""
from __future__ import annotations

from typing import Any, AsyncGenerator

from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


def call(name: str, **args: Any) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def text(value: str) -> types.Part:
    return types.Part(text=value)


class ScriptedLlm(BaseLlm):
    model: str = "scripted-test-double"
    script: list[Any] = []
    requests: list[Any] = []

    @property
    def capabilities(self) -> LlmCapabilities:
        # Same path as the Gemini API (non-Vertex): output_schema + tools -> set_model_response tool.
        return LlmCapabilities(output_schema_and_tools=False)

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request)
        if not self.script:
            raise RuntimeError("ScriptedLlm script exhausted")
        part = self.script.pop(0)
        if isinstance(part, BaseException):      # a scripted failure of this model request
            raise part
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=100, candidates_token_count=20, total_token_count=120))

    def system_instructions(self) -> list[str]:
        return [str(r.config.system_instruction) for r in self.requests]
