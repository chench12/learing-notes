"""统一公式兼容写法并同步 GitHub 首页：python scripts/update_readme.py。"""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "study-notes" / "notes.md"
TARGET = ROOT / "README.md"

GUIDES = {
    "Self-Refine": "生成 → 反馈 → 改进：同一个模型如何反复完善输出？",
    "FunSearch": "程序搜索 + 自动评估：如何从候选程序中发现新解？",
    "AlphaEvolve": "演化式编码智能体：如何协同提示、模型与评估器？",
    "Test-time training to discover": "搜索 + 参数学习：如何用当前问题的奖励更新模型？",
    "Frontier-Eng": "工程优化评测：如何衡量预算内的持续改进？",
}


INLINE_TOKEN = re.compile(
    r"(?<![\\$])\$(?!\$)(?:`(?P<quoted>[^`\n]+)`|(?P<plain>[^$\n]+?))\$(?!\$)"
    r"|(?P<ticks>`+).*?(?P=ticks)"
)


def normalize_math(source: str) -> str:
    """只处理公式，保留普通代码、Mermaid 和正文。"""
    lines = []
    fence = None
    display = False
    for line in source.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            char, length, language = fence
            if marker and marker[1][0] == char and len(marker[1]) >= length and not marker[2].strip():
                fence = None
            elif language == "math":
                line = line.replace(r"\operatorname", r"\mathrm")
            lines.append(line)
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]), marker[2].strip())
        elif line.strip() == "$$":
            display = not display
        elif display:
            line = line.replace(r"\operatorname", r"\mathrm")
        else:
            def replace_inline(match):
                if match["ticks"]:
                    return match[0]
                formula = match["quoted"] if match["quoted"] is not None else match["plain"]
                formula = formula.strip().replace(r"\operatorname", r"\mathrm")
                before = " " if match.start() and not line[match.start() - 1].isspace() else ""
                after = " " if match.end() < len(line) and not line[match.end()].isspace() else ""
                return before + "$`" + formula + "`$" + after

            line = INLINE_TOKEN.sub(replace_inline, line)
        lines.append(line)
    if display:
        raise ValueError("块级公式缺少配对的 $$，请修正后再同步。")
    return "".join(lines)


def build_readme(source: str) -> str:
    source = normalize_math(source)
    topics = [
        title
        for title in re.findall(r"^## (.+)$", source, re.MULTILINE)
        if title != "参考链接"
    ]
    rows = []
    anchors = {}
    for number, title in enumerate(topics, start=1):
        # 与当前 GitHub Markdown 标题锚点对应，并处理同名章节。
        base = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = anchors.get(base, 0)
        anchors[base] = count + 1
        anchor = f"{base}-{count}" if count else base
        guide = GUIDES.get(title, "阅读本节笔记")
        rows.append(f"| {number:02d} | **[{title}](#{anchor})** | {guide} |")

    body = re.sub(r"^(#{1,5}) ", r"\1# ", source.strip(), flags=re.MULTILINE)
    # README 位于根目录，图片链接需要相对于根目录解析。
    body = body.replace("](./images/", "](./study-notes/images/")

    header = """<!-- 由 scripts/update_readme.py 从 study-notes/notes.md 生成。 -->

<div align="center">

# Learning Notes

**论文阅读 · 生成式优化 · 持续学习**

从自我反馈到程序搜索、测试时训练，记录 AI 如何持续改进候选解。

[开始阅读](#生成式优化笔记) · [代码项目](#代码项目) · [原始笔记](./study-notes/notes.md) · [参考论文](#参考链接)

</div>

---

## 代码项目

### [Evolve-Agent](./Evolve-Agent/README.md)

依据 AlphaEvolve 公开方法重建的代码进化实验框架，支持任务替换、自动评估与多种父代选择策略。

[项目说明](./Evolve-Agent/README.md) · [源代码](./Evolve-Agent/Evolve/) · [运行示例](./Evolve-Agent/Examples/) · [使用指南](./Evolve-Agent/docs/agent_usage.md)

---

## 阅读导航

全文就在本页，点击主题可跳到对应章节。

| 序号 | 主题 | 阅读要点 |
| :--- | :--- | :--- |
"""
    title = re.search(r"^# (.+)$", source, re.MULTILINE)
    if title:
        anchor = re.sub(r"[^\w\- ]", "", title[1].lower()).replace(" ", "-")
        header = header.replace("#生成式优化笔记", "#" + anchor)

    footer = r"""

---

[回到阅读导航](#阅读导航)

<details>
<summary>笔记维护与仓库说明</summary>

正文维护在 [study-notes/notes.md](./study-notes/notes.md)，插图放在同目录的 `images/` 中。
修改笔记后，在仓库根目录运行以下命令，将最新正文同步到首页：

```powershell
python scripts/update_readme.py
```

同步时会统一行内公式为 GitHub 支持的美元符号配合反引号的写法，
并将公式中的 `\operatorname` 替换为兼容的 `\mathrm`；普通代码与 Mermaid 不受影响。

提交时同时包含 `study-notes/notes.md`、`README.md` 和新增的图片。

本仓库采用 [MIT License](./LICENSE)。

</details>
"""
    return header + "\n".join(rows) + "\n\n---\n\n" + body + footer


if __name__ == "__main__":
    original = SOURCE.read_text(encoding="utf-8")
    normalized = normalize_math(original)
    readme = build_readme(normalized)
    if normalized != original:
        SOURCE.write_text(normalized, encoding="utf-8")
    TARGET.write_text(readme, encoding="utf-8")
    print("已统一公式写法，并同步 README.md。")
