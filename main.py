"""Muika-After-Story — AstrBot IPC 适配器。

将接管会话内的消息转发给独立部署的 MAS Core 进程（WebSocket IPC）。
插件本身只做传输接入，不包含任何人格、记忆或 LLM 实现：

- 只有配置的 ``master_id`` 的私聊（及显式列出的群聊 umo）会被接管；
- 接管会话内 ``.`` 开头的消息作为 MAS 命令转发，其余作为对话消息；
- ``/`` 开头的消息留给 AstrBot 自己的命令系统；
- 非接管会话的消息原样放行，AstrBot 的 Provider、插件、审核不受影响。

Core 的回复与主动消息总是发往最近一次触发消息的会话（重启后恢复上次
会话）；建议只将一个会话划给 MAS。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from sys import maxsize
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .event_adapter import build_chain, extract_content, resolve_resources
from .ipc import MasIpcClient
from .ipc.protocol import (
    TYPE_ACTION_RESPONSE,
    TYPE_COMMAND_RESULT,
    TYPE_ERROR,
    TYPE_SEND_MESSAGE,
)

_SESSION_TARGET_FILE = "session_target"


class MASPlugin(Star):
    """AstrBot 插件入口。

    启动时建立到 Core 的 IPC 连接；运行时按接管规则转发消息，
    并把 Core 的回复发送到最近一次触发消息的会话。
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._config = config
        self._client: Optional[MasIpcClient] = None
        self._connect_task: Optional[asyncio.Task] = None
        self._data_dir: Optional[Path] = None
        self._active_umo: Optional[str] = None

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """建立 IPC 连接（早于 on_astrbot_loaded；连接在后台进行）。"""
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_mas")
        self._active_umo = self._load_active_umo()
        self._client = MasIpcClient(
            core_url=str(self._config.get("core_ws_url") or "ws://127.0.0.1:8765/ws"),
            secret=str(self._config.get("ipc_secret") or ""),
            client_name=str(self._config.get("client_name") or "astrbot"),
            fallback_urls=[
                str(u) for u in (self._config.get("core_fallback_urls") or [])
            ],
            attachments_dir=self._data_dir / "chat_attachments",
        )
        self._client.on_connected = self._send_bootstrap
        self._register_downlink_handlers()
        if not self._config.get("master_id"):
            logger.warning("[MAS] master_id is empty — no session will be taken over")
        if not self._config.get("ipc_secret"):
            logger.warning(
                "[MAS] ipc_secret is empty — Core will reject the connection (4003) if it sets a token"
            )
        logger.info(
            f"[MAS] Connecting to Core at {self._client.endpoint} in background"
        )
        self._connect_task = asyncio.create_task(self._client.connect())

    async def terminate(self) -> None:
        """插件卸载时断开 IPC 连接并回收后台任务。"""
        if self._client is not None:
            await self._client.disconnect()
        if self._connect_task is not None:
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[MAS] Connect task ended with error: {e!r}")
            self._connect_task = None

    # ── 消息处理 ──────────────────────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def _on_message(self, event: AstrMessageEvent) -> None:
        """接管会话的消息入口。

        priority 与官方 builtin star 相同（先注册者先执行），确保在其他
        插件与默认 LLM 之前接管；非接管会话不拦截、不暂停事件。
        """
        if not self._takes_over(event):
            return

        raw_text, files = await extract_content(event.get_messages())
        if not raw_text and not files:
            # 空 @/空消息交还内置 empty-mention handler（priority maxsize-1）
            return
        if raw_text.startswith("/"):
            # AstrBot 自己的命令空间（唤醒前缀已被 waking check 剥离，
            # 需从原始组件判断），不拦截、不转发
            return

        event.stop_event()
        assert self._client is not None
        if event.unified_msg_origin != self._active_umo:
            self._active_umo = event.unified_msg_origin
            self._save_active_umo(self._active_umo)
            logger.info(f"[MAS] Active session: {self._active_umo}")

        if raw_text.startswith("."):
            await self._client.send_command(raw_text)
            return

        # message_str 已被 waking check 剥离唤醒前缀；无文本时保留附件上行
        text = event.message_str or raw_text
        await self._client.send_user_message(text, files)

    def _takes_over(self, event: AstrMessageEvent) -> bool:
        """接管判定：主人私聊始终接管，群聊仅在白名单 umo 且被 @/唤醒词唤醒时接管。

        唤醒信号必须是 ``is_at_or_wake_command``——本插件注册了 ALL 监听，
        ``is_wake`` 对所有消息恒为 True，不能作为判定依据。
        """
        master_id = str(self._config.get("master_id") or "")
        if not master_id:
            return False
        try:
            sender = event.get_sender_id()
        except Exception:
            return False
        if sender != master_id:
            return False
        if event.is_private_chat():
            return True
        umos = [str(u) for u in (self._config.get("takeover_group_umos") or [])]
        return (
            bool(umos)
            and event.unified_msg_origin in umos
            and event.is_at_or_wake_command
        )

    # ── 会话路由锚点 ──────────────────────────────────────────────────────

    def _load_active_umo(self) -> Optional[str]:
        """恢复上次的触发会话，使 Core 主动消息在重启后仍有路由目标。"""
        assert self._data_dir is not None
        try:
            umo = (
                (self._data_dir / _SESSION_TARGET_FILE)
                .read_text(encoding="utf-8")
                .strip()
            )
            return umo or None
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning(f"[MAS] Failed to load active session: {e!r}")
            return None

    def _save_active_umo(self, umo: str) -> None:
        """持久化触发会话。"""
        assert self._data_dir is not None
        try:
            (self._data_dir / _SESSION_TARGET_FILE).write_text(umo, encoding="utf-8")
        except OSError as e:
            logger.warning(f"[MAS] Failed to persist active session: {e!r}")

    async def _send_bootstrap(self) -> None:
        """每次连接（含重连）后向 Core 发送会话引导；Core 对重复引导幂等。"""
        assert self._client is not None
        await self._client.send_session_bootstrap()

    # ── Core 下行 ─────────────────────────────────────────────────────────

    def _register_downlink_handlers(self) -> None:
        assert self._client is not None
        client = self._client
        client.on_message(TYPE_SEND_MESSAGE, self._handle_reply)
        client.on_message(TYPE_COMMAND_RESULT, self._handle_reply)
        client.on_message(TYPE_ERROR, self._handle_core_error)
        client.on_message(TYPE_ACTION_RESPONSE, self._handle_action_response)

    async def _handle_reply(self, data: dict) -> None:
        """把 Core 的回复发送到最近一次触发消息的会话。"""
        umo = self._active_umo
        if not umo:
            logger.warning(
                "[MAS] Reply arrived before any taken-over message and no session was restored — dropped"
            )
            return
        assert self._client is not None
        resolved = await resolve_resources(
            data.get("resources") or [], self._client.attachment_transfer()
        )
        chain = build_chain(str(data.get("content") or ""), resolved)
        sent = await self.context.send_message(umo, chain)
        if not sent:
            logger.warning(f"[MAS] Failed to deliver reply to {umo!r}")

    async def _handle_core_error(self, data: dict) -> None:
        """记录 Core 报告的错误。"""
        logger.error(
            f"[MAS] Core error: {data.get('message', 'Unknown')}; {data.get('detail') or ''}"
        )

    async def _handle_action_response(self, data: dict) -> None:
        """以 DEBUG 级别记录 Core 的确认响应。"""
        logger.debug(f"[MAS] Core action: {data.get('action')} {data.get('status')}")
