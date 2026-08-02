"""MAS → AstrBot 环境适配补丁。

每个函数修复一类不兼容问题，全部幂等可重复调用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .config import PluginConfig

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# ── data dir ──────────────────────────────────────────────────────────────────


def redirect_data_dir(plugin_name: str) -> None:
    """``mas_config.data_dir`` → ``data/plugin_data/<plugin_name>``。"""
    from muika.config import mas_config

    data_dir = Path(get_astrbot_plugin_data_path()) / plugin_name
    mas_config.data_dir = data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"[MAS/Asterbot] MAS data dir → {data_dir}")


# ── topics.yml ────────────────────────────────────────────────────────────────


def redirect_topics_path() -> None:
    """让 TopicStore 使用插件自带的 ``configs/topics.yml``。

    site-packages 不含此文件，而 ``TopicStore.__init__`` 的默认参数在
    模块导入时就固化了旧路径，因此需要同时覆盖 ``_TOPICS_PATH`` 常量
    和给 ``__init__`` 套 wrapper 替换默认值。
    """
    import muika.core.topic_manager as _tm

    topics_path = Path(__file__).resolve().parent.parent / "configs" / "topics.yml"
    if not topics_path.is_file():
        logger.warning(f"[MAS/Asterbot] 插件未打包 configs/topics.yml: {topics_path}")
        return

    _tm._TOPICS_PATH = topics_path
    _orig_init = _tm.TopicStore.__init__

    def _wrapper(self, path: Optional[Path] = None) -> None:
        _orig_init(self, path or _tm._TOPICS_PATH)

    _tm.TopicStore.__init__ = _wrapper
    logger.info(f"[MAS/Asterbot] topics.yml → {topics_path}")


# ── alembic ───────────────────────────────────────────────────────────────────


def patch_alembic() -> None:
    """绕过缺失的 ``alembic.ini``，直接使用 muika 包内 ``migrations/``。

    原本 ``db._run_migrations`` 用 ``Path(__file__)`` 推导配置路径，
    site-packages 不含源码根目录的 alembic.ini。
    """
    import muika
    import muika.database.db as db

    async def _migrate(db_path: Path) -> None:
        from alembic import command as alembic_command
        from alembic.config import Config as AlembicConfig

        migrations_dir = Path(muika.__file__).resolve().parent / "migrations"
        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(migrations_dir))
        cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")

        def _sync() -> None:
            db._ensure_alembic_version_table(db_path, cfg)
            alembic_command.upgrade(cfg, "head")

        await asyncio.to_thread(_sync)
        db._cleanup_alembic_modules()

    db._run_migrations = _migrate
    logger.info("[MAS/Asterbot] Alembic migration routed to muika/migrations")


# ── loguru ────────────────────────────────────────────────────────────────────

_patched_loguru: bool = False


def patch_loguru() -> None:
    """给全局 loguru logger 挂 patcher，补齐 AstrBot handler 需要的 extra 字段。

    AstrBot 的 handler 格式含 ``{extra[plugin_tag]}`` 等字段，
    muika（用全局 logger）打日志时会命中这些 handler 但缺少 extra → ``KeyError``。
    用 ``setdefault`` 补齐，不覆盖 AstrBot 自身已填写的值。
    """
    global _patched_loguru
    if _patched_loguru:
        return

    from loguru import logger as loguru_logger

    _short = {
        "DEBUG": "DBUG",
        "INFO": "INFO",
        "WARNING": "WARN",
        "ERROR": "ERRO",
        "CRITICAL": "CRIT",
    }

    def _enrich(record) -> None:
        extra = record["extra"]
        extra.setdefault("plugin_tag", "[MAS]")
        extra.setdefault(
            "short_levelname",
            _short.get(record["level"].name, record["level"].name[:4].upper()),
        )
        extra.setdefault("astrbot_version_tag", "")
        extra.setdefault("source_file", "muika")
        extra.setdefault("source_line", record["line"])
        extra.setdefault("is_trace", False)
        extra.setdefault("category", "system")

    loguru_logger.configure(patcher=_enrich)
    _patched_loguru = True
    logger.info("[MAS/Asterbot] Loguru patcher installed")


# ── config sync ───────────────────────────────────────────────────────────────


def sync_config(config: "PluginConfig") -> None:
    """将 AstrBot WebUI 配置同步到 ``mas_config``。

    MAS 工具写文件/执行代码的权限检查读 ``mas_config``，
    但用户通过 AstrBot WebUI 设的值在 ``AstrBotConfig`` 里。
    """
    from muika.config import mas_config

    mas_config.fs_allowed_paths = config.fs_allowed_paths
    mas_config.enable_file_write = config.enable_file_write
    mas_config.enable_code_execution = config.enable_code_execution
    mas_config.enable_shell_execution = config.enable_shell_execution

    logger.info(
        "[MAS/Asterbot] Config synced: "
        f"fs_paths={len(config.fs_allowed_paths)} "
        f"write={config.enable_file_write} "
        f"code={config.enable_code_execution} "
        f"shell={config.enable_shell_execution}"
    )
