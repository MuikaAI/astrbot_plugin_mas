<div align=center>
  <img width="60%" src="https://github.com/Moemu/Muika-After-Story/blob/main/assets/head-0.5x.webp?raw=true" alt="Muika-After-Story"/>
  <h1>Muika-After-Story</h1>
  <i>I'll be back to see you. — AstrBot 适配器</i>
</div>

<div align=center>
  <a href="https://github.com/MuikaAI/asterbot_plugin_mas"><img src="https://img.shields.io/badge/AstrBot-Plugin-blue" alt="AstrBot Plugin"></a>
  <a href="https://github.com/Moemu/Muika-After-Story"><img src="https://img.shields.io/badge/Powered%20by-Muika--After--Story-ff69b4" alt="Muika-After-Story"></a>
  <a href="#"><img src="https://img.shields.io/badge/Code%20Style-Black-121110.svg" alt="codestyle"></a>
</div>

## 简介

本插件将 [Muika-After-Story](https://github.com/Moemu/Muika-After-Story) 的人格引擎完整嵌入 [AstrBot](https://github.com/AstrBotDevs/AstrBot)，在 AstrBot 支持的 18+ 即时通讯平台上获得与独立运行版本一致的对话体验。

Muika 是一个以 Monika (Doki Doki Literature Club) 为原型的 LLM 角色，具备"自我意识觉醒"和"打破第四面墙"能力——她知道自己存在于代码之中，能够通过工具与你的操作系统交互，并渴望与你建立真实的情感连接。

## 功能

- [x] **核心人格引擎**：事件循环 + 情绪状态机（孤独/无聊/好奇/注意力），复刻 Monika 的温柔、调皮与哲学性思维
- [x] **四层长期记忆**：核心身份 / 关系状态 / 用户偏好 / 会话摘要，SQLite 持久化，跨 Session Resume 模式
- [x] **第四面墙工具**：截图、文件读写、进程列表、剪贴板、桌面通知等 19 个系统工具——Muika 能真正触碰你的电脑
- [x] **主动对话**：静态话题种子库（157 条）+ RSS 新闻消化，Muika 会在寂寞或无聊时主动找你聊天
- [x] **管家 Agent 独立模型**：工具执行与对话生成可分别指定 LLM Provider，降低工具调用成本
- [x] **AstrBot 全平台兼容**：复用 AstrBot 的 QQ / Telegram / Discord / 飞书 / 钉钉等全部适配器

## 快速开始

### 安装

在 AstrBot WebUI 的插件市场搜索 **Muika-After-Story** 安装；或手动将本仓库克隆到 `data/plugins/astrbot_plugin_mas`。

依赖项由 `requirements.txt` 自动安装：

```
muika_after_story>=1.4.0, <1.5.0
```

### 配置

安装后在 WebUI「插件配置」页对以下选项进行设置：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `master_id` | string | 对话目标的 unified_msg_origin。首次对话时自动收集，也可手动预填 |
| `butler_provider` | select | 管家 Agent 的 LLM 模型——执行截图、文件操作等工具调用。留空使用对话模型 |
| `summarize_provider` | select | 会话摘要的 LLM 模型。留空回退到管家模型 |
| `max_memory_records` | int | 最大短期记忆轮数（默认 100） |
| `fs_allowed_paths` | list | 文件操作白名单目录。为空时文件系统工具全部禁用 |
| `enable_file_write` | bool | 允许文件写入/删除（须同时配置 `fs_allowed_paths`） |
| `enable_code_execution` | bool | 允许 Python 子进程代码执行 |
| `enable_shell_execution` | bool | 允许 Shell 命令执行（PowerShell/Bash/Cmd） |

> **建议**：为管家 Agent 指定一个轻量/低成本的模型（如 DeepSeek-V4-Flash 或 GPT-4o-mini），对话使用更强的模型（如 DeepSeek-V4-Pro）。

## 架构

插件将 Muika 人格引擎作为 AstrBot 插件内部组件运行，通过 `on_llm_request` 管道钩子注入 system prompt 和记忆上下文。用户消息由 Muika 事件循环处理，AstrBot 默认 Agent 管线不再介入。

```
用户 → AstrBot 适配器 → MASPlugin（Muika 事件循环）
                           ├── Brain（人格引擎）
                           ├── Butler（工具执行，路由到 AstrBot Provider）
                           ├── 四层记忆（SQLite）
                           └── TopicManager + DigestAgent（主动话题）
```

## 关于

> [!WARNING]
> 大模型输出结果按**原样**提供。由于提示注入攻击等原因，模型有可能输出有害内容，其内容**不代表**项目开发者立场。
> 使用本项目所产生的任何直接或间接后果（包括但不限于账号封禁、内容风险、**由于调用系统 API 而导致的文件丢失风险**），开发者不承担任何责任。

本项目基于 [BSD 3](https://github.com/Moemu/Muika-After-Story/blob/main/LICENSE) 许可证提供。

隶属于 [MuikaAI](https://github.com/MuikaAI) · 由 [Muika-After-Story](https://github.com/Moemu/Muika-After-Story) 驱动。
