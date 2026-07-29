# Web API

## 启动

```bash
simple-agent-web --workspace /path/to/project
```

默认监听 `127.0.0.1:8765`。交互式 OpenAPI 文档位于：

```text
http://127.0.0.1:8765/api/docs
```

服务没有认证，只允许通过命令行绑定 `127.0.0.1` 或 `localhost`。

## 通用规则

- `workspace` 必须是运行服务的本机可访问目录。
- 请求和响应使用 UTF-8 JSON；知识库上传使用 `multipart/form-data`。
- 参数或工作区错误通常返回 `400 {"detail": "..."}`。
- 不存在的 Job、Episode 或知识文档返回 `404`。
- Job 状态为 `queued`、`running`、`completed` 或 `failed`。
- 同一工作区的 Job 串行执行；不同工作区最多并行使用 4 个线程。
- 每个 Job 最多保留最近 200 条进度事件。
- Job 记录在内存中，服务重启后丢失；已完成需求的 Episode 持久化在工作区。

## 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/bootstrap` | 默认工作区、模式和上传限制 |
| GET | `/api/workspace` | 工作区会话、知识库和索引概览 |
| GET | `/api/sessions` | 列出会话 |
| POST | `/api/sessions` | 创建会话 |
| GET | `/api/sessions/{id}/requirements` | 列出会话需求摘要 |
| GET | `/api/episodes/{id}` | 读取完整 Episode |
| GET | `/api/requirements/{id}` | 读取持久化需求结果 |
| POST | `/api/requirements` | 提交后台需求 |
| GET | `/api/jobs/{id}` | 轮询 Job |
| GET | `/api/knowledge` | 列出知识文档 |
| POST | `/api/knowledge/upload` | 上传知识文档 |
| DELETE | `/api/knowledge/{id}` | 删除知识文档 |
| GET | `/api/project-index` | 查看项目索引概览 |
| POST | `/api/project-index/refresh` | 增量刷新项目索引 |

## 初始化

```http
GET /api/bootstrap
```

响应：

```json
{
  "default_workspace": "/path/to/project",
  "agent_modes": ["auto", "react", "plan"],
  "max_upload_bytes": 52428800
}
```

## 工作区和会话

```http
GET /api/workspace?path=/path/to/project
```

返回规范化路径、会话、知识文档和项目索引状态。首次打开空工作区时会创建
`default` 会话。

创建会话：

```bash
curl -X POST http://127.0.0.1:8765/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "workspace": "/path/to/project",
    "title": "认证模块"
  }'
```

列出会话：

```http
GET /api/sessions?workspace=/path/to/project
```

列出某会话需求：

```http
GET /api/sessions/<session-id>/requirements?workspace=/path/to/project
```

## 提交和轮询需求

```bash
curl -X POST http://127.0.0.1:8765/api/requirements \
  -H 'Content-Type: application/json' \
  -d '{
    "workspace": "/path/to/project",
    "session_id": "default",
    "request": "为项目增加健康检查并运行测试",
    "agent_mode": "auto"
  }'
```

`request` 长度为 1 到 100,000 字符；`agent_mode` 必须是 `auto`、`react` 或
`plan`。接口返回 `202` 和 Job：

```json
{
  "job_id": "job-0123456789ab",
  "status": "queued",
  "phase": "queued",
  "progress": [],
  "result": null,
  "error": ""
}
```

轮询：

```http
GET /api/jobs/job-0123456789ab
```

完成后 `result` 包含：

```json
{
  "requirement_id": "task-...",
  "session_id": "default",
  "content": "最终回答",
  "summary": {},
  "workflow": {},
  "iterations": 8,
  "compactions": 0
}
```

失败时 `status` 为 `failed`，错误文本位于 `error`。预算或停滞保护正常完成收尾
时 Job 仍是 `completed`，`workflow.status` 为 `stopped_with_answer`。

## 持久化结果

读取精简结果：

```http
GET /api/requirements/<requirement-id>?workspace=/path/to/project
```

读取完整 Episode：

```http
GET /api/episodes/<requirement-id>?workspace=/path/to/project
```

Episode 可能包含代码片段、工具参数和命令输出，应按敏感开发数据处理。

## 知识库

列出：

```http
GET /api/knowledge?workspace=/path/to/project
```

上传：

```bash
curl -X POST \
  'http://127.0.0.1:8765/api/knowledge/upload?workspace=/path/to/project' \
  -F 'files=@开发规范.md' \
  -F 'files=@架构设计.pdf'
```

单文件最大 50 MiB。删除：

```bash
curl -X DELETE \
  'http://127.0.0.1:8765/api/knowledge/<document-id>?workspace=/path/to/project'
```

## 项目索引

查看概览：

```http
GET /api/project-index?workspace=/path/to/project
```

手动增量刷新：

```bash
curl -X POST \
  'http://127.0.0.1:8765/api/project-index/refresh?workspace=/path/to/project'
```

通常不需要手动刷新：新需求开始和 `apply_patch` 修改后都会自动更新索引。
