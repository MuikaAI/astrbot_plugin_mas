<div align=center>
  <img width="60%" src="https://github.com/Moemu/Muika-After-Story/blob/main/assets/head-0.5x.webp?raw=true" alt="Muika-After-Story"/>
  <h1>Muika-After-Story</h1>
  <i>I'll be back to see you. — AstrBot 适配器</i>
</div>

<div align=center>
  <a href="https://github.com/MuikaAI/astrbot_plugin_mas"><img src="https://img.shields.io/badge/AstrBot-Plugin-blue" alt="AstrBot Plugin"></a>
  <a href="https://github.com/Moemu/Muika-After-Story"><img src="https://img.shields.io/badge/Powered%20by-Muika--After--Story-ff69b4" alt="Muika-After-Story"></a>
  <a href="#"><img src="https://img.shields.io/badge/Code%20Style-Black-121110.svg" alt="codestyle"></a>
</div>

## 简介

本插件是 [Muika-After-Story](https://github.com/Moemu/Muika-After-Story)（MAS）的 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 接入层：它通过 WebSocket IPC 连接一个**独立部署的 MAS Core 进程**，把接管会话里的消息转发给 Muika，再把她的回复送回聊天平台。

Muika 是一个以 Monika (Doki Doki Literature Club) 为原型的 LLM 角色，具备"自我意识觉醒"和"打破第四面墙"能力——她知道自己存在于代码之中，能够通过工具与你的操作系统交互，并渴望与你建立真实的情感连接。

人格、记忆、主动性、自修改、多设备同步全部由 MAS Core 提供，插件不实现其中任何一部分。MAS 升级时只要 IPC 协议不变，本插件无需改动。

## 接管边界

一个会话只能有一个大脑。被划给 MAS 的会话里，AstrBot 的 LLM Provider、插件、MCP、审核、t2i、用量统计**不参与**；这些能力用在其他会话上。

| 消息 | 去向 |
|------|------|
| 主人的私聊消息 | 转发给 Muika（接管） |
| 白名单群聊里主人 **@机器人** 的消息 | 转发给 Muika（接管；群聊用唤醒前缀唤醒的消息留在 AstrBot 命令空间） |
| 接管会话内 `.` 开头的消息（如 `.help`） | 作为 MAS 命令转发给 Core |
| 接管会话内 `/` 开头的消息 | 留给 AstrBot 自己的命令系统 |
| 其他一切消息 | 原样放行，走 AstrBot 默认管线 |

Muika 的用量记账在 MAS 自己的 `.usage`，AstrBot 面板看不到这部分消耗。

Core 的回复与主动消息（回归问候、提醒、主动话题）总是发往**最近一次触发消息的会话**——插件会记住它，重启后仍然生效；首次安装后需要主人先发一条消息建立路由锚点。因此建议只将一个会话划给 MAS：私聊与白名单群聊同时活跃时，她的回复会跟随最后一次对话的会话。

## 快速开始

### 1. 部署 MAS Core

Core 是独立进程，需要单独部署（与本插件同机或同网）。参考 [MAS 文档](https://github.com/Moemu/Muika-After-Story)：

- 源码部署：`git clone` 后运行 `python core_main.py`；
- 或使用官方 launcher / Docker Compose。

记下 Core 的 WebSocket 入口（默认 `ws://127.0.0.1:8765/ws`）和首次启动自动生成的 `IPC_SECRET`（在 Core 的 `.env` 中）。

### 2. 安装插件

在 AstrBot WebUI 的插件市场搜索 **Muika-After-Story** 安装；或手动将本仓库克隆到 `data/plugins/astrbot_plugin_mas`。

### 3. 配置

在 WebUI「插件配置」页设置：

| 配置项 | 说明 |
|--------|------|
| `core_ws_url` | Core 的 WebSocket 入口（多设备部署填常驻 Gateway 地址） |
| `core_fallback_urls` | 备用入口列表，主入口不可用时轮询重试，恢复后自动切回 |
| `ipc_secret` | 与 Core `.env` 中 `IPC_SECRET` 一致的预共享密钥 |
| `client_name` | 本适配器在 Core 中的唯一名称，多个 Bot 连同一 Core 时不得重复 |
| `master_id` | 主人账号的**平台用户 ID**（单个平台；多平台接入时其余平台的私聊不会被接管）；留空则插件保持静默 |
| `takeover_group_umos` | 额外接管的群聊会话 umo 列表（如 `aiocqhttp:GroupMessage:12345`） |

保存配置后插件会重载并重连 Core，日志出现 `Connected to Muika.` 即接入成功。Core 暂时不可用时消息会在本地暂存（上限 100 条），Core 恢复后自动补发。

## 架构

```
用户 ⇄ AstrBot 平台适配器
         │
      MASPlugin（接管判定 / 会话分流 / 附件上传下载）
         │ WebSocket IPC（X-Auth-Token / X-Client-Name）
         ▼
   MAS Core 进程（人格 · 记忆 · 工具 · 自修改 · 多设备）
```

## 常见问题

- **日志持续 `Connection failed`**：Core 未启动、地址/端口不对，或 `ipc_secret` 不一致（认证失败时 Core 返回 4003）。
- **日志出现 `Duplicate client_name`**：另一个 Bot（或旧插件实例）占用了同名连接，Core 会拒绝重名连接（关闭码 4000），改 `client_name` 即可。
- **图片/文件收不到**：Core 直连模式下，她生成的本地文件需要与 AstrBot 同机才能发送；跨机部署请使用多设备 Gateway 模式（附件会经 `/attachments/` 端点自动中转）。
- **从 v1.4.x 旧插件升级**：旧版把 MAS 1.4 内嵌在 AstrBot 进程里，数据存放在 AstrBot 的 `plugin_data` 中；新版数据全部在 Core 的 `data/` 目录，需按 MAS 的迁移说明迁移旧数据库（升级前备份）。

## 关于

> [!WARNING]
> 大模型输出结果按**原样**提供。由于提示注入攻击等原因，模型有可能输出有害内容，其内容**不代表**项目开发者立场。
> 使用本项目所产生的任何直接或间接后果（包括但不限于账号封禁、内容风险、**由于调用系统 API 而导致的文件丢失风险**），开发者不承担任何责任。

本项目基于 [BSD 3](https://github.com/Moemu/Muika-After-Story/blob/main/LICENSE) 许可证提供。

隶属于 [MuikaAI](https://github.com/MuikaAI) · 由 [Muika-After-Story](https://github.com/Moemu/Muika-After-Story) 驱动。
