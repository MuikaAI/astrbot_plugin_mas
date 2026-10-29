"""AstrBot 消息组件 ↔ MAS IPC 消息的映射。

上行：从事件组件中提取文本与附件（临时文件在事件存活期内读取）。
下行：把 Core 下发的资源解析为本地文件并组装 ``MessageChain``。
"""

from __future__ import annotations

import logging
import mimetypes
import os
from typing import List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import aiohttp
from astrbot.api.event import MessageChain
from astrbot.api.message_components import (
    BaseMessageComponent,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)

from .ipc.attachments import MAX_ATTACHMENT_BYTES, AttachmentTransfer
from .ipc.protocol import OutgoingFile

logger = logging.getLogger("astrbot_plugin_mas")

_REPLY_PREFIX = "\n被引用的消息: "


async def extract_content(
    components: Sequence[BaseMessageComponent],
) -> Tuple[str, List[OutgoingFile]]:
    """提取消息文本与待上传附件；无法取得内容的组件跳过并记录。"""
    texts: List[str] = []
    quotes: List[str] = []
    files: List[OutgoingFile] = []
    for comp in components:
        try:
            if isinstance(comp, Plain):
                texts.append(comp.text)
            elif isinstance(comp, Reply):
                quote = "".join(
                    part.text for part in (comp.chain or []) if isinstance(part, Plain)
                )
                if quote.strip():
                    quotes.append(quote)
            elif isinstance(comp, (Image, Record, Video, File)):
                item = await _component_file(comp)
                if item is not None:
                    files.append(item)
            # At / Face 等唤醒与表情组件不进入对话
        except Exception as e:
            logger.warning(
                f"[MAS] Failed to extract {type(comp).__name__} component: {e!r}"
            )
    text = "".join(texts) + "".join(_REPLY_PREFIX + quote for quote in quotes)
    return text, files


def build_chain(content: str, resolved: Sequence[Tuple[str, str]]) -> MessageChain:
    """把文本与已解析到本地的资源组装成一条消息链。"""
    chain = MessageChain()
    if content:
        chain.message(content)
    for rtype, path in resolved:
        if rtype == "image":
            chain.file_image(path)
        elif rtype == "audio":
            chain.chain.append(Record(file=path))
        elif rtype == "video":
            chain.chain.append(Video(file=path))
        else:
            chain.chain.append(File(name=os.path.basename(path), file=path))
    return chain


async def resolve_resources(
    resources: Sequence[dict], transfer: AttachmentTransfer
) -> List[Tuple[str, str]]:
    """把 Core 下发的资源解析为 (类型, 本地路径)；同机部署时直接复用 Core 的本地路径。"""
    resolved: List[Tuple[str, str]] = []
    for res in resources:
        rtype = str(res.get("type") or "")
        try:
            if str(res.get("url") or "").startswith("/attachments/"):
                path = await transfer.download(res)
            else:
                path = str(res.get("path") or "")
        except Exception as e:
            logger.warning(
                f"[MAS] Failed to resolve {rtype or 'unknown'} resource: {e!r}"
            )
            continue
        if path and os.path.isfile(path):
            resolved.append((rtype, path))
        else:
            logger.warning(
                f"[MAS] Resource of type {rtype!r} is not accessible: {path!r}"
            )
    return resolved


async def _component_file(comp: BaseMessageComponent) -> Optional[OutgoingFile]:
    """读取单个媒体组件的内容，本地路径优先，其次 HTTP URL。"""
    if isinstance(comp, Image):
        rtype = "image"
    elif isinstance(comp, Record):
        rtype = "audio"
    elif isinstance(comp, Video):
        rtype = "video"
    else:
        rtype = "file"
    name = str(getattr(comp, "name", "") or "")
    if isinstance(comp, File):
        path = await comp.get_file()
        content = _read_local(path)
        suffix = _suffix(name or path)
    else:
        content = None
        for candidate in (
            getattr(comp, "path", None),
            getattr(comp, "file", None),
            getattr(comp, "url", None),
        ):
            content = _read_local(candidate)
            if content is not None:
                suffix = _suffix(str(candidate))
                break
        if content is None:
            url = _http_url(getattr(comp, "url", None)) or _http_url(
                getattr(comp, "file", None)
            )
            if url is None:
                logger.warning(
                    f"[MAS] {type(comp).__name__} carries no accessible content, skipped"
                )
                return None
            content = await _download(url)
            suffix = _suffix(url)
    if content is None:
        return None
    if len(content) > MAX_ATTACHMENT_BYTES:
        logger.warning(
            f"[MAS] {type(comp).__name__} exceeds {MAX_ATTACHMENT_BYTES} bytes, skipped"
        )
        return None
    mimetype = mimetypes.guess_type("x" + suffix)[0] if suffix else None
    return OutgoingFile(type=rtype, content=content, mimetype=mimetype)


def _read_local(candidate: Optional[str]) -> Optional[bytes]:
    """候选值指向存在的本地文件（含 ``file://`` URI）时返回其内容。"""
    if not candidate:
        return None
    candidate = str(candidate)
    if candidate.startswith("file://"):
        candidate = url2pathname(unquote(urlsplit(candidate).path))
    try:
        if os.path.isfile(candidate):
            with open(candidate, "rb") as f:
                return f.read()
    except OSError as e:
        logger.warning(f"[MAS] Failed to read {candidate!r}: {e!r}")
    return None


def _http_url(candidate: Optional[str]) -> Optional[str]:
    if candidate and str(candidate).startswith(("http://", "https://")):
        return str(candidate)
    return None


def _suffix(name_or_url: Optional[str]) -> str:
    """从文件名或 URL 路径中取扩展名（含点），无则空串。"""
    if not name_or_url:
        return ""
    path = (
        urlsplit(str(name_or_url)).path
        if "://" in str(name_or_url)
        else str(name_or_url)
    )
    _, ext = os.path.splitext(path)
    return ext.lower()


async def _download(url: str) -> bytes:
    """下载一个 HTTP 媒体文件，超过附件上限即中止。"""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=60)
        ) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                content.extend(chunk)
                if len(content) > MAX_ATTACHMENT_BYTES:
                    raise ValueError("Chat attachment exceeds 20 MiB")
            return bytes(content)
