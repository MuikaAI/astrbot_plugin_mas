import sys
from pathlib import Path

# 仓库目录本身就是插件包（astrbot_plugin_mas），把其父目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
