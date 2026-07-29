# Simple Agent Program

一个使用 OpenAI 兼容接口和工具调用能力的最小开发 Agent。

当前支持：

- 多轮 Chat Completions 调用
- 标准 `tools` / `tool_calls` 工具协议
- 列出项目文件
- 读取项目内的 UTF-8 文本文件
- 创建新文件或精确替换已有文本
- 运行受控的测试、构建和静态检查命令
- 按需将完整执行过程保存为 JSON
- 请求前估算上下文 Token，并在 80K 时安全压缩旧工具交互
- 将输入限制在 96K、输出限制在 16K，总预算为 128K
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

## 工具

- `list_files`：列出项目结构
- `read_file`：读取带行号的 UTF-8 文本
- `apply_patch`：创建文件，或通过唯一的 `old_text` 精确替换文本
- `run_command`：运行允许列表内的命令，不经过 Shell

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

## 测试

```bash
python -m unittest discover -s tests -v
```
