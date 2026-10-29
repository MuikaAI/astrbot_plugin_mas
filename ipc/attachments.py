"""聊天附件的认证上传与下载（20 MiB 上限，内容哈希寻址）。

与 Core 侧 ``muika.ipc.attachments`` 的 HTTP 端点配对：
``PUT/GET /attachments/{sha256}``，凭 ``X-Auth-Token`` 认证。
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .protocol import OutgoingFile

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

_ATTACHMENT_TIMEOUT = aiohttp.ClientTimeout(total=60)

_ATTACHMENT_KEY = re.compile(r"[0-9a-f]{64}")


class AttachmentTransfer:
    """只向配置的 Core 入口发送认证信息，下载前校验内容哈希。"""

    def __init__(self, endpoint: str, secret: str, directory: Path) -> None:
        parsed = urlsplit(endpoint)
        scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
        self.base = urlunsplit((scheme, parsed.netloc, "", "", ""))
        self._secret = secret
        self._directory = directory

    async def upload(self, item: OutgoingFile) -> dict:
        """上传附件内容，返回可放入 ``resources`` 的 Resource.to_dict() 字段。"""
        if len(item.content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Chat attachment exceeds 20 MiB")
        url = "/attachments/" + hashlib.sha256(item.content).hexdigest()
        async with aiohttp.ClientSession(
            headers={"X-Auth-Token": self._secret}
        ) as session:
            async with session.put(
                self.base + url, data=item.content, timeout=_ATTACHMENT_TIMEOUT
            ) as response:
                response.raise_for_status()
        return {"type": item.type, "path": "", "mimetype": item.mimetype, "url": url}

    async def download(self, resource: dict) -> str:
        """下载 Core 上传的附件到插件数据目录，返回本地路径。"""
        url = str(resource.get("url") or "")
        if not url.startswith("/attachments/"):
            raise ValueError(
                "Attachment does not belong to the configured Core endpoint"
            )
        key = url.removeprefix("/attachments/")
        if not _ATTACHMENT_KEY.fullmatch(key):
            raise ValueError("Invalid attachment hash")
        suffix = (
            mimetypes.guess_extension(str(resource.get("mimetype") or "")) or ".bin"
        )
        target = self._directory / (key + suffix)
        if not target.is_file():
            async with aiohttp.ClientSession(
                headers={"X-Auth-Token": self._secret}
            ) as session:
                async with session.get(
                    self.base + url, timeout=_ATTACHMENT_TIMEOUT
                ) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        content.extend(chunk)
                        if len(content) > MAX_ATTACHMENT_BYTES:
                            raise ValueError("Chat attachment exceeds 20 MiB")
            if hashlib.sha256(content).hexdigest() != key:
                raise ValueError("Downloaded attachment hash does not match")
            self._directory.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f".{uuid.uuid4().hex}.download")
            temporary.write_bytes(bytes(content))
            temporary.replace(target)
        return str(target)
