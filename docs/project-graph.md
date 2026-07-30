# 项目知识图谱与文件功能档案

## 目标

项目知识图谱用于回答“哪个文件负责什么”“这个文件依赖谁”“修改它可能影响哪些
文件和测试”。它建立在增量源码索引之上，目的是把反复通读整个项目降为最低
优先级，而不是替代源码。

首次构建仍需由源码索引读取所有符合安全规则的文件。此后刷新先比较路径、大小、
修改时间和内容哈希，只为变化文件重建索引与功能档案；图关系从本地索引记录重建，
不会重新读取未变化源码。`apply_patch` 成功后会立即刷新对应路径，外部编辑器的
变化会在下个需求开始时被发现。

## 数据模型

本地数据库位于：

```text
.simple-agent/graph/project-graph.db
```

主要节点：

- `Workspace`：工作区；
- `File`：带内容哈希、语言和功能摘要的文件；
- `Symbol`：类、函数和其他公开声明；
- `Module`：无法直接解析为工作区文件的外部或逻辑模块。

主要关系：

- `CONTAINS`：工作区包含文件；
- `DEFINES`：文件定义符号；
- `IMPORTS`：文件导入模块；
- `DEPENDS_ON`：导入可解析到工作区文件；
- `TESTS`：测试文件与被测文件的启发式关联。

每个文件档案保存内容哈希、用途、职责、公开符号、导入、关联测试、置信度和证据。
当前用途与职责由路径、首段注释或文档字符串、符号和导入等确定性证据生成，不会
额外调用 LLM，因此可重复且成本稳定。档案发生 `stale=true` 或需要修改实现时，
必须读取当前源码核对。

## 查询优先级

Agent 默认按以下顺序理解项目：

1. `project_graph_overview` / `query_file_profiles` 找负责相关功能的文件；
2. `file_profile` / `query_project_graph` / `impact_analysis` 看职责和影响范围；
3. 项目源码索引工具定位精确代码片段和符号；
4. `read_file` 读取准备修改或必须核实的真实源码；
5. 只有图谱和索引不足时才扫描目录或全文搜索，通读全部源码为最低优先级。

图谱上下文最多自动检索 6 个相关档案，引用格式为 `graph:<path>`。图谱、
代码索引、知识库和场景记忆由同一工作区的所有会话共享。

## 默认后端与自动降级

默认请求 Neo4j：

```dotenv
PROJECT_GRAPH_BACKEND=neo4j
```

标准安装已经包含官方 Neo4j Python Driver。只有 URI、用户名和密码完整且最近
同步成功时，状态中的活动后端才是 `neo4j`。以下任一情况会自动使用 SQLite：

- Neo4j 连接配置不完整；
- 驱动初始化或连通性检查失败；
- 约束创建或快照同步失败。

SQLite 本地图谱始终同步维护，承担低延迟查询缓存和故障保底。它支持文件档案
FTS5 搜索以及最多四层关系遍历。下一次刷新会重试失败的 Neo4j 同步；成功后自动
清除错误并把活动后端切回 `neo4j`。如需强制仅使用本地图谱：

```dotenv
PROJECT_GRAPH_BACKEND=sqlite
```

## Neo4j 主后端

```dotenv
PROJECT_GRAPH_BACKEND=neo4j
NEO4J_URI=neo4j://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=change-me
NEO4J_DATABASE=neo4j
```

文件档案变化后，系统使用参数化 Cypher 把当前工作区节点和关系同步到 Neo4j，
并用 `(workspace_id, node_key)` 唯一约束隔离不同工作区。SQLite 查询缓存让模型
查询不依赖每次网络往返，也保证 Neo4j 故障不会破坏已完成的索引和代码修改。

凭据只能放在未提交的 `.env` 或进程环境中。远程 Neo4j 应使用其提供的加密连接
URI、最小权限账号和网络访问控制。当前版本不向模型开放任意 Cypher，模型只能
调用受边界约束的图谱工具。

## CLI 与 HTTP

```bash
simple-agent --workspace /path/to/project --refresh-graph
simple-agent --workspace /path/to/project --graph-status
```

```http
GET /api/project-graph?workspace=/path/to/project
POST /api/project-graph/refresh?workspace=/path/to/project
```

状态字段：

- `requested_backend`：配置要求的后端，默认 `neo4j`；
- `backend`：当前活动后端；
- `fallback_active` / `fallback_reason`：是否降级及原因；
- `neo4j_last_sync`：最近成功同步时间；
- `neo4j_last_error`：最近同步错误。

## 一致性边界

- 本地 Agent 编辑通过回调同步对应文件；
- 外部编辑在下一需求的全工作区元数据检查中发现；
- Neo4j 当前按变化触发工作区快照同步，不是逐边事务日志；
- 异常退出可能使 Neo4j 暂时落后，本地 SQLite 会立即接管；
- 删除 `.simple-agent/graph/` 只会丢失可重建图谱，不会删除源码、记忆或知识库。
