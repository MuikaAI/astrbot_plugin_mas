"""MAS → AstrBot 桥接入口。

用法::

    from core.bridge import install_bridge
    install_bridge(self.context, plugin_name=\"astrbot_plugin_mas\",
                   get_umo=lambda: self.master_id, config=self.config)
"""

from __future__ import annotations

from typing import Callable, Optional

from astrbot.api import logger
from astrbot.api.star import Context as AstrContext

from .config import PluginConfig
from .model import DUMMY_MODEL_CONFIG, AsterbotModel, FakeConfigManager
from .patches import (
    patch_alembic,
    patch_loguru,
    redirect_data_dir,
    redirect_topics_path,
    sync_config,
)


def install_bridge(
    context: AstrContext,
    plugin_name: str,
    get_umo: Callable[[], Optional[str]],
    config: PluginConfig | None = None,
) -> None:
    """安装桥接——必须在 ``Muika()`` 构造之前调用。

    ``Muika.__init__`` 内构造 Brain/Butler/DigestAgent 时会触发
    ``muika.llm.load_model()``。此函数将其全部路由到 AstrBot Provider，
    并适配路径/日志/迁移/权限等环境差异。
    """
    import muika.config as _cfg
    import muika.core.brain as _brain
    import muika.core.butler.agent as _butler
    import muika.core.digest_agent as _digest
    import muika.llm as _llm
    import muika.llm.loader as _llm_loader

    # 环境适配
    redirect_data_dir(plugin_name)
    redirect_topics_path()
    patch_alembic()
    patch_loguru()
    if config is not None:
        sync_config(config)

    # LLM 路由：每个 Agent 使用独立 AsterbotModel，支持不同 Provider
    butler_provider = config.butler_provider_id if config else None
    summarize_provider = config.summarize_provider_id if config else None

    brain_model = AsterbotModel(context, get_umo)
    digest_model = AsterbotModel(context, get_umo, provider_id=butler_provider)
    butler_model = AsterbotModel(context, get_umo, provider_id=butler_provider)
    summarize_model = AsterbotModel(
        context, get_umo, provider_id=summarize_provider or butler_provider
    )

    # _butler.load_model 会被 ButlerAgent.__init__ 调用两次：
    # 第一次 → self.model，第二次 → self.summarize_model
    _butler_models = iter([butler_model, summarize_model])
    _butler.load_model = lambda _c=None: next(_butler_models)

    _llm_loader.load_model = lambda _c=None: brain_model
    _llm.load_model = lambda _c=None: brain_model
    _brain.load_model = lambda _c=None: brain_model
    _digest.load_model = lambda _c=None: digest_model

    # 模型配置 stub：避免无 configs/models.yml 时抛错
    fake_cfg = FakeConfigManager()
    _brain.get_model_config_manager = lambda: fake_cfg
    _butler.get_model_config = lambda *a, **k: DUMMY_MODEL_CONFIG
    _digest.get_model_config = lambda *a, **k: DUMMY_MODEL_CONFIG
    _cfg.get_model_config = lambda *a, **k: DUMMY_MODEL_CONFIG
    _cfg.get_model_config_manager = lambda: fake_cfg

    logger.info("[MAS/Asterbot] Bridge installed: load_model() → AstrBot Provider")
