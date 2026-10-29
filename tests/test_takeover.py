"""接管判定与消息分流回归测试。

覆盖审查修复 F1（群聊唤醒必须用 ``is_at_or_wake_command``，
``is_wake`` 被本插件的 ALL 注册污染）与 F2（空消息交还内置
empty-mention handler）。需要 astrbot 可导入（在 AstrBot venv 中运行）。
"""

import pytest

pytest.importorskip("astrbot")

from astrbot.api.message_components import Plain  # noqa: E402

from astrbot_plugin_mas.main import MASPlugin  # noqa: E402

MASTER = "10000"
PRIVATE_UMO = "aiocqhttp:FriendMessage:10000"
GROUP_UMO = "aiocqhttp:GroupMessage:1"
OTHER_GROUP_UMO = "aiocqhttp:GroupMessage:2"

CONFIG = {"master_id": MASTER, "takeover_group_umos": [GROUP_UMO]}


class StubClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.messages: list[tuple[str, list]] = []

    async def send_command(self, raw: str) -> bool:
        self.commands.append(raw)
        return True

    async def send_user_message(self, message: str, files=()) -> bool:
        self.messages.append((message, list(files)))
        return True


class StubEvent:
    def __init__(
        self,
        sender: str,
        umo: str,
        private: bool,
        at_or_wake: bool,
        message_str: str = "",
        components: list | None = None,
    ) -> None:
        self._sender = sender
        self.unified_msg_origin = umo
        self._private = private
        self.is_at_or_wake_command = at_or_wake
        self.is_wake = True  # 本插件注册 ALL 后对所有消息恒为 True
        self.message_str = message_str
        self._components = components or []
        self.stopped = False

    def get_sender_id(self) -> str:
        return self._sender

    def is_private_chat(self) -> bool:
        return self._private

    def get_messages(self) -> list:
        return self._components

    def is_stopped(self) -> bool:
        return self.stopped

    def stop_event(self) -> None:
        self.stopped = True


def make_plugin(tmp_path) -> MASPlugin:
    plugin = MASPlugin.__new__(MASPlugin)
    plugin._config = CONFIG
    plugin._client = StubClient()
    plugin._data_dir = tmp_path
    plugin._active_umo = None
    return plugin


def test_master_private_chat_taken_over(tmp_path):
    plugin = make_plugin(tmp_path)
    event = StubEvent(
        MASTER,
        PRIVATE_UMO,
        private=True,
        at_or_wake=True,
        message_str="你好",
        components=[Plain("你好")],
    )
    import asyncio

    asyncio.run(plugin._on_message(event))
    assert event.stopped
    assert plugin._client.messages == [("你好", [])]
    assert plugin._active_umo == PRIVATE_UMO
    assert (tmp_path / "session_target").read_text(encoding="utf-8") == PRIVATE_UMO


def test_group_without_wake_not_taken_over(tmp_path):
    """F1 回归：群聊消息未 @ 机器人时不得接管（is_wake 已被污染，不可用）。"""
    plugin = make_plugin(tmp_path)
    event = StubEvent(
        MASTER,
        GROUP_UMO,
        private=False,
        at_or_wake=False,
        message_str="随便聊聊",
        components=[Plain("随便聊聊")],
    )
    import asyncio

    asyncio.run(plugin._on_message(event))
    assert not event.stopped
    assert plugin._client.messages == [] and plugin._client.commands == []


def test_group_at_with_command_forwarded(tmp_path):
    plugin = make_plugin(tmp_path)
    event = StubEvent(
        MASTER,
        GROUP_UMO,
        private=False,
        at_or_wake=True,
        message_str=".help",
        components=[Plain(".help")],
    )
    import asyncio

    asyncio.run(plugin._on_message(event))
    assert event.stopped
    assert plugin._client.commands == [".help"]


def test_group_slash_message_left_to_astrbot(tmp_path):
    plugin = make_plugin(tmp_path)
    event = StubEvent(
        MASTER,
        GROUP_UMO,
        private=False,
        at_or_wake=True,
        message_str="foo",
        components=[Plain("/foo")],
    )
    import asyncio

    asyncio.run(plugin._on_message(event))
    assert not event.stopped
    assert plugin._client.messages == [] and plugin._client.commands == []


def test_empty_message_returned_to_builtin_handler(tmp_path):
    """F2 回归：空 @/空消息不转发、不拦截。"""
    plugin = make_plugin(tmp_path)
    event = StubEvent(MASTER, GROUP_UMO, private=False, at_or_wake=True)
    import asyncio

    asyncio.run(plugin._on_message(event))
    assert not event.stopped
    assert plugin._client.messages == [] and plugin._client.commands == []


def test_non_master_ignored(tmp_path):
    plugin = make_plugin(tmp_path)
    event = StubEvent(
        "99999",
        PRIVATE_UMO,
        private=True,
        at_or_wake=True,
        message_str="你好",
        components=[Plain("你好")],
    )
    import asyncio

    asyncio.run(plugin._on_message(event))
    assert not event.stopped
    assert plugin._client.messages == []


def test_group_not_in_whitelist_ignored(tmp_path):
    plugin = make_plugin(tmp_path)
    event = StubEvent(
        MASTER,
        OTHER_GROUP_UMO,
        private=False,
        at_or_wake=True,
        message_str="你好",
        components=[Plain("你好")],
    )
    import asyncio

    asyncio.run(plugin._on_message(event))
    assert not event.stopped
    assert plugin._client.messages == []
