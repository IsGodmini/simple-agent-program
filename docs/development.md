# 二次开发

## 开发环境

项目要求 Python 3.9 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

运行测试：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests
node --check src/simple_agent/web/app.js
git diff --check
```

构建 wheel：

```bash
python -m build --wheel --no-isolation
```

## 目录结构

```text
src/simple_agent/
├── agent.py              单 Agent 工具循环
├── workflow.py           ReAct / Plan / Reflection 编排
├── context.py            Token 预算和压缩
├── memory.py             会话与场景记忆
├── session.py            需求生命周期
├── knowledge.py          文档解析和 RAG
├── project_index.py      增量源码索引
├── project_graph.py      Neo4j 优先关系图、文件档案和 SQLite 保底
├── llm.py                Chat Completions 适配器
├── cli.py                CLI 入口和依赖装配
├── webapp.py             FastAPI 与后台 Job
├── prompts.py            中文角色提示词
├── workspace.py          工作区路径边界
├── tools/                Agent 工具
└── web/                  原生 Web 客户端
tests/                    unittest 测试
docs/                     设计与使用文档
```

`cli.build_agent` 是主要依赖装配点：LLM、工具注册表、上下文管理器和工作流在这里
连接。Web 默认复用同一个工厂。

## 新增工具

1. 在 `src/simple_agent/tools/` 新建或选择模块。
2. 继承 `Tool` 并定义：

```python
class ExampleTool(Tool):
    name = "example"
    description = "清楚说明什么时候使用，以及返回什么。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"}
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def execute(self, arguments):
        return "text returned to the model"
```

3. 从 `tools/__init__.py` 导出。
4. 在 `cli.build_agent` 的正确角色注册表中注册。
5. 添加成功、无结果、非法参数和安全边界测试。
6. 更新 README 工具列表和相关文档。

不要把写工具注册给 Planner 或 Reviewer。工具应返回有界文本，避免把整仓库或超长
命令输出放入上下文。

## 新增知识文档解析器

文档解析入口是 `DocumentParser`：

1. 将扩展名加入支持集合。
2. 实现只读解析，返回 `ParsedSection`。
3. 对压缩包格式调用归档校验，防止异常成员。
4. 遵守 50 MiB 源文件和 2,000,000 字符提取上限。
5. 添加真实最小样本、损坏样本和二进制拒绝测试。

解析器不能执行文档中的宏、脚本或外部链接。

## 修改项目索引

项目索引使用 SQLite FTS5：

- `files`：元数据和内容哈希；
- `chunks`：代码片段和行号；
- `symbols`：声明；
- `imports`：依赖边；
- `code_fts`：全文检索。

修改索引结构时应提升 `INDEX_VERSION`，考虑旧数据库迁移或安全重建。测试至少覆盖：

- 第二次刷新不读取未变化文件；
- 单文件修改只重建一个文件；
- 删除文件清理关联记录；
- 敏感路径和符号链接拒绝；
- 中文和标识符检索。

## 修改项目图谱

图谱本地表包括 `file_profiles`、`profile_fts`、`graph_nodes` 和 `graph_edges`。
文件档案必须绑定内容哈希；修改档案算法时提升 `PROFILE_VERSION`，修改图结构时
提升 `GRAPH_VERSION`。Neo4j 是默认请求后端，SQLite 是始终维护的查询缓存和故障
保底；查询功能不能依赖网络可用性，也不能向模型暴露任意 Cypher。

测试至少覆盖：未变化源码不重读、修改和删除同步、关系与影响分析、符号链接拒绝、
参数化 Neo4j 查询，以及 Neo4j 凭据不出现在查询文本或结果中。

## 修改工作流

`WorkflowOrchestrator` 负责：

- 确定 ReAct 或 Plan-and-Act；
- 创建共享 `IterationBudget`；
- 传递同一个原始用户需求；
- 执行和评审步骤；
- 收集结构化证据；
- 提前收尾和最终综合。

新增模型调用路径时必须：

1. 消耗同一个需求级预算；
2. 包含原始用户需求和当前范围；
3. 发送目的驱动提醒；
4. 使用正确角色的工具权限；
5. 能在预算耗尽时返回最终答复；
6. 添加跨角色预算和上下文测试。

不要在子 Agent 内递归创建新的 `WorkflowOrchestrator`。

## 上下文变更

固定跨需求上下文由 `ContextBuilder` 构建。单 Agent 消息和逐轮提醒由 `Agent.run`
构建。`ContextManager` 只删除完整旧工具交互块。

新增上下文来源时应明确：

- 来源是否可信；
- 自动召回条件；
- 最大条数和字符预算；
- 引用格式；
- 是否跨会话或跨工作区；
- 压缩时是否必须保留。

避免把整个项目、知识库或 Episode 默认发送给模型。

## Web API 和进度事件

FastAPI 路由位于 `webapp.create_app`。后台执行通过 `JobManager`，同一工作区锁确保
串行修改。

新增进度事件时：

- 使用稳定的 `event` 名称；
- 提供简短 `message`；
- 不包含隐藏思维链、工具参数、API Key 或完整敏感内容；
- 更新前端展示和 Web 测试；
- 注意每个 Job 只保留最近 200 条事件。

接口变更应同步更新 [Web API](web-api.md)。

## 提示词

提示词位于 `prompts.py`。工具调用时允许公开的模型说明必须使用：

```text
行动说明：<一句简短说明>
```

它不是思维链。不要要求模型输出内部逐步推理。Planner 和 Reviewer 的最终输出是
严格 JSON，修改 Schema 时必须同步更新解析器和测试。

## 提交前检查

- 完整测试通过；
- Python 和 JavaScript 语法检查通过；
- `git diff --check` 无错误；
- 新行为有边界测试；
- README 和 `docs/` 与默认配置一致；
- 没有提交 `.env`、`.simple-agent`、trace、构建产物或测试生成文件。
