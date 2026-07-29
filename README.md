# Simple Agent Program

一个使用 OpenAI 兼容接口和工具调用能力的最小开发 Agent。

当前第一阶段支持：

- 多轮 Chat Completions 调用
- 标准 `tools` / `tool_calls` 工具协议
- 列出项目文件
- 读取项目内的 UTF-8 文本文件
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

## 测试

```bash
python -m unittest discover -s tests -v
```
