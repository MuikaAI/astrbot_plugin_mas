"""插件配置模型 —— 对标 ``_conf_schema.json`` 的类型安全表示。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class PluginConfig(BaseModel):
    """从 ``AstrBotConfig`` dict 构造的类型化配置。

    WebUI 通过 ``_conf_schema.json`` 渲染表单，存入 ``data/config/<name>_config.json``。
    插件启动时 AstrBot 自动加载为 ``AstrBotConfig`` 实例（dict 子类），
    这里将其转为带校验的 Pydantic 模型。
    """

    master_id: str = Field(default="", description="主账号 unified_msg_origin")
    butler_provider: str = Field(
        default="", description="管家 Agent 的 LLM Provider ID"
    )
    summarize_provider: str = Field(
        default="", description="会话摘要的 LLM Provider ID"
    )

    max_memory_records: int = Field(default=100, ge=1, description="最大短期记忆轮数")

    fs_allowed_paths: list[str] = Field(
        default_factory=list, description="文件操作白名单目录"
    )
    enable_file_write: bool = Field(default=False, description="允许文件写入/删除")
    enable_code_execution: bool = Field(
        default=False, description="允许 Python 代码执行"
    )
    enable_shell_execution: bool = Field(
        default=False, description="允许 Shell 命令执行"
    )

    @classmethod
    def from_astrbot_config(cls, raw: dict[str, Any]) -> PluginConfig:
        """从 AstrBotConfig dict 构造，忽略不在模型中的多余 key。"""
        return cls.model_validate(
            {k: v for k, v in raw.items() if k in cls.model_fields}
        )

    @property
    def has_master(self) -> bool:
        return bool(self.master_id)

    @property
    def butler_provider_id(self) -> Optional[str]:
        """有效的 Butler provider ID：配置值或 None。"""
        return self.butler_provider or None

    @property
    def summarize_provider_id(self) -> Optional[str]:
        """有效的 Summarize provider ID：配置值或 None（回退到 butler 或默认）。"""
        return self.summarize_provider or None
