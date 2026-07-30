from tests import _TEST_STORAGE_HOME  # noqa: F401

import json
import shutil
import subprocess
import unittest
from pathlib import Path


class MarkdownRendererTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_renders_common_markdown_and_escapes_unsafe_html(self):
        renderer = (
            Path(__file__).parents[1]
            / "src"
            / "simple_agent"
            / "web"
            / "markdown.js"
        )
        markdown = """# 标题

**加粗**与`代码`

| 名称 | 状态 |
| --- | --- |
| 测试 | 通过 |

```python
print("<safe>")
```

[安全链接](https://example.com)
[危险链接](javascript:alert(1))
<script>alert(1)</script>
"""
        script = (
            "const md=require(process.argv[1]);"
            "process.stdout.write(md.render(JSON.parse(process.argv[2])));"
        )
        completed = subprocess.run(
            ["node", "-e", script, str(renderer), json.dumps(markdown)],
            check=True,
            capture_output=True,
            text=True,
        )
        html = completed.stdout

        self.assertIn("<h1>标题</h1>", html)
        self.assertIn("<strong>加粗</strong>", html)
        self.assertIn("<table>", html)
        self.assertIn('class="language-python"', html)
        self.assertIn('href="https://example.com"', html)
        self.assertIn('href="#"', html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
