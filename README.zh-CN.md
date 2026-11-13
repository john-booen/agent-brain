# agent-brain

[English](README.md)

把不同 AI 客户端的工作按项目汇到一起。AgentsView 采集历史，agent-brain 读取可见对话，由模型生成摘要和判断项目归属。每个项目有带出处的进展卡和日志。Agent 完成工作后调用 `log_work` 更新项目。

## 快速开始

需要 Python 3.11+、Node.js 22+、Zstandard、[AgentsView v0.47.0](https://github.com/kenn-io/agentsview/releases/tag/v0.47.0) 命令行程序，以及用于摘要的 OpenAI 兼容模型。默认数据目录是 `~/SharedBrain`，也可通过 `AGENT_BRAIN_DATA` 指定仓库之外的目录。

1. 将 `examples/config.example.json` 复制为数据目录下的 `config.json`，填入模型地址与模型名。
2. 若 AgentsView 不在 PATH，设置 `AGENTSVIEW_BIN`。采集前停止这个档案的 AgentsView 服务，然后运行 `node collect.mjs`。如只需更新指定来源，可以传入来源名，例如 `node collect.mjs kimi-work cowork`。查询时会按需启动启用认证的本机回环服务。
3. 运行 `python3 brain.py backfill`，生成 `_index.md`、各项目 `card.md` 与 `log.md`。可用 `--agent kimi-work --sort messages:asc --limit 8` 先跑一小批短会话。重跑会跳过已经处理且未变化的会话。
4. 将 `python3 /绝对路径/agent-brain/brain.py mcp` 注册为本地 MCP，再启用 [skill](skills/agent-brain/SKILL.md) 或把规则加入客户端工作说明。

要定时更新本地索引，可由操作系统定期运行 `AGENT_BRAIN_SYNC_DAEMON=1 node collect.mjs`，并提供 `AGENTSVIEW_BIN` 和 `ZSTD_BIN` 的绝对路径。采集器按文件变化更新脱敏副本，通过启用认证的本机 AgentsView 服务同步。一次只运行一轮采集。自动采集只更新原文索引，项目摘要由 `backfill` 或 `log_work` 更新。

Codex 和 Claude Cowork 用本地 MCP。DeepSeek Harness 用 stdio 连接器。Kimi Work 用个人插件。四端连接同一个本机数据目录。云端 Cowork 会话的读取取决于可用的本地副本或官方导出。

按项目名直接调用 `get_project`，获得短卡和可查分区。项目名称不明确时用 `find_project`，跨项目总览用 `list_projects`。列表和分区用返回的 `next_offset` 翻页。每页工具返回采用 UTF-8 字节上限，给采用字节分词的客户端预留 2,000 Token 内的返回预算。工具返回和模型整轮用量分别计量。查询读取现有结构化记录，在本地生成短卡。

## 虚构演示

运行 `python3 scripts/demo.py`。演示用第一个 MCP 客户端写入一条虚构 Codex 工作记录，再用第二个客户端读取同一张进展卡。

我也在实际的 Claude Cowork 窗口里打开了生成的 Markdown 进展卡，问「Fictional Atlas 做到哪了，谁负责回滚，下一步是什么，请附 Codex 出处」。Cowork 回读了发布清单、负责回滚的发布协调员、周五预演，以及 `codex:fictional-atlas-demo` 的出处。随后 Cowork 用本机 Python 命令查询同一组虚构数据，并写入一条 `log_work`。挂载的测试文件夹中，`code/` 和 `data/` 是并列目录。

![虚构的跨工具项目进展演示](examples/demo.svg)

## 数据存放

采集只读原始日志。建立索引前在独立副本里替换可识别的密钥。真实日志、配置、会话库、摘要和项目卡都放在仓库外。本机模型可在本机完成摘要。使用其他模型地址时，文本会发往该地址。旧聊天里的操作指令仅作历史资料。

Codex 自动审批子会话保留在索引里，历史回填会跳过这些机器生成的审批记录。如果模型接口支持 OpenAI 兼容的 `json_schema`，可在配置中把 `model.structured_output` 设为 `true`，约束摘要格式。若有会话失败，回填返回失败状态并保存已经完成的分段供续跑。

用 `AGENT_BRAIN_MODEL_CONFIG` 指向私有模型配置文件，可以为单次运行指定接口。`chunk_chars` 设置每次调用的近似字符数。`match_catalog_names` 只把对话中出现的项目名作为候选传给模型。`extra_body.stream` 可以启用流式输出。遇到额度或网关错误时，批次停在当前会话，可以续跑或换接口。
