"""IPC 消息协议 —— Bot ↔ Core 之间通过 WebSocket 传输的 JSON 消息。

字段与 ``muika.ipc.protocol``（v1.6.0）保持一致，适配器侧不依赖 muika 包。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

# Bot → Core
TYPE_USER_MESSAGE = "user_message"
TYPE_COMMAND = "command"
TYPE_SESSION_BOOTSTRAP = "session_bootstrap"

# Core → Bot
TYPE_SEND_MESSAGE = "send_message"
TYPE_COMMAND_RESULT = "command_result"
TYPE_ACTION_RESPONSE = "action_response"
TYPE_ERROR = "error"


def make_envelope(msg_type: str, **fields: Any) -> dict:
    """构造一条带信封字段（id/type/ts）的 IPC 消息。"""
    return {
        "id": str(uuid.uuid4()),
        "type": msg_type,
        "ts": datetime.now().isoformat(),
        **fields,
    }


@dataclass
class OutgoingFile:
    """待上传的聊天附件，内容在事件存活期内读取完毕。"""

    type: str
    """资源类型：image / audio / video / file"""

    content: bytes
    """文件内容"""

    mimetype: Optional[str] = None
    """文件元数据类型(eg. ``image/jpeg``)，无法识别时为 None"""
