# 故障排查

## 启动时缺少环境变量

错误：

```text
Missing required environment variable: LLM_BASE_URL
```

处理：

```bash
cp .env.example .env
```

至少配置：

```env
LLM_BASE_URL=...
LLM_API_KEY=...
```

确认从项目目录运行，或已在当前进程环境中设置变量。不要把 `.env` 提交到 Git。

## 模型不能调用工具

现象：

- 模型只描述操作但不返回 `tool_calls`；
- 响应字段与 OpenAI Chat Completions 不兼容；
- 工具参数始终为空或不是 JSON 字符串。

处理：

1. 确认模型支持 OpenAI-compatible `tools` 和 `tool_calls`。
2. 确认使用 Chat Completions 兼容地址，而不是普通文本生成地址。
3. 用 `--trace-file` 检查规范化消息和工具调用。
4. 查看服务商原始错误，核对模型名和 API 权限。

参见 [LLM 与工具协议](llm-protocol.md)。

## Agent 调用次数很多

当前保护包括：

- 每轮原始需求和当前子任务提醒；
- 连续无新证据停滞检测；
- 默认 96 次需求级共享硬上限；
- 最后一次无工具最终回答。

可以降低：

```env
AGENT_TOTAL_ITERATION_BUDGET=48
AGENT_STAGNATION_LIMIT=4
```

过低会使大型改造在完成测试前收尾。查看 Web 进度中的
`requirement_budget_low`、`stagnation_observed` 和工具链，判断模型是在推进任务
还是重复探索。

## 返回 `stopped_with_answer`

这表示工具循环已安全停止，但不一定完成全部需求。检查：

```json
{
  "workflow": {
    "status": "stopped_with_answer",
    "stop_reason": "budget_exhausted"
  }
}
```

常见 `stop_reason`：

- `budget_exhausted`
- `stagnation`
- `model_error`
- `empty_model_response`

最终文本会区分已完成、已验证和未完成内容。下一次需求应明确要求继续未完成部分，
并先检查工作区和测试状态。

## 上下文超限

错误通常说明安全压缩后仍超过 `LLM_MAX_INPUT_TOKENS`。

处理：

- 缩短单次用户需求中的超长粘贴内容；
- 把大型规范上传到知识库，而不是直接放进需求；
- 用项目索引定位后分段读取文件；
- 合理提高模型上下文配置，且保证：

```text
LLM_MAX_INPUT_TOKENS + LLM_MAX_OUTPUT_TOKENS
<= LLM_CONTEXT_WINDOW
```

不要仅提高 `AGENT_COMPACT_AT_TOKENS` 到输入上限以上。

## 项目索引没有找到新代码

通常新需求开始和 `apply_patch` 后会自动刷新。外部编辑器刚修改文件时可执行：

```bash
simple-agent --workspace /path/to/project --refresh-index
simple-agent --workspace /path/to/project --index-status
```

仍有问题时：

1. 检查文件是否超过 2 MiB、属于二进制或不支持的扩展名。
2. 检查文件是否位于 `node_modules`、`dist`、`.venv` 等跳过目录。
3. 确认 `.simple-agent/index` 不是符号链接。
4. 可在停止服务后删除 `.simple-agent/index/`，下次需求会完整重建。

删除索引不会删除源码、会话、知识库或 Episode。

## 项目图谱或 Neo4j 没有更新

```bash
simple-agent --workspace /path/to/project --refresh-graph
simple-agent --workspace /path/to/project --graph-status
```

SQLite 保底图谱位于 `.simple-agent/graph/project-graph.db`。Neo4j 是默认请求
后端，若状态显示已经降级：

1. 确认已执行 `pip install -e .`；
2. 检查 `NEO4J_URI`、用户名、密码和数据库名；
3. 查看状态中的 `neo4j_last_error`；
4. 修复连接后再次刷新，成功时间会写入 `neo4j_last_sync`。

Neo4j 不可用不会使本地代码修改回滚。状态中的 `backend=sqlite`、
`fallback_active=true` 和 `fallback_reason` 会说明降级原因。停止 Agent 后可删除
`graph/` 重建本地图谱；Neo4j 中旧工作区快照会在下一次成功同步时替换。

## 知识文档无法导入

- 单文件不能超过 50 MiB。
- 最多提取 2,000,000 字符。
- DOC、PPT、XLS 旧格式需要先转换。
- 扫描 PDF 没有文本层时需要先 OCR。
- 损坏的 Office ZIP 会被拒绝。
- `.env` 和私钥文件不应作为知识文档导入。

查看已导入文档：

```bash
simple-agent --list-knowledge
```

## 命令被拒绝

错误 `command is not allowlisted` 表示该程序或子命令不在允许列表。Agent 不支持
shell 管道、重定向、`cd` 或任意脚本执行。

优先在项目的标准测试脚本中定义验证命令，然后使用受支持的
`npm run`、`python -m pytest` 等入口。不要为了绕过限制扩大允许列表。

## Web 页面一直显示排队

同一工作区的需求串行运行；前一个 Job 未完成时，后续 Job 保持排队。检查：

```http
GET /api/jobs/<job-id>
```

若服务已重启，旧 Job ID 无法恢复。使用会话需求列表或 Episode 判断上一需求是否
已经持久化，然后重新提交未完成需求。

## Web 无法从其他机器访问

这是预期行为。CLI 只允许绑定 localhost，服务也没有认证。当前版本不应暴露为
远程多用户服务。

## 状态文件损坏

操作前先备份 `.simple-agent/`。不同子目录可以独立重建：

- `index/`：可删除并自动重建；
- `graph/`：可删除并从项目索引自动重建；
- `knowledge/`：删除会丢失已导入知识，需要重新导入；
- `memory/` 和 `episodes/`：删除会永久丢失会话和历史。

不要在 Agent 或 Web Job 正在运行时修改这些文件。
