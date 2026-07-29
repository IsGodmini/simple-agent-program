# Simple Agent Program

一个使用 OpenAI 兼容接口和工具调用能力的最小开发 Agent。

当前支持：

- 多轮 Chat Completions 调用
- 标准 `tools` / `tool_calls` 工具协议
- 分页列出和查找大型项目文件
- 搜索代码、符号及调用位置
- 生成紧凑仓库地图
- 按行分段读取大型 UTF-8 文本文件
- 创建新文件或精确替换已有文本
- 运行受控的测试、构建和静态检查命令
- 按需将完整执行过程保存为 JSON
- 请求前估算上下文 Token，并在 80K 时安全压缩旧工具交互
- 将输入限制在 96K、输出限制在 16K，总预算为 128K
- 将单次需求工作上下文与跨需求项目记忆分离
- 自动保存任务摘要，并按需检索详细情景记忆
- 导入多格式项目资料并构建本地 RAG 知识库
- 按需求自动检索相关规范，也可通过工具继续查询和精读
- 自动在 ReAct 与 Plan-and-Act 之间路由复杂任务
- 使用独立 Reflection Reviewer 基于代码和测试证据验收结果
- 限制工具只能访问指定工作目录
- 隐藏并拒绝读取 `.env`、私钥和 Git 内部文件
- 最大迭代次数保护

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

然后在 `.env` 中填写 API Key。

## 使用

```bash
simple-agent "分析这个项目当前实现了什么"
```

### Web 客户端

项目包含一个本地 Web 客户端，可以管理工作区会话、提交开发需求、观察
Plan-and-Act / Reflection 执行过程，以及上传或删除共享知识库资料：

```bash
simple-agent-web --workspace /path/to/project
```

然后访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。也可以用
`--port` 指定其他端口。

客户端和 Agent 都只在本机运行，服务只允许绑定 `127.0.0.1` 或 `localhost`。
任务状态在页面中实时轮询；会话、需求摘要、场景记忆和知识库仍持久化在目标项目
的 `.simple-agent/` 目录中。为了避免多个 Agent 同时修改同一个项目，同一工作区
提交的需求会串行执行。

模型的完整回复会按安全 Markdown 渲染，支持标题、列表、任务列表、引用、链接、
图片、代码块和表格。执行观察区会实时显示上下文构建、模式路由、模型调用、工具
执行、计划步骤、Reflection 评审、结果整理和场景记忆写入过程；只展示阶段信息，
不泄露模型隐藏推理或工具参数。

也可以指定其他项目目录：

```bash
simple-agent --workspace /path/to/project "分析项目结构"
```

默认使用 `auto` 模式：小型任务直接进入 ReAct；复杂任务会先由只读 Planner
调查仓库并生成结构化计划，再为每个步骤创建隔离的 Executor，最后由只读
Reflection Reviewer 检查需求、代码和验证证据。

```bash
# 强制使用普通 ReAct
simple-agent --agent-mode react "修正 README 中的错别字"

# 强制使用 Plan-and-Act
simple-agent --agent-mode plan "重构认证模块并完成数据库迁移"
```

普通 ReAct 执行过程中如果发现新的复杂子任务，也可以向顶层编排器申请一次
Plan-and-Act 升级。编排器不允许子 Agent 继续递归创建 Agent。

角色权限：

- Planner：仅能搜索和读取项目、记忆与知识库。
- Executor：可以读取、精确修改文件并运行受控命令。
- Reviewer：不能修改文件，但可以读取当前代码、Git 差异和运行受控验证。

计划步骤、实际工具证据、评审结论和修订次数会写入 Episode 与可选 trace。

## 工作区与会话

一个工作区可以包含多个会话，每个会话可以连续提交多个需求。不指定会话时，
命令兼容地使用该工作区的 `default` 会话。

```bash
# 创建会话；输出中会包含 session_id
simple-agent --workspace /path/to/project \
  --new-session --session-title "认证模块"

# 在指定会话中执行多个连续需求
simple-agent --workspace /path/to/project \
  --session <session-id> "实现登录接口"
simple-agent --workspace /path/to/project \
  --session <session-id> "增加登录失败次数限制"

# 查看工作区中的所有会话
simple-agent --workspace /path/to/project --list-sessions
```

同一会话会自动延续会话摘要和最近需求摘要，但不会重放旧需求的原始工具对话。
同一工作区的不同会话共享知识库和场景记忆；其他会话的场景记忆只有与当前需求
至少存在两个检索词项重合且状态为 `completed` 时才会自动注入。失败 Episode
仍会保存，也可以显式使用 `search_memory` / `read_episode` 查看，但不会自动
进入其他会话。

保存完整执行日志：

```bash
simple-agent \
  --trace-file .simple-agent/latest.json \
  "为项目添加一个健康检查函数并运行测试"
```

## 项目知识库

知识库用于保存用户提供的开发注意事项、项目规范、设计文档和其他参考资料。
导入操作完全在本地完成，不需要调用 LLM：

```bash
# 导入一个或多个文件
simple-agent --workspace /path/to/project \
  --knowledge-file ./开发规范.md \
  --knowledge-file ./架构设计.pdf

# 递归导入目录中的受支持文档
simple-agent --workspace /path/to/project \
  --knowledge-dir ./project-docs

# 导入后立即执行需求
simple-agent --workspace /path/to/project \
  --knowledge-file ./接口规范.docx \
  "实现用户注册接口"

# 查看或删除已索引文档
simple-agent --workspace /path/to/project --list-knowledge
simple-agent --workspace /path/to/project \
  --remove-knowledge <document-id>
```

