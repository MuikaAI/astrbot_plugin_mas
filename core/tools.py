"""MAS 工具 → Asterbot FunctionTool 桥接。

将 MAS 通过 ``@on_function_call`` 注册的工具包装为 Asterbot
``FunctionTool`` 实例，供 ``context.tool_loop_agent()`` 调用。
工具执行时复用 Butler 已设置的 contextvars 上下文。
"""

from __future__ import annotations

from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult, ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

# ── 模块级引用 ────────────────────────────────────────────────────────────────

_current_event: Optional["AstrMessageEvent"] = None
"""当前 AstrMessageEvent。由 main.py 在 on_all_message 入口设置，
供 tool_loop_agent 使用。Muika 事件循环在 on_all_message 返回后
异步处理 <Butler:> 标签，因此事件引用需跨 await 持久化。"""

_cached_toolset: Optional[ToolSet] = None
"""MAS 工具转换后的缓存，首次调用 get_toolset() 时构建。"""


def set_event(event: Optional["AstrMessageEvent"]) -> None:
    global _current_event
    _current_event = event


def get_event() -> Optional["AstrMessageEvent"]:
    return _current_event


def get_toolset() -> ToolSet:
    """将 MAS ``Caller`` 全部包装为 Asterbot ``FunctionTool``（仅首次调用时构建）。"""
    global _cached_toolset
    if _cached_toolset is not None:
        return _cached_toolset

    from muika.plugin.func_call import get_function_calls

    tools = ToolSet()
    for _name, caller in get_function_calls().items():
        data = caller.data()
        func = data["function"]
        tool = _WrappedTool(
            name=func["name"],
            description=func["description"],
            parameters=func.get("parameters", {}),
            _caller=caller,
        )
        tools.add_tool(tool)

    _cached_toolset = tools
    logger.info(
        f"[MAS/Asterbot] Converted {len(tools)} MAS tools → Asterbot FunctionTools"
    )
    return _cached_toolset


# ── 工具包装 ──────────────────────────────────────────────────────────────────


@dataclass
class _WrappedTool(FunctionTool[AstrAgentContext]):
    """将单个 MAS ``Caller`` 包装为 Asterbot ``FunctionTool``。

    ``call()`` 委托给 ``Caller.run(**kwargs)``。
    Butler 已在调用链上游通过 contextvars 设置了 state/executor 上下文，
    工具内的 ``get_state()`` / ``get_executor()`` / ``add_resource()`` 可正常使用。
    """

    _caller: Any = Field(default=None, repr=False, exclude=True)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: object
    ) -> ToolExecResult:
        result = await self._caller.run(**kwargs)
        return str(result) if not isinstance(result, str) else result
