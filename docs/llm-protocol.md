# LLM 与工具协议

## 接口要求

项目使用 OpenAI-compatible Chat Completions：

```text
POST <LLM_BASE_URL>/chat/completions
```

具体路径由 OpenAI Python SDK 根据 `base_url` 组合。模型必须支持标准
`tools`、`tool_calls` 和 `role: tool` 消息。项目当前不使用 Responses API，也不
使用流式输出。

必需配置：

```env
LLM_MODEL=ark-code-latest
LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3
LLM_API_KEY=your-api-key
```

`LLM_BASE_URL` 和 `LLM_API_KEY` 必填；模型名有默认值。

## 发送请求

适配器构建的请求核心结构如下：

```json
{
  "model": "ark-code-latest",
  "messages": [
    {
      "role": "system",
      "content": "你是一个软件开发 Agent..."
    },
    {
      "role": "user",
      "content": "实现健康检查接口"
    }
  ],
  "max_tokens": 8000,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "读取项目中的文本文件...",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string"}
          },
          "required": ["path"],
          "additionalProperties": false
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

没有工具的最终综合或强制收尾不会发送 `tools` 和 `tool_choice`。

## 模型请求工具

兼容响应中的 assistant 消息示例：

```json
{
  "role": "assistant",
  "content": "行动说明：读取入口文件以确认路由结构",
  "tool_calls": [
    {
      "id": "call_abc",
      "type": "function",
      "function": {
        "name": "read_file",
        "arguments": "{\"path\":\"src/app.py\",\"start_line\":1,\"max_lines\":200}"
      }
    }
  ]
}
```

工具由 `function.name` 选择，不是通过响应数组的 `index` 字段选择。
`function.arguments` 是 JSON 字符串，解析后必须是对象。

模型可以在同一 assistant 消息中请求多个工具。程序按响应顺序执行，并为每个调用
生成独立 tool 消息。

## 返回工具结果

本地执行完成后，程序把结果追加到下一次请求：

```json
{
  "role": "tool",
  "tool_call_id": "call_abc",
  "content": "1 | from fastapi import FastAPI\n2 | app = FastAPI()"
}
```

`tool_call_id` 必须与模型请求中的 `id` 一致。工具成功与否不依赖 HTTP
`finish_reason`；参数错误、未知工具和允许范围内的执行错误会以文本返回：

```text
Tool error: unknown tool 'delete_everything'
```

这样模型可以修正参数，而不是让整个 Agent 进程立即失败。

## `finish_reason` 和 `index`

- `finish_reason` 由模型服务生成，项目没有规定它的取值，也不依赖它判断是否执行
  工具。
- 是否执行工具由 assistant 消息中是否存在 `tool_calls` 决定。
- Chat Completions 响应中的 choice `index` 只表示候选响应位置，不是工具编号。
- 工具调用通过 `tool_calls[n].function.name` 分派。

## Agent 循环

```text
构建 messages + tools
    ↓
调用 LLM
    ├── 返回 tool_calls
    │      ↓
    │   执行本地工具
    │      ↓
    │   追加 tool 消息并再次调用 LLM
    └── 返回非空 content
           ↓
        当前 Agent 完成
```

同一个 Agent 的消息会保留到当前子任务结束，超过上下文阈值后按完整工具交互块
压缩。Planner、Executor 和 Reviewer 通过结构化结果衔接，不重放彼此完整历史。

## 调用预算和强制回答

默认每个需求共享 24 次模型调用。普通工具循环最多使用前 23 次，最后一次保留给
禁止工具的最终回答。

以下情况会触发强制回答：

- 普通调用额度耗尽；
- 连续工具轮次没有新证据；
- 模型返回既无 content 也无 tool_calls 的空消息；
- 模型请求异常。

若保留的模型调用也失败，程序生成本地兜底答复，明确说明无法确认完成状态。

## 调试真实 JSON

CLI 可以将完整规范化消息和工具执行过程写入 trace：

```bash
simple-agent \
  --trace-file .simple-agent/latest.json \
  "分析项目入口"
```

trace 包含发送前在 Agent 内维护的消息、工具参数和结果，但不包含 SDK 返回对象的
全部 HTTP 元数据。不要把含有敏感代码或数据的 trace 提交到 Git。
