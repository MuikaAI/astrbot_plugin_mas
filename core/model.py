"""MAS LLM 模型替身 —— 路由 MAS 的 ``load_model()`` 到 AstrBot Provider。"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from astrbot.api import logger
from astrbot.api.star import Context as AstrContext
from astrbot.core.agent.message import Message
from muika.config import ModelConfig
from muika.core.memory import SessionTurn
from muika.llm import ModelCompletions, ModelRequest, Usage

DUMMY_MODEL_CONFIG: ModelConfig = ModelConfig(provider="openai")
"""供 get_model_config mock 返回的占位配置，避免缺失 models.yml 时抛错。"""

_role_map: dict[str, str] = {"user": "user", "muika": "assistant", "agent": "assistant"}


class AsterbotModel:
    """不继承 ``BaseLLM``、不注册进 MAS provider registry 的鸭子类型替身。

    实现 ``async ask(request, *, stream=False) -> ModelCompletions``，
    内部路由到 ``Context.llm_generate()``（纯文本）或
    ``Context.tool_loop_agent()``（工具调用，即 Butler ``<Butler:>`` 标签）。
    """

    is_running: bool = False

    def __init__(
        self,
        context: AstrContext,
        get_umo: Callable[[], Optional[str]],
        provider_id: Optional[str] = None,
    ) -> None:
        self._context = context
        self._get_umo = get_umo
        self._provider_id = provider_id

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _convert_turn(turn: SessionTurn) -> Message:
        role = turn.role
        role = "assistant" if role == "muika" else role
        role = "user" if role == "agent" else role
        return Message(role=role, content=turn.content)

    @staticmethod
    def _message_text(msg: Message) -> str:
        """读取 Message.content 为纯文本（运行时始终为 str）。"""
        return msg.content if isinstance(msg.content, str) else ""

    @classmethod
    def _convert_history(cls, turns: Sequence[SessionTurn]) -> list[Message]:
        contexts: list[Message] = []
        for turn in turns:
            if not turn.content:
                continue
            converted = cls._convert_turn(turn)
            if contexts and contexts[-1].role == converted.role:
                prev = contexts[-1]
                merged = cls._message_text(prev) + "\n" + cls._message_text(converted)
                contexts[-1] = Message(role=prev.role, content=merged)
            else:
                contexts.append(converted)
        return contexts

    def _resolve_provider_id(self) -> Optional[str]:
        """返回有效的 provider ID：配置优先 → session 默认 → None。"""
        if self._provider_id:
            return self._provider_id
        provider = self._context.get_using_provider(umo=self._get_umo() or None)
        if provider is None:
            logger.error("[MAS/Asterbot] No chat provider available.")
            return None
        return provider.meta().id

    # ── ask (pure text) ──────────────────────────────────────────────────

    async def _ask_text(self, request: ModelRequest) -> ModelCompletions:
        provider_id = self._resolve_provider_id()
        if provider_id is None:
            return ModelCompletions(text="", succeed=False)

        image_urls: list[str] = [
            r.path for r in request.resources if r.type == "image" and r.path
        ]
        audio_urls: list[str] = [
            r.path for r in request.resources if r.type == "audio" and r.path
        ]

        try:
            resp = await self._context.llm_generate(
                chat_provider_id=provider_id,
                prompt=request.prompt,
                system_prompt=request.system,
                contexts=self._convert_history(request.history),
                image_urls=image_urls or None,
                audio_urls=audio_urls or None,
            )
        except Exception as e:
            logger.error(f"[MAS/Asterbot] llm_generate failed: {e}")
            return ModelCompletions(text="", succeed=False)

        usage = Usage()
        if resp.usage is not None:
            usage = Usage(
                input_tokens=resp.usage.input_other + resp.usage.input_cached,
                output_tokens=resp.usage.output,
                cached_tokens=resp.usage.input_cached,
            )
        return ModelCompletions(
            text=resp.completion_text or "", usage=usage, succeed=True
        )

    # ── ask (tools) ──────────────────────────────────────────────────────

    async def _ask_with_tools(self, request: ModelRequest) -> ModelCompletions:
        """路由到 AstrBot Agent Runner 处理 ``<Butler:>`` 工具调用。

        Butler 已在调用链上游通过 contextvars 设好了
        state / executor / resources 上下文。
        """
        from .tools import get_event, get_toolset

        event = get_event()
        if event is None:
            logger.error("[MAS/Asterbot] No AstrMessageEvent for tool_loop_agent")
            return ModelCompletions(text="", succeed=False)

        provider_id = self._resolve_provider_id()
        if provider_id is None:
            return ModelCompletions(text="", succeed=False)

        try:
            resp = await self._context.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                prompt=request.prompt,
                system_prompt=request.system or "",
                tools=get_toolset(),
                max_steps=5,
            )
        except Exception as e:
            logger.error(f"[MAS/Asterbot] tool_loop_agent failed: {e}")
            return ModelCompletions(text="", succeed=False)

        usage = Usage()
        if resp.usage is not None:
            usage = Usage(
                input_tokens=resp.usage.input_other + resp.usage.input_cached,
                output_tokens=resp.usage.output,
                cached_tokens=resp.usage.input_cached,
            )
        return ModelCompletions(
            text=resp.completion_text or "", usage=usage, succeed=True
        )

    # ── public ───────────────────────────────────────────────────────────

    async def ask(
        self, request: ModelRequest, *, stream: bool = False
    ) -> ModelCompletions:
        if request.tools:
            return await self._ask_with_tools(request)
        return await self._ask_text(request)


class FakeConfigManager:
    """替代 ModelConfigManager —— 所有方法为 no-op，只返回占位配置。"""

    def register_listener(self, listener: Callable) -> None:
        pass

    def unregister_listener(self, listener: Callable) -> None:
        pass

    def get_model_config(self, name: Optional[str] = None) -> ModelConfig:
        return DUMMY_MODEL_CONFIG

    def change_current_config(self, config: ModelConfig) -> None:
        pass

    def get_name_from_config(self, config: Optional[ModelConfig] = None) -> str:
        return "asterbot"
