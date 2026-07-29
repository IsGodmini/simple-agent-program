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

也可以指定其他项目目录：

```bash
simple-agent --workspace /path/to/project "分析项目结构"
```

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
```

Agent 会使用保守的 UTF-8 长度估算请求 Token。在达到压缩阈值后，只会
淘汰完整的旧工具交互块，保留最初的系统消息、用户任务和最新工具结果。
如果安全压缩后仍超过输入上限，请求会在本地终止。

## 跨需求记忆

每次 CLI 调用被视为一个独立需求。需求内部的模型消息、工具调用和工具结果
构成临时工作上下文；需求结束后，只把紧凑摘要自动提供给后续需求，原始过程
不会自动进入新上下文。

本地记忆结构：

```text
.simple-agent/
├── memory/
│   └── task_summaries.json
└── episodes/
    └── <task-id>.json
```

`task_summaries.json` 记录需求、结果、修改文件和验证命令。Episode 保存完整
消息和工具执行过程，只能通过 `search_memory` / `read_episode` 按需访问。
`.simple-agent` 已被 Git 和普通文件工具忽略。

记忆可能落后于当前代码，因此 Agent 会把当前文件和测试结果作为最终事实来源。

## 测试

```bash
python -m unittest discover -s tests -v
```
