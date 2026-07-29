# 系统架构

## 定位

Simple Agent Program 是一个本地优先的软件开发 Agent。文件读取、代码修改、命令
执行、索引、记忆和知识库位于用户工作区；选中的提示词、代码片段和工具结果会通过
OpenAI-compatible Chat Completions 接口发送到用户配置的 LLM 服务。

项目提供两个入口：

- `simple-agent`：一次 CLI 调用代表一个需求。
- `simple-agent-web`：本地 FastAPI 服务和浏览器客户端，可管理多个会话和需求。

## 核心模块

| 模块 | 职责 |
|---|---|
| `config.py` | 读取和校验环境变量 |
| `llm.py` | OpenAI-compatible Chat Completions 适配器 |
| `agent.py` | 单个工具调用循环、停滞检测、调用预算和最终回答 |
| `workflow.py` | ReAct、Plan-and-Act、Reflection 和复杂度路由 |
| `context.py` | Token 估算及完整工具交互块压缩 |
| `session.py` | 单次需求生命周期、Episode 和 trace |
| `memory.py` | 会话摘要、需求摘要、场景记忆检索和上下文构建 |
| `knowledge.py` | 多格式文档解析、分块和本地 FTS5 RAG |
| `project_index.py` | 持久化项目树、代码块、符号和依赖增量索引 |
| `tools/` | 文件、命令、项目索引、知识库和记忆工具 |
| `webapp.py` | 本地 HTTP API、后台 Job 和工作区串行调度 |
| `web/` | 无构建步骤的 HTML、CSS 和 JavaScript 客户端 |

## 一次需求的执行链路

```text
CLI / Web 请求
    ↓
Workspace 路径校验
    ↓
SessionManager.start_requirement
    ↓
ContextBuilder
    ├── 原始需求锚点
    ├── 当前会话摘要
    ├── 相关跨会话场景记忆
    ├── 知识库相关片段
    └── 项目索引相关片段
    ↓
WorkflowOrchestrator
    ├── ReAct Executor
    └── Planner → Executor(s) → Reviewer → Synthesizer
    ↓
SessionManager.complete_task
    ├── 更新需求摘要和会话摘要
    └── 写入完整 Episode
```

所有 Planner、Executor、Reviewer 和最终综合共享一个需求级模型调用预算。

## ReAct

简单需求直接交给 Executor：

```text
LLM → tool_calls → 本地工具 → tool 消息 → LLM
```

如果没有修改文件或运行命令，通常直接返回结果。发生修改或验证后，编排器会调用
只读 Reflection Reviewer。普通 ReAct 若发现一个新的复杂子任务，可申请一次
Plan-and-Act 升级；子 Agent 不能递归创建更多编排器。

## Plan-and-Act

复杂需求的流程是：

1. Planner 使用只读工具调查项目，输出结构化无环计划。
2. 编排器按依赖顺序执行每个步骤。
3. 每个 Executor 只接收当前步骤、原始需求和已完成步骤摘要。
4. Reviewer 根据当前文件、Git 差异和验证证据返回 `pass`、`revise` 或 `blocked`。
5. `revise` 会触发有限次数修订。
6. 全部步骤通过后，Synthesizer 整理最终回答。

Planner 和 Reviewer 没有 `apply_patch`。Reviewer 使用比 Executor 更严格的只读
命令工具。

## 持久化状态

每个工作区都有独立状态：

```text
.simple-agent/
├── index/
│   ├── project-index.db
│   └── repository-map.json
├── knowledge/
│   └── knowledge.db
├── memory/
│   ├── conversation_sessions.json
│   └── task_summaries.json
└── episodes/
    └── <requirement-id>.json
```

这些文件不会进入 Git，也不会被普通文件工具返回。数据库使用 SQLite；会话和需求
摘要使用 JSON。

## 隔离和并发

- 不同工作区的索引、知识库、会话和 Episode 完全隔离。
- 同一工作区的不同会话共享项目索引、知识库和相关场景记忆。
- 同一会话连续需求会继承会话摘要和最近需求摘要。
- Web 后台最多使用 4 个工作线程，但同一工作区通过互斥锁串行执行，避免两个
  Agent 同时修改同一个项目。
- Web Job 记录只存在于当前 Python 进程内；服务重启不会恢复排队或运行中的 Job。
  已经完成并写入 Episode 的需求仍可读取。

## 结束状态

正常完成返回 `AgentResult`。达到预算、检测到停滞或模型响应异常时，Agent 会停止
工具循环并生成最终回答。若是提前收尾，工作流包含：

```json
{
  "status": "stopped_with_answer",
  "stop_reason": "budget_exhausted",
  "iteration_budget": {
    "used": 96,
    "maximum": 96,
    "remaining": 0
  }
}
```

最后一次模型调用专用于无工具回答。如果模型服务也失败，程序返回本地兜底文本，
但不会把未验证的工作描述为完成。
