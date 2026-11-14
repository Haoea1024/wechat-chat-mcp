# WeChat Chat MCP

**让你正在使用的智能体去微信查资料：跨群聊和私聊找信息，按需读附件，保存有出处的项目资料。**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](pyproject.toml)
[![Transport: MCP stdio](https://img.shields.io/badge/MCP-stdio-green.svg)](docs/INSTALL.md)

## 直接发给智能体安装

在能执行**本机 Windows 命令**的 WorkBuddy 或 Codex 中发送：

> 请安装 https://github.com/Haoea1024/wechat-chat-mcp 并接入我当前使用的客户端。按照仓库 docs/INSTALL.md 完成安装和验证。

智能体负责检查环境、安装依赖、连接微信、同步本地索引和注册 MCP。微信登录、多个账户的选择及客户端要求的授权由你处理，无需自己填写 MCP 配置。

- **WorkBuddy 5.9.3**：已由使用者验证 20 个原生工具可见、`project_status` 原生调用成功；具体项目检索和媒体读取仍待逐项验证。
- **Codex**：提供同一安装脚本的自动注册入口；旧版已有本机使用验证，新版客户端读图需验收。DSH 暂不作为本轮适配目标。

WorkBuddy 5.9.3 的确认入口是 **「专家·技能·连接器 → 连接器 → 自定义连接器」**，首次连接按提示点击“信任”。安装程序完成配置写入后，还需在当前客户端通过原生工具调用验证；重启不代替信任。命令行入口只作授权后的辅助方式。只发链接可能被理解为阅读，建议带上“安装并接入当前客户端”。

[安装指南](docs/INSTALL.md) · [完整使用说明](docs/USAGE.md) · [工具与格式参考](docs/REFERENCE.md)

## 可以怎样用

> 帮我查“订单同步”项目最近的讨论，先找相关群，再补充成员私聊和遗漏会话，告诉我进展、分歧和需要我处理的事情。

> 把已经核实的项目资料保存下来，保留出处和待确认问题。重要附件备份到本机，下次先看新增和变更。

> 群里的方案后来改了，去读最新消息和对应文件，核对旧结论。

> 我只负责接口部分，测试验收由同事负责。请记住这个分工，后续不要把验收任务算到我头上。

首次使用不预设项目；你说出项目名称和别名后，智能体可保存到本机。也可以直接查普通聊天。

## 核心能力

| 能力 | 使用方式 |
| --- | --- |
| 跨会话查询 | 先按群名和项目别名发现候选，再从相关群成员找私聊；成员关系只作线索 |
| 按需读证据 | 分页读取正文、上下文和附件，保留消息 ID、会话、时间与来源 |
| 项目资料 | 保存背景、进展、决定与疑问，保留版本；下次补读新增或变更消息 |
| 附件副本 | 按准确消息关联、内容指纹保存本机副本；原文件不动，可继续按需读取 |
| 用户纠正 | 保存明确分工和线下补充，可查看、可撤销 |
| 本地增量索引 | 默认同步近 30 天到 SQLite；同步不调用模型，不把整月聊天塞进上下文 |

支持图片、语音、PDF、Word、PowerPoint、Excel、文本和 ZIP；具体格式、依赖及部分读取限制见[附件说明](docs/REFERENCE.md#图片语音与文件)。资料摘要不替代原文，关键细节、矛盾和重要决定仍需回查证据。当前使用关键词与会话关联检索，没有向量数据库。

## 环境与边界

- 当前是 **Windows 优先的开发者预览版本**，读取能力受微信版本、本机同步与下载情况影响；不是微信官方插件。
- 微信数据库必须在运行服务的本机可访问；云端智能体不能仅凭仓库地址读取你的电脑。
- **默认无需额外 Codex CLI 或 API 密钥**：图片、扫描页和文档插图直接交回当前客户端识读。语音由本机 Whisper 转写，安装阶段下载并测试模型，查询时不临时下载。客户端仍需支持读图，实际展示与识读要验收。
- 未下载、已清理、缺失、失败或仅部分读取会明确标注；视频解析暂未实现，旧 DOC/PPT 转换需要可用的 Office/WPS。
- 默认由当前客户端查询、总结并保存资料；网页用项目卡片展示进展、下一步和待确认事项，完整资料与出处点开查看；更新时引导回当前对话。独立后台分析是可选项，普通 MCP 查询不需要一直开网页。

## 数据放在哪里

资料、配置与缓存默认在 `~/.wechat-project-assistant`，微信访问配置由上游组件保存在用户目录；均不应提交到仓库。附件副本仍是本机备份，不是异地备份。

同步本身不调用模型，但工具返回的聊天和附件内容会进入当前智能体的模型上下文；默认图片也交给当前客户端；仅显式启用独立模型时才使用额外模型入口。是否发送到云端取决于所用模型和客户端设置。本地保存不等于所有推理都在本机执行。

## 开发与反馈

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_native_command.ps1
```

测试使用虚构数据。欢迎用 Issues 反馈操作系统、微信版本、客户端版本、复现步骤与脱敏错误；不要附聊天数据库或密钥。当前由维护者独立写入，不接受外部 PR，公开源码可下载和 Fork。实现约定见 [AGENTS.md](AGENTS.md)。

## 致谢与许可证

采用 [Apache-2.0](LICENSE)。基于 [huohuoer/wechat-cli](https://github.com/huohuoer/wechat-cli) 的数据库访问与解码能力，参考 wechat-cli-plus 和 wechatauto-replica 的 Windows 与图片兼容实现，使用 MCP Python SDK、faster-whisper 及文档解析依赖。

上游说明见 [UPSTREAM_README.md](UPSTREAM_README.md)，具体来源与改动归属见 [NOTICE](NOTICE)。