支持的主要格式：

- 文本及开发文件：TXT、Markdown、RST、代码、配置、YAML、TOML、SQL 等
- 结构化数据：JSON、JSONL、CSV、TSV、HTML、XML
- 办公文档：PDF、DOCX、PPTX、XLSX
- 开放文档：ODT、EPUB

旧版 DOC、PPT、XLS 需要先转换为对应的新版格式；扫描型 PDF 当前不包含 OCR，
需要先转换为可搜索 PDF。单文件默认上限为 50 MiB，最多提取 200 万字符。

文档会被解析、分块并写入项目的
`.simple-agent/knowledge/knowledge.db`。检索使用 SQLite FTS5 和中英文词项，
不依赖外部 Embedding 服务。每次新需求只自动注入少量相关片段，并保留
`knowledge:<document-id>#chunk-<n>` 引用；完整知识库不会被塞入上下文。

## 工具

- `repository_map`：统计技术栈清单、入口、扩展名和顶层模块
- `list_files`：按深度和 offset 分页列出项目结构
- `find_files`：使用 Glob 分页查找文件
- `search_code`：搜索文本或正则表达式，返回文件和行号
- `read_file`：使用行范围分段读取带行号的文本
- `search_knowledge`：检索相关项目规范和参考片段
- `read_knowledge`：根据文档 ID 和片段序号精读知识
- `list_knowledge`：列出已索引文档，不加载正文
- `apply_patch`：创建文件，或通过唯一的 `old_text` 精确替换文本
- `run_command`：运行允许列表内的命令，不经过 Shell
- `search_memory`：搜索之前需求的紧凑摘要
- `read_episode`：按任务 ID 读取详细情景记忆

`run_command` 当前支持 Python 的 `unittest`、`pytest`、`compileall`、
`ruff`、`mypy`，常用的测试/构建型 npm、pnpm、yarn、Go、Cargo 命令，
以及 `git status/diff/log/show` 等只读 Git 命令。

命令允许列表用于降低误操作风险，但不等同于操作系统级沙箱。项目自身的
测试或构建脚本仍然是本地代码，只应在可信仓库中运行。

## 上下文预算

默认配置：

```env
LLM_CONTEXT_WINDOW=128000
LLM_MAX_INPUT_TOKENS=96000
LLM_MAX_OUTPUT_TOKENS=16000
AGENT_COMPACT_AT_TOKENS=80000
AGENT_MODE=auto
AGENT_MAX_ITERATIONS=64
AGENT_TOTAL_ITERATION_BUDGET=512
AGENT_ITERATION_EXTENSION=16
AGENT_STAGNATION_LIMIT=6
AGENT_PLAN_COMPLEXITY_THRESHOLD=3
AGENT_MAX_PLAN_STEPS=12
AGENT_MAX_STEP_REVISIONS=2
AGENT_PLANNER_MAX_ITERATIONS=24
AGENT_REVIEWER_MAX_ITERATIONS=24
```

`AGENT_MAX_ITERATIONS` 是每个 Executor 的初始执行额度，不再是固定终止上限；
Planner 和 Reflection Reviewer 分别使用自己的初始额度。达到初始额度时，只要
Agent 仍在读取新的证据、产生新的代码修改或获得新的测试结果，就会每次按照
`AGENT_ITERATION_EXTENSION` 自动增加额度。

连续 `AGENT_STAGNATION_LIMIT` 轮只得到重复工具结果或工具参数错误时，系统才会
认定 Agent 陷入无进展循环并停止。`AGENT_TOTAL_ITERATION_BUDGET` 是整个需求中
所有 Planner、Executor、Reviewer 和最终结果整理共享的灾难保护上限，正常任务
不应依赖它结束。Web 客户端会显示自动扩容、停滞计数和需求总预算使用情况。

Agent 会使用保守的 UTF-8 长度估算请求 Token。在达到压缩阈值后，只会
淘汰完整的旧工具交互块，保留最初的系统消息、用户任务和最新工具结果。
如果安全压缩后仍超过输入上限，请求会在本地终止。

## 跨需求记忆

每次 CLI 调用被视为一个独立需求。需求内部的模型消息、工具调用和工具结果
构成临时工作上下文；需求结束后，只把紧凑摘要自动提供给同一会话的后续需求，
原始过程不会自动进入新上下文。其他会话只能按相关性获得已完成场景记忆。

本地记忆结构：

```text
.simple-agent/
├── memory/
│   ├── conversation_sessions.json
│   └── task_summaries.json
└── episodes/
    └── <task-id>.json
```

`conversation_sessions.json` 记录会话标题、会话摘要和需求 ID；
`task_summaries.json` 记录每条需求所属的 `session_id`、结果、可信度、修改文件
和验证命令。Episode 保存完整消息和工具执行过程，只能通过
`search_memory` / `read_episode` 按需访问。
`.simple-agent` 已被 Git 和普通文件工具忽略。

记忆可能落后于当前代码，因此 Agent 会把当前文件和测试结果作为最终事实来源。

## 测试

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```
