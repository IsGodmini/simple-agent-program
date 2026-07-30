# Neo4j 项目图谱、LLM 文件档案与 Chroma 检索

## 数据职责

项目理解层使用三类互相独立的存储：

- SQLite 源码索引：文件元数据、代码块、符号、import 和 FTS5/BM25；
- Neo4j：唯一项目关系图和文件功能档案存储；
- Chroma：代码块、知识片段、文件档案和记忆摘要的向量。

不存在 SQLite 图谱副本。Neo4j 不可用时，关键词源码索引仍可工作，但图谱工具会
返回不可用状态。

## 增量构建

1. 源码索引先用大小和修改时间检查变化，只重新读取变化文件并计算内容哈希。
2. 每次 `apply_patch` 只更新确定性源码索引，并将对应 Neo4j 文件节点标记为
   `stale=true`；此时不调用档案 LLM，也不重新生成 Embedding。
3. 需求完成后按去重的变化文件列表执行一次批量刷新。
4. 从索引缓存取得变化文件的代码片段、符号和 import 证据。
5. 对内容哈希或档案版本发生变化的文件，按最多 2 个文件一批调用 LLM，并限制为
   最多 3 批并发；档案请求使用独立的输出预算和超时，避免阻塞主 Agent。
6. LLM 输出 `purpose`、`responsibilities`、`confidence` 和行号证据。
7. 每个成功批次立即以 `draft=true` 暂存到 Neo4j；若其他批次失败，下次只重试
   尚未成功的文件。
8. 保留未变化文件已有的 Neo4j 档案。
9. 全部档案齐全后，在一个 Neo4j 事务内替换该工作区的节点和关系快照。
10. 按内容哈希更新 Chroma；未变化内容不重新生成 Embedding。

LLM 必须输出 JSON，遗漏文件或缺少用途、职责时本次图谱刷新失败，旧 Neo4j
事务不会被部分覆盖。LLM 档案是导航信息，修改代码前仍必须读取当前源码。
如果文件修改后恢复到原内容哈希，系统直接清除 `stale` 标记，不调用 LLM。

## Neo4j 模型

节点：

- `ProjectWorkspace`
- `ProjectFile`
- `ProjectSymbol`
- `ProjectModule`
- `ProjectNode`：以上节点的公共标签

关系使用真实 Neo4j 类型：

- `CONTAINS`
- `DEFINES`
- `IMPORTS`
- `DEPENDS_ON`
- `TESTS`

所有节点和关系带 `workspace_id`。节点使用
`(workspace_id, node_key)` 组合唯一约束；文件档案建立 Neo4j 全文索引。

```dotenv
NEO4J_URI=neo4j://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=change-me
NEO4J_DATABASE=neo4j
```

## Chroma 混合检索

Chroma 数据位于：

```text
.simple-agent/vector/
```

集合按工作区和数据类型隔离：

- `code_chunks`
- `knowledge_chunks`
- `file_profiles`
- `memory_summaries`

项目只允许本机 Ollama 生成 Embedding：

```bash
ollama pull qwen3-embedding:0.6b
```

```dotenv
EMBEDDING_MODEL=qwen3-embedding:0.6b
EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1
```

不需要 API Key。代码会拒绝非回环地址和不以 `/v1` 结尾的地址，避免源码被误发到
远程 Embedding 服务。

查询流程：

```text
FTS5/BM25 或 Neo4j 全文候选
              +
       Chroma 向量候选
              ↓
 Reciprocal Rank Fusion
              ↓
 图关系扩展与上下文预算裁剪
```

代码块、知识片段和文件档案直接执行混合检索；场景记忆摘要也会写入 Chroma，并在
相关记忆选择时与词项结果融合。Chroma 与 Neo4j 不共享生命周期或数据库连接。

## 状态与操作

```bash
simple-agent --workspace /path/to/project --refresh-graph
simple-agent --workspace /path/to/project --graph-status
```

```http
GET /api/project-graph?workspace=/path/to/project
POST /api/project-graph/refresh?workspace=/path/to/project
```

状态中的 `backend` 固定为 `neo4j`，`storage` 为 `neo4j-only`。缺少配置、连接
失败、LLM 档案生成失败或事务失败会写入 `last_error`。向量状态独立显示是否启用、
Embedding 模型和本地 Chroma 路径。

旧版本可能遗留 `.simple-agent/graph/project-graph.db`；新代码不会读取或更新它。
确认不需要回退旧版本后可以手动删除 `.simple-agent/graph/`。
