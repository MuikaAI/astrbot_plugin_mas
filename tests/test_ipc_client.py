"""IPC 客户端与协议回归测试。

对进程内 mock Core（镜像 ``muika/ipc/server.py`` 的认证与重名拒绝语义）
验证：连接、会话引导钩子、命令往返、附件上传下载、断线暂存补发、
重名与认证拒绝、离线附件字节上限。
"""

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import pytest
from aiohttp import WSMsgType, web

from astrbot_plugin_mas.ipc import MasIpcClient
from astrbot_plugin_mas.ipc.client import _MAX_PENDING_FILE_BYTES
from astrbot_plugin_mas.ipc.protocol import OutgoingFile

SECRET = "test-secret"
PNG = b"\x89PNG-fake-image-bytes"

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class MockCore:
    """最小 Core：X-Auth-Token 认证、重名 4000、未认证 4003、命令回执。"""

    def __init__(self) -> None:
        self.received: asyncio.Queue[dict] = asyncio.Queue()
        self.names: set[str] = set()
        self.attachments: dict[str, bytes] = {}
        self.runner: web.AppRunner | None = None
        self.port = 0

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_put("/attachments/{key}", self.handle_attachment)
        app.router.add_get("/attachments/{key}", self.handle_attachment)
        return app

    async def start(self) -> None:
        self.runner = web.AppRunner(self.make_app())
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if SECRET and request.headers.get("X-Auth-Token", "") != SECRET:
            await ws.close(code=4003, message=b"Unauthorized")
            return ws
        name = request.headers.get("X-Client-Name", "").strip() or "unknown"
        if name in self.names:
            await ws.close(code=4000, message=f"dup {name}".encode())
            return ws
        self.names.add(name)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                await self.received.put(data)
                if data.get("type") == "command":
                    await ws.send_json(
                        {
                            "id": "r1",
                            "type": "command_result",
                            "ts": "t",
                            "content": "ok",
                            "resources": [],
                        }
                    )
        finally:
            self.names.discard(name)
        return ws

    async def handle_attachment(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Auth-Token") != SECRET:
            raise web.HTTPUnauthorized()
        key = request.match_info["key"]
        if request.method == "PUT":
            content = await request.read()
            if hashlib.sha256(content).hexdigest() != key:
                raise web.HTTPBadRequest(text="hash mismatch")
            self.attachments[key] = content
            return web.Response(status=201)
        if key not in self.attachments:
            raise web.HTTPNotFound()
        return web.Response(body=self.attachments[key])


async def wait_until(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached in time")


async def start_client(core: MockCore, name: str, tmp_path: Path) -> tuple:
    client = MasIpcClient(
        f"ws://127.0.0.1:{core.port}/ws", SECRET, name, attachments_dir=tmp_path / name
    )
    task = asyncio.create_task(client.connect())
    assert await client.wait_connected(5)
    return client, task


async def stop_client(client: MasIpcClient, task) -> None:
    await client.disconnect()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def run(scenario) -> None:
    asyncio.run(scenario())


# ── 用例 ──────────────────────────────────────────────────────────────────


def test_command_roundtrip_and_bootstrap_hook(tmp_path: Path):
    """命令往返；每次连接后 on_connected 钩子先于用户消息发出 bootstrap。"""

    async def scenario():
        core = MockCore()
        await core.start()
        client = MasIpcClient(
            f"ws://127.0.0.1:{core.port}/ws",
            SECRET,
            "t-roundtrip",
            attachments_dir=tmp_path / "att",
        )
        client.on_connected = client.send_session_bootstrap
        got = asyncio.Event()
        box: dict = {}

        async def on_result(data: dict) -> None:
            box.update(data)
            got.set()

        client.on_message("command_result", on_result)
        task = asyncio.create_task(client.connect())
        assert await client.wait_connected(5)

        assert await client.send_command(".help")
        await asyncio.wait_for(got.wait(), 5)
        assert box["content"] == "ok"

        first = await core.received.get()
        second = await core.received.get()
        assert first["type"] == "session_bootstrap"
        assert second["type"] == "command" and second["raw"] == ".help"

        await stop_client(client, task)
        await core.stop()

    run(scenario)


def test_attachment_upload_download_roundtrip(tmp_path: Path):
    async def scenario():
        core = MockCore()
        await core.start()
        client, task = await start_client(core, "t-att", tmp_path)

        transfer = client.attachment_transfer()
        resource = await transfer.upload(
            OutgoingFile(type="image", content=PNG, mimetype="image/png")
        )
        assert resource["url"].startswith("/attachments/")
        assert resource["path"] == ""

        path = await transfer.download(resource)
        assert Path(path).read_bytes() == PNG

        await stop_client(client, task)
        await core.stop()

    run(scenario)


def test_offline_queue_flush(tmp_path: Path):
    """主入口不可达时事件暂存，连上备用入口后按序补发。"""

    async def scenario():
        core = MockCore()
        await core.start()
        dead = MockCore()  # 占住一个无人监听的端口号
        await dead.start()
        dead_port = dead.port
        await dead.stop()

        client = MasIpcClient(
            f"ws://127.0.0.1:{dead_port}/ws",
            SECRET,
            "t-queue",
            fallback_urls=[f"ws://127.0.0.1:{core.port}/ws"],
            attachments_dir=tmp_path / "att",
        )
        task = asyncio.create_task(client.connect())
        # 主入口连接失败 → 轮询到备用入口；此处直接入队，等待补发
        assert await client.send_user_message("hello")
        assert await client.send_command(".status")

        await wait_until(lambda: core.received.qsize() >= 2)
        first = await core.received.get()
        second = await core.received.get()
        assert first["type"] == "user_message" and first["message"] == "hello"
        assert second["type"] == "command" and second["raw"] == ".status"

        await stop_client(client, task)
        await core.stop()

    run(scenario)


def test_duplicate_name_rejected(tmp_path: Path, caplog):
    """同名客户端被 Core 以 4000 拒绝，日志给出明确原因。"""

    async def scenario():
        core = MockCore()
        await core.start()
        first, first_task = await start_client(core, "t-dup", tmp_path)
        second = MasIpcClient(
            f"ws://127.0.0.1:{core.port}/ws",
            SECRET,
            "t-dup",
            attachments_dir=tmp_path / "d",
        )
        second_task = asyncio.create_task(second.connect())

        with caplog.at_level(logging.ERROR, logger="astrbot_plugin_mas"):
            await wait_until(
                lambda: any("close 4000" in r.message for r in caplog.records)
            )
        assert first.is_connected
        assert "t-dup" in core.names

        await stop_client(second, second_task)
        await stop_client(first, first_task)
        await core.stop()

    run(scenario)


def test_bad_secret_rejected(tmp_path: Path, caplog):
    """密钥错误被 Core 以 4003 拒绝，日志指向 ipc_secret。"""

    async def scenario():
        core = MockCore()
        await core.start()
        client = MasIpcClient(
            f"ws://127.0.0.1:{core.port}/ws",
            "wrong-secret",
            "t-auth",
            attachments_dir=tmp_path / "att",
        )
        task = asyncio.create_task(client.connect())

        with caplog.at_level(logging.ERROR, logger="astrbot_plugin_mas"):
            await wait_until(
                lambda: any("close 4003" in r.message for r in caplog.records)
            )
        assert not client.is_connected

        await stop_client(client, task)
        await core.stop()

    run(scenario)


def test_pending_file_bytes_cap(tmp_path: Path, monkeypatch):
    """离线附件总量超上限后丢弃附件、保留文本。"""
    monkeypatch.setattr("astrbot_plugin_mas.ipc.client._MAX_PENDING_FILE_BYTES", 1024)
    import astrbot_plugin_mas.ipc.client as client_mod

    async def scenario():
        core = MockCore()
        await core.start()
        dead = MockCore()
        await dead.start()
        dead_port = dead.port
        await dead.stop()

        client = MasIpcClient(
            f"ws://127.0.0.1:{dead_port}/ws",
            SECRET,
            "t-cap",
            fallback_urls=[f"ws://127.0.0.1:{core.port}/ws"],
            attachments_dir=tmp_path / "att",
        )
        assert client_mod._MAX_PENDING_FILE_BYTES == 1024
        task = asyncio.create_task(client.connect())
        big = OutgoingFile(type="image", content=b"x" * 800, mimetype="image/png")
        assert await client.send_user_message("one", [big])
        assert await client.send_user_message("two", [big])  # 800+800 > 1024 → 附件丢弃

        await wait_until(lambda: core.received.qsize() >= 2)
        first = await core.received.get()
        second = await core.received.get()
        assert len(first.get("resources", [])) == 1
        assert second.get("resources", []) == [] and second["message"] == "two"

        await stop_client(client, task)
        await core.stop()

    run(scenario)


def test_default_cap_is_100mib():
    assert _MAX_PENDING_FILE_BYTES == 100 * 1024 * 1024
