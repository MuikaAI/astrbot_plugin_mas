"""Muika-After-Story — AstrBot 插件。

将 Muika 人格引擎完整嵌入 AstrBot，提供与独立运行版本一致的对话体验。
"""

from __future__ import annotations

import os

# 必须在任何 muika.* 导入之前注入环境变量。
# muika.config 模块级会实例化 MASConfig()，其 validator 强制要求
# MASTER_ID 且 ipc_secret 为空时会触发 init_logger() 循环导入。
# isort 会打乱 import 顺序，因此 env 注入不能依赖 bridge 模块的副作用，
# 必须放在 main.py 文件最顶部。
if not os.getenv("MASTER_ID"):
    os.environ["MASTER_ID"] = "placeholder"
if not os.getenv("IPC_SECRET"):
    os.environ["IPC_SECRET"] = "placeholder"

from asyncio import Queue
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import BaseMessageComponent, File, Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.db.po import Persona
from muika.core.events import (
    SessionBootstrapEvent,
    UserMessageEvent,
    UserMessagePayload,
)
from muika.core.executor import Executor
from muika.core.loop import Muika
from muika.models import Message, Resource

from .core.bridge import install_bridge
from .core.config import PluginConfig
from .core.prompt import SYSTEM_PROMPT
from .core.tools import set_event


@register(
    name="astrbot_plugin_mas",
    author="Muika",
    desc="Muika-After-Story 在 AstrBot 中的适配器",
    version="1.4.0",
    repo="https://github.com/MuikaAI/asterbot_plugin_mas",
)
class MASPlugin(Star):
    """AstrBot 插件入口。

    启动时安装 LLM 桥接，构造 Muika 引擎；运行时将所有用户消息
    转发给 Muika 事件循环处理，并阻止 AstrBot 默认 Agent 管线。
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._raw_config = config
        self._cfg = PluginConfig.from_astrbot_config(config)
        self.persona_mgr = context.persona_manager
        self._bootstrapped: bool = False

        install_bridge(
            self.context,
            plugin_name="astrbot_plugin_mas",
            get_umo=lambda: self._cfg.master_id or None,
            config=self._cfg,
        )

        self._executor = Executor(Queue(), self.send_message)
        self._muika = Muika(self._executor)

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """AstrBot 插件初始化（早于 on_astrbot_loaded）。"""
        from muika.database.db import init_db

        await init_db()
        # Muika.__init__ 中 create_task(memory.load()) 早于 init_db()，
        # 会产生一条无害的 ERRO 日志；这里补一次可靠加载。
        await self._muika.memory.load()
        self._persona = await self._init_persona()
        self._muika.start()

    @filter.on_astrbot_loaded()
    async def _on_ready(self) -> None:
        """AstrBot 完全启动后唤醒 Muika（此时 Provider / Platform 已就绪）。"""
        if self._cfg.master_id and not self._bootstrapped:
            await self._muika.create_event(SessionBootstrapEvent())
            self._bootstrapped = True

    async def terminate(self) -> None:
        """插件卸载时清理 Persona。"""
        try:
            await self.persona_mgr.delete_persona("Muika")
        except ValueError:
            pass

    # ── Persona ────────────────────────────────────────────────────────────

    async def _init_persona(self) -> Persona:
        try:
            return await self.persona_mgr.get_persona("Muika")
        except ValueError:
            return await self.persona_mgr.create_persona(
                persona_id="Muika",
                system_prompt=SYSTEM_PROMPT,
            )

    # ── 消息处理 ───────────────────────────────────────────────────────────

    async def send_message(
        self,
        text: str,
        resources: Optional[list[Resource]] = None,
        target: Optional[str] = None,
    ) -> None:
        """Executor 回调：将 Muika 回复发送给用户。"""
        if not self._cfg.master_id:
            return
        chain = self._build_chain(text, *(resources or []))
        await self.context.send_message(self._cfg.master_id, chain)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-100)
    async def _on_message(self, event: AstrMessageEvent) -> None:
        """所有用户消息的入口。

        priority=-100 确保其他插件的 command / event handler 先执行。
        若事件已被 stopped（其他插件已处理），不重复介入。
        """
        if event.is_stopped():
            return

        # 首次消息收集 master_id，以 SessionBootstrap 初次模式问候
        if not self._cfg.master_id:
            self._cfg.master_id = event.unified_msg_origin
            self._raw_config.save_config({"master_id": self._cfg.master_id})

        if not self._bootstrapped:
            await self._muika.create_event(SessionBootstrapEvent())
            self._bootstrapped = True
            event.stop_event()
            return

        # 持久化事件引用供 tool_loop_agent 使用（Muika 事件循环
        # 在 _on_message 返回后异步处理 <Butler:> 标签）
        set_event(event)

        message = _extract_message(event.message_obj.message)
        await self._muika.create_event(UserMessageEvent(UserMessagePayload(message)))
        event.stop_event()

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_chain(*items: str | Resource) -> MessageChain:
        chain = MessageChain()
        for item in items:
            if isinstance(item, str):
                chain = chain.message(item)
            elif isinstance(item, Resource) and item.type == "image":
                chain = chain.file_image(item.path)
            else:
                logger.warning(f"[MAS/Asterbot] 不支持的多模态类型: {item}")
        return chain


# ── module-level helpers ──────────────────────────────────────────────────────


def _extract_message(components: list[BaseMessageComponent]) -> Message:
    """从 AstrBot 消息组件列表提取 MAS Message（文本 + 资源）。"""
    texts: list[str] = []
    files: list[Resource] = []
    for comp in components:
        if isinstance(comp, Plain):
            texts.append(comp.text)
        elif isinstance(comp, Image) and comp.path:
            files.append(Resource(type="image", path=comp.path))
        elif isinstance(comp, File):
            files.append(Resource(type="file", path=comp.file))
        else:
            logger.warning(f"[MAS/Asterbot] 不支持的消息组件: {comp}")
    return Message(message="\n".join(texts), resources=files)
