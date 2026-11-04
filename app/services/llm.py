"""DeepSeek chat / JSON client."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from loguru import logger

from app.config import settings
from app.core.text import parse_json_loose


class LLMError(RuntimeError):
    pass


def _to_messages(messages: list[dict[str, str]]) -> list[BaseMessage]:
    result: list[BaseMessage] = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system":
            result.append(SystemMessage(content=content))
        elif role == "assistant":
            result.append(AIMessage(content=content))
        else:
            result.append(HumanMessage(content=content))
    return result


class LLMService:
    def __init__(self) -> None:
        self._chat: ChatOpenAI | None = None

    def _require_key(self) -> None:
        if not settings.deepseek_api_key:
            raise LLMError("未配置 DEEPSEEK_API_KEY，请在 .env 中填写")

    def chat_model(self) -> ChatOpenAI:
        self._require_key()
        if self._chat is None:
            self._chat = ChatOpenAI(
                model=settings.deepseek_model,
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
                timeout=settings.llm_timeout,
                max_retries=1,
            )
        return self._chat

    def chat_json(self, prompt: str, system: str | None = None, retries: int = 2) -> dict:
        self._require_key()
        model = ChatOpenAI(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            temperature=0.1,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout,
            max_retries=0,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
        messages: list[BaseMessage] = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = model.invoke(messages)
                content = response.content
                if isinstance(content, list):
                    content = "".join(str(part) for part in content)
                return parse_json_loose(str(content))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("DeepSeek JSON attempt {} failed: {}", attempt + 1, exc)
        raise LLMError(f"DeepSeek JSON 调用失败：{last_error}") from last_error

    async def astream(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式响应"""
        self._require_key()
        model = self.chat_model()
        if temperature is not None:
            model = model.bind(temperature=temperature)
        try:
            async for chunk in model.astream(_to_messages(messages)):
                content = chunk.content
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in content
                    )
                if content:
                    yield {"type": "token", "text": str(content)}
                usage = getattr(chunk, "usage_metadata", None)
                if usage:
                    yield {"type": "usage", "usage": usage}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("DeepSeek stream failed")
            raise LLMError(f"DeepSeek 调用失败：{exc}") from exc

    async def complete(self, messages: list[dict[str, str]]) -> str:
        '''非流式调用大模型'''
        self._require_key()
        model = self.chat_model()
        try:
            response = await model.ainvoke(_to_messages(messages))
            content = response.content
            if isinstance(content, list):
                content = "".join(str(part) for part in content)
            return str(content)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"DeepSeek 调用失败：{exc}") from exc

    def check(self) -> dict:
        if not settings.deepseek_api_key:
            return {"ok": False, "configured": False, "error": "未配置 DEEPSEEK_API_KEY"}
        try:
            response = httpx.get(
                f"{settings.deepseek_base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                timeout=15,
            )
            return {
                "ok": response.status_code == 200,
                "configured": True,
                "status": response.status_code,
                "error": "" if response.status_code == 200 else response.text[:200],
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "configured": True, "error": str(exc)}


llm_service = LLMService()

if __name__=="__main__":
    llm=LLMService()
    print(llm.chat_json(
        prompt="请以json格式回复：你好",
        system="你是一个助手，必须用json格式输出。示例：{\"reply\": \"你好\"}"
    ))
