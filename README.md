# Simple Agent Program

一个使用 OpenAI 兼容接口、持久化项目理解和工具调用能力的本地优先开发 Agent。

当前支持：

- 多轮 Chat Completions 调用
- 标准 `tools` / `tool_calls` 工具协议
- 分页列出和查找大型项目文件
- 搜索代码、符号及调用位置
- 生成紧凑仓库地图
- 持久化工作区项目树、代码片段、符号和依赖关系索引
- 持久化文件功能档案与项目关系图，Neo4j 优先并自动降级到 SQLite
- 每个新需求只重读发生变化的源码文件
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

## 文档导航

- [系统架构](docs/architecture.md)：模块职责、执行链路、工作流和持久化状态
- [项目知识图谱](docs/project-graph.md)：文件职责、关系、增量维护和 Neo4j
- [上下文与记忆](docs/context-and-memory.md)：上下文顺序、隔离、压缩和任务锚点
- [LLM 与工具协议](docs/llm-protocol.md)：请求/响应 JSON、工具调用和收尾
- [Web API](docs/web-api.md)：本地 HTTP 接口、Job 状态和调用示例
- [安全边界](docs/security.md)：远程模型、文件、命令和本地 Web 风险
- [故障排查](docs/troubleshooting.md)：配置、循环、索引、知识库和 Web 问题
- [二次开发](docs/development.md)：项目结构、扩展方式、测试和提交检查

完整文档索引见 [docs/README.md](docs/README.md)。

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

Web 服务、工具执行和持久化数据位于本机，服务只允许绑定 `127.0.0.1` 或
`localhost`；选中的需求、代码片段和工具结果会发送到 `.env` 配置的远程 LLM
服务。任务状态在页面中实时轮询；会话、需求摘要、场景记忆和知识库仍持久化在
目标项目的 `.simple-agent/` 目录中。为了避免多个 Agent 同时修改同一个项目，
同一工作区提交的需求会串行执行。Web Job 只保存在当前进程内，服务重启后不会
恢复排队或运行中的 Job。

模型的完整回复会按安全 Markdown 渲染，支持标题、列表、任务列表、引用、链接、
图片、代码块和表格。执行观察区会实时显示上下文构建、模式路由、模型调用、工具
执行、计划步骤、Reflection 评审、结果整理和场景记忆写入过程；只展示阶段信息，
不泄露模型隐藏推理或工具参数。

需求执行期间，中间对话区会显示模型主动输出的简短公开行动说明，例如接下来要
检查什么、修改什么或验证什么；右侧执行观察区继续显示独立的模型与工具调用链。
只有严格使用 `行动说明：...` 协议的内容才会展示，其他内部内容会被忽略并替换成
基于工具名称生成的安全说明。

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

## 增量项目索引

项目首次收到需求时，会在本地构建工作区共享索引：

```text
.simple-agent/index/
├── project-index.db
└── repository-map.json
```

索引包含安全过滤后的文件树、语言和模块统计、清单与入口文件、代码片段全文索引、
类和函数等符号，以及 Python、JavaScript/TypeScript、Go、Rust 的主要依赖关系。
`.env`、私钥、Git 内部数据、虚拟环境、依赖目录和构建产物不会进入索引。

后续需求仍会检查文件路径、大小和修改时间，但不会重新读取内容没有变化的源码。
新增、修改和删除的文件会增量更新；Agent 使用 `apply_patch` 修改代码后，会立即
刷新对应文件。索引用于定位范围，修改前仍会使用 `read_file` 核对当前真实内容。

可以不调用 LLM，手动刷新或查看状态：

```bash
simple-agent --workspace /path/to/project --refresh-index
simple-agent --workspace /path/to/project --index-status
```

同一工作区的所有会话共享一份项目索引。新需求会自动注入紧凑项目地图和少量相关
代码片段，不会把完整项目树或全部代码加入模型上下文。

## 项目知识图谱

项目索引之上还会维护 `.simple-agent/graph/project-graph.db`，持久化每个文件的
功能、职责、公开符号、依赖、关联测试和内容哈希，以及 `DEFINES`、
`DEPENDS_ON`、`TESTS` 等关系。新需求先查询图谱和文件档案，再查询精确源码
片段；只有准备修改或需要核实时才读取真实文件。未变化文件不会重复读取。

