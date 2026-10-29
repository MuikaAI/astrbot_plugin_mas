"""Bot 侧 WebSocket 客户端。

连接独立部署的 MAS Core 进程，负责：

1. 将用户消息/命令/会话引导事件转发给 Core
2. 接收 Core 的 ``send_message`` / ``command_result`` 并分发给注册的处理器
3. 自动重连（指数退避 + 备用入口轮询 + 主入口恢复）
4. Core 不可用时暂存待发事件（上限 100 条），重连后补发

本模块不依赖任何聊天框架，可在 AstrBot 之外独立驱动与验证。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

import aiohttp
from aiohttp import WSMsgType

from .attachments import AttachmentTransfer
from .protocol import (
    TYPE_COMMAND,
    TYPE_SESSION_BOOTSTRAP,
    TYPE_USER_MESSAGE,
    OutgoingFile,
    make_envelope,
)

logger = logging.getLogger("astrbot_plugin_mas")

# 重连参数
_INITIAL_RECONNECT_DELAY = 1.0
_MAX_RECONNECT_DELAY = 30.0

# 待发送事件队列上限（Core 不可用时暂存）
_MAX_PENDING_EVENTS = 100

# 暂存附件的总字节上限，超出后丢弃附件、保留文本
_MAX_PENDING_FILE_BYTES = 100 * 1024 * 1024

# Core 侧拒绝连接的关闭码（muika/ipc/server.py）
_CLOSE_UNAUTHORIZED = 4003
_CLOSE_DUPLICATE_NAME = 4000

MessageHandler = Callable[[dict], Awaitable[None]]


class MasIpcClient:
    """与 MAS Core 的 WebSocket IPC 客户端，在整个插件生命周期中维持连接。"""

    def __init__(
        self,
        core_url: str,
        secret: str,
        client_name: str,
        fallback_urls: Optional[List[str]] = None,
        attachments_dir: Optional[Path] = None,
    ) -> None:
        self._endpoints = [core_url, *(fallback_urls or [])]
        self._endpoint_index = 0
        self._return_primary = False
        self._secret = secret
        self.client_name = client_name
        self._attachments_dir = attachments_dir or Path("chat_attachments")
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None

        # 消息处理器: type → handler
        self._handlers: Dict[str, MessageHandler] = {}

        # 待发送事件（Core 不可用时暂存），条目为 (信封, 待上传附件)
        self._pending: deque[tuple[dict, list[OutgoingFile]]] = deque(
            maxlen=_MAX_PENDING_EVENTS
        )

        self._connected = False
        self._running = False
        self._reconnect_count = 0
        self._connected_event = asyncio.Event()

        # 每次（重）连接成功后由客户端调用，用于发送会话引导等
        self.on_connected: Optional[Callable[[], Awaitable[None]]] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def endpoint(self) -> str:
        """返回当前连接或正在尝试的地址。"""
        return self._endpoints[self._endpoint_index]

    def attachment_transfer(self) -> AttachmentTransfer:
        return AttachmentTransfer(self.endpoint, self._secret, self._attachments_dir)

    def on_message(self, msg_type: str, handler: MessageHandler) -> MessageHandler:
        """注册一个 Core 下行消息类型的处理器。"""
        self._handlers[msg_type] = handler
        return handler

    async def connect(self) -> None:
        """建立到 Core 的 WebSocket 连接并维持自动重连，阻塞至 ``disconnect``。"""
        self._running = True
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

        try:
            while self._running:
                try:
                    await self._connect_once()
                except asyncio.TimeoutError:
                    self._log_connect_failure(
                        f"Connection timed out after {self._session.timeout.total}s"
                    )
                except aiohttp.ClientError as e:
                    self._log_connect_failure(f"Connection failed: {e}")
                except Exception as e:
                    logger.error(f"[MAS] Connection failed: {type(e).__name__}: {e}")

                if not self._running:
                    break

                self._reconnect_count += 1
                self._endpoint_index = (
                    0
                    if self._return_primary
                    else (self._endpoint_index + 1) % len(self._endpoints)
                )
                self._return_primary = False
                delay = min(
                    _INITIAL_RECONNECT_DELAY * (2 ** min(self._reconnect_count - 1, 5)),
                    _MAX_RECONNECT_DELAY,
                )
                self._log_connect_failure(
                    f"Reconnecting in {delay:.1f}s (attempt {self._reconnect_count})"
                )
                await asyncio.sleep(delay)
        finally:
            if self._session:
                await self._session.close()
                self._session = None

    async def disconnect(self) -> None:
        """断开连接并停止重连。"""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        logger.info("[MAS] Disconnected from Muika.")

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        """等待首次连接建立，超时返回 False（后台继续重试）。"""
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            return self._connected
        except asyncio.TimeoutError:
            return False

    async def send_user_message(
        self, message: str, files: Sequence[OutgoingFile] = ()
    ) -> bool:
        """向 Core 发送用户对话消息；附件在上行前经认证端点上传。"""
        return await self._send_or_queue(
            make_envelope(TYPE_USER_MESSAGE, message=message), list(files)
        )

    async def send_command(self, raw: str) -> bool:
        """向 Core 发送一条命令（含前缀）。"""
        return await self._send_or_queue(make_envelope(TYPE_COMMAND, raw=raw), [])

    async def send_session_bootstrap(self) -> bool:
        """通知 Core 开始（或恢复）会话。"""
        return await self._send_or_queue(make_envelope(TYPE_SESSION_BOOTSTRAP), [])

    async def _connect_once(self) -> None:
        """单次连接尝试，阻塞在接收循环直到连接断开。"""
        headers = {"X-Client-Name": self.client_name}
        if self._secret:
            headers["X-Auth-Token"] = self._secret
        assert self._session is not None
        self._ws = await self._session.ws_connect(
            self.endpoint,
            headers=headers,
            heartbeat=20,
            timeout=aiohttp.ClientWSTimeout(ws_close=5),
            receive_timeout=None,
        )
        self._connected = True
        self._reconnect_count = 0
        self._connected_event.set()
        logger.info("Connected to Muika.")
        if self.on_connected is not None:
            try:
                await self.on_connected()
            except Exception as e:
                logger.warning(f"[MAS] on_connected callback raised: {e!r}")

        messages: asyncio.Queue[str | None] = asyncio.Queue()
        consumer = asyncio.create_task(self._consume_messages(messages, self._ws))
        primary = asyncio.create_task(self._prefer_primary())

        try:
            async for msg in self._ws:
                if msg.type == WSMsgType.TEXT:
                    messages.put_nowait(msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                    break
        finally:
            primary.cancel()
            self._connected = False
            self._connected_event.clear()
            messages.put_nowait(None)
            cleanup = asyncio.gather(primary, consumer, return_exceptions=True)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            finally:
                code = self._ws.close_code if self._ws is not None else None
                self._ws = None
                if code == _CLOSE_UNAUTHORIZED:
                    logger.error(
                        "[MAS] Core rejected the connection (close 4003) — check ipc_secret"
                    )
                elif code == _CLOSE_DUPLICATE_NAME:
                    logger.error(
                        f"[MAS] Core rejected the connection (close 4000) — "
                        f"client_name {self.client_name!r} is already connected"
                    )
                elif code is not None:
                    logger.debug(f"[MAS] Connection to Core lost (close code {code})")
                else:
                    logger.debug("[MAS] Connection to Core lost")
            consumer.result()

    async def _prefer_primary(self) -> None:
        """使用备用入口时探测主入口健康状态，恢复后重连主入口。"""
        while self._running and self._endpoint_index:
            await asyncio.sleep(10)
            assert self._session is not None
            base = AttachmentTransfer(
                self._endpoints[0], self._secret, self._attachments_dir
            ).base
            try:
                async with self._session.get(
                    base + "/health", timeout=aiohttp.ClientTimeout(total=3)
                ) as response:
                    if response.status == 200 and self._ws is not None:
                        self._return_primary = True
                        await self._ws.close()
                        return
            except (aiohttp.ClientError, TimeoutError):
                continue

    async def _consume_messages(
        self, messages: asyncio.Queue[str | None], ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        """按顺序处理下行消息，断线后完成补发再释放连接。"""
        try:
            await self._flush_pending()
            while (raw := await messages.get()) is not None:
                await self._dispatch(raw)
        finally:
            await ws.close()

    async def _dispatch(self, raw: str) -> None:
        """分发收到的下行消息给注册的处理器。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"[MAS] Invalid JSON from Core: {raw[:100]!r}")
            return

        handler = self._handlers.get(data.get("type", ""))
        if handler is None:
            return
        try:
            await handler(data)
        except Exception as exc:
            logger.error(f"[MAS] Handler for type={data.get('type')!r} raised: {exc!r}")

    async def _send_or_queue(self, envelope: dict, files: list[OutgoingFile]) -> bool:
        """发送或暂存一条上行事件。"""
        if self._connected and self._ws and not self._ws.closed:
            try:
                await self._send_packet(envelope, files)
                return True
            except Exception as e:
                logger.warning(f"[MAS] Send failed: {e!r}")
                self._connected = False

        if len(self._pending) >= _MAX_PENDING_EVENTS:
            logger.warning(
                f"[MAS] Pending queue full — dropping event of type {envelope.get('type')!r}"
            )
            return False

        if files:
            queued_bytes = sum(
                len(item.content) for _, fs in self._pending for item in fs
            )
            if (
                queued_bytes + sum(len(item.content) for item in files)
                > _MAX_PENDING_FILE_BYTES
            ):
                logger.warning(
                    "[MAS] Offline queue exceeds 100 MiB of attachments — dropping files, keeping text"
                )
                files = []
        self._pending.append((envelope, files))
        return True

    async def _flush_pending(self) -> int:
        """补发所有暂存事件。"""
        sent = 0
        while self._pending:
            envelope, files = self._pending.popleft()
            try:
                if self._ws and self.is_connected:
                    await self._send_packet(envelope, files)
                    sent += 1
                else:
                    self._pending.appendleft((envelope, files))
                    break
            except Exception as e:
                logger.warning(f"[MAS] Failed to flush pending event: {e!r}")
                self._pending.appendleft((envelope, files))
                break
        if sent:
            logger.debug(f"[MAS] Flushed {sent} pending event(s)")
        return sent

    async def _send_packet(self, envelope: dict, files: list[OutgoingFile]) -> None:
        """上传附件后发送信封，保留事件自身的稳定编号。"""
        assert self._ws is not None
        if files:
            transfer = self.attachment_transfer()
            envelope = {
                **envelope,
                "resources": [await transfer.upload(item) for item in files],
            }
        await self._ws.send_json(envelope)

    def _log_connect_failure(self, detail: str) -> None:
        """连续失败时降频输出，避免 Core 未就绪期间刷屏。"""
        if self._reconnect_count < 3 or self._reconnect_count % 10 == 0:
            logger.warning(f"[MAS] {detail}")
        else:
            logger.debug(f"[MAS] {detail}")
