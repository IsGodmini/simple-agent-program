"use strict";

(function exposeMarkdown(root) {
  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function safeUrl(value) {
    const url = String(value).trim();
    if (/^(https?:|mailto:)/i.test(url) || /^(\/|#)/.test(url)) {
      return escapeHtml(url);
    }
    return "#";
  }

  function inlineMarkdown(value) {
    const tokens = [];
    const keep = (html) => {
      const key = `\u0000MD${tokens.length}\u0000`;
      tokens.push(html);
      return key;
    };
    let text = String(value).replace(/\u0000/g, "");

    text = text.replace(/`([^`\n]+)`/g, (_, code) =>
      keep(`<code>${escapeHtml(code)}</code>`));
    text = text.replace(
      /!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,
      (_, alt, url) => keep(
        `<img src="${safeUrl(url)}" alt="${escapeHtml(alt)}" loading="lazy">`,
      ),
    );
    text = text.replace(
      /\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,
      (_, label, url) => keep(
        `<a href="${safeUrl(url)}" target="_blank" rel="noopener noreferrer">` +
        `${escapeHtml(label)}</a>`,
      ),
    );
    text = escapeHtml(text)
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
      .replace(/(^|[^\w])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/(^|[^\w])_([^_\n]+)_/g, "$1<em>$2</em>");

    tokens.forEach((html, index) => {
      text = text.replace(`\u0000MD${index}\u0000`, html);
    });
    return text;
  }

  function splitTableRow(line) {
    return line
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((cell) => cell.trim());
  }

  function isTableDivider(line) {
    const cells = splitTableRow(line);
    return cells.length > 0 && cells.every(
      (cell) => /^:?-{3,}:?$/.test(cell),
    );
  }

  function renderMarkdown(source) {
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    const output = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = line.match(/^\s*```([\w.+-]*)\s*$/);
      if (fence) {
        const language = fence[1] || "text";
        const code = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        output.push(
          `<pre data-language="${escapeHtml(language)}"><code class="language-${escapeHtml(language)}">` +
          `${escapeHtml(code.join("\n"))}</code></pre>`,
        );
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        const level = heading[1].length;
        output.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        output.push("<hr>");
        index += 1;
        continue;
      }

      if (
        index + 1 < lines.length &&
        line.includes("|") &&
        isTableDivider(lines[index + 1])
      ) {
        const headers = splitTableRow(line);
        const rows = [];
        index += 2;
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          rows.push(splitTableRow(lines[index]));
          index += 1;
        }
        output.push(
          "<div class=\"table-wrap\"><table><thead><tr>" +
          headers.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join("") +
          "</tr></thead><tbody>" +
          rows.map((row) => "<tr>" + headers.map(
            (_, cellIndex) => `<td>${inlineMarkdown(row[cellIndex] || "")}</td>`,
          ).join("") + "</tr>").join("") +
          "</tbody></table></div>",
        );
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        const quote = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          quote.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        output.push(`<blockquote>${renderMarkdown(quote.join("\n"))}</blockquote>`);
        continue;
      }

      const listMatch = line.match(/^(\s*)([-+*]|\d+[.)])\s+(.+)$/);
      if (listMatch) {
        const ordered = /^\d/.test(listMatch[2]);
        const tag = ordered ? "ol" : "ul";
        const items = [];
        while (index < lines.length) {
          const item = lines[index].match(/^(\s*)([-+*]|\d+[.)])\s+(.+)$/);
          if (!item || /^\d/.test(item[2]) !== ordered) break;
          let content = item[3];
          const checked = content.match(/^\[([ xX])\]\s+(.+)$/);
          if (checked) {
            const marker = checked[1].toLowerCase() === "x" ? " checked" : "";
            content = `<input type="checkbox" disabled${marker}> ${inlineMarkdown(checked[2])}`;
          } else {
            content = inlineMarkdown(content);
          }
          items.push(`<li>${content}</li>`);
          index += 1;
        }
        output.push(`<${tag}>${items.join("")}</${tag}>`);
        continue;
      }

      const paragraph = [line.trim()];
      index += 1;
      while (
        index < lines.length &&
        lines[index].trim() &&
        !/^\s*```/.test(lines[index]) &&
        !/^(#{1,6})\s+/.test(lines[index]) &&
        !/^(\s*)([-+*]|\d+[.)])\s+/.test(lines[index]) &&
        !/^\s*>\s?/.test(lines[index]) &&
        !(index + 1 < lines.length && lines[index].includes("|") &&
          isTableDivider(lines[index + 1]))
      ) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      output.push(`<p>${paragraph.map(inlineMarkdown).join("<br>")}</p>`);
    }
    return output.join("");
  }

  const api = { render: renderMarkdown, escapeHtml };
  root.SimpleMarkdown = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