默认优先使用 Neo4j，标准安装已包含官方驱动。若连接配置不完整、驱动初始化失败
或服务暂时不可用，系统会自动使用 `.simple-agent/graph/project-graph.db` 中的
SQLite 本地图谱；后续刷新会重试 Neo4j，恢复成功后自动切回。

```dotenv
PROJECT_GRAPH_BACKEND=neo4j
NEO4J_URI=neo4j://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=change-me
NEO4J_DATABASE=neo4j
```

SQLite 同时作为低延迟查询缓存和故障保底，因此 Neo4j 临时不可用不会阻断 Agent；
活动后端、降级状态和原因会显示在图谱状态中。详细模型、一致性边界和安全说明见
[项目知识图谱文档](docs/project-graph.md)。

```bash
simple-agent --workspace /path/to/project --refresh-graph
simple-agent --workspace /path/to/project --graph-status
```

## 工具

- `project_graph_overview`：查看图谱规模、关系类型和代表性文件职责
- `query_file_profiles`：按功能或概念检索文件档案
- `file_profile`：读取单个文件的职责、依赖、测试和证据
- `query_project_graph`：遍历文件、符号、模块和测试关系
- `impact_analysis`：分析修改文件的潜在影响与验证范围
- `graph_status` / `refresh_project_graph`：查看或增量刷新图谱
- `project_overview`：读取缓存的项目树、模块、语言、入口和索引状态
- `query_project_index`：从持久化 FTS5 索引检索相关代码片段
- `search_symbols`：定位类、函数、接口等声明
- `find_references`：从缓存代码片段查找符号引用
- `dependency_graph`：查看 import、require、use 等依赖关系
- `index_status`：查看项目索引规模和最近刷新时间
- `refresh_project_index`：按需增量刷新指定文件或整个索引
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
LLM_MODEL=ark-code-latest
LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3
LLM_API_KEY=your-api-key
LLM_CONTEXT_WINDOW=128000
LLM_MAX_INPUT_TOKENS=96000
LLM_MAX_OUTPUT_TOKENS=16000
AGENT_COMPACT_AT_TOKENS=80000
AGENT_MODE=auto
AGENT_MAX_ITERATIONS=64
AGENT_TOTAL_ITERATION_BUDGET=96
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
所有 Planner、Executor、Reviewer 和最终结果整理共享、不可突破的模型调用硬
上限，默认 96 次。预算进入最后约 20%（最多保留 12 次）时，系统会向当前 Agent
注入收敛指令，要求停止扩展调查范围，优先完成必要修改与验证，或明确报告阻塞。
其中最后 1 次调用专门保留给无工具的最终答复：达到硬上限或检测到停滞时，
系统会停止后续规划与评审，要求模型根据已有证据说明完成情况、验证结果和未完成
事项。如果最终模型调用本身失败，系统会返回本地兜底答复，因此客户端不会因为
循环保护只得到一个异常。Web 客户端会显示自动扩容、停滞计数、预算告警、强制
收尾和需求总预算使用情况。

Agent 会使用保守的 UTF-8 长度估算请求 Token。在达到压缩阈值后，只会
淘汰完整的旧工具交互块，保留最初的系统消息、用户任务和最新工具结果。
如果安全压缩后仍超过输入上限，请求会在本地终止。

当前用户需求会以不可丢失的任务锚点加入需求上下文。除此之外，每次调用 LLM
前都会在上下文末尾临时加入原始需求、当前子任务范围和目的驱动约束：工具结果
必须能够推进实现决策、代码修改或验证，否则应停止继续调查；证据足够时必须
立即回复。这个逐轮提醒不会写回长期对话，所以不会随循环次数重复累积。
Planner、Executor、Reflection Reviewer、修复任务和最终结果整理始终共享同一个
原始需求，即使早期工具交互被上下文压缩也不会丢失总体目标。

## 跨需求记忆

每次 CLI 调用被视为一个独立需求。需求内部的模型消息、工具调用和工具结果
构成临时工作上下文；需求结束后，只把紧凑摘要自动提供给同一会话的后续需求，
原始过程不会自动进入新上下文。其他会话只能按相关性获得已完成场景记忆。

本地工作区状态结构：

```text
.simple-agent/
├── index/
│   ├── project-index.db
│   └── repository-map.json
├── graph/
│   └── project-graph.db
├── knowledge/
│   └── knowledge.db
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
