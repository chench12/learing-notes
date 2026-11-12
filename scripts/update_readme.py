"""将学习笔记同步到 GitHub 仓库首页：python scripts/update_readme.py。"""

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


def build_readme(source: str) -> str:
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

[开始阅读](#生成式优化笔记) · [原始笔记](./study-notes/notes.md) · [参考论文](#参考链接)

</div>

---

## 阅读导航

全文就在本页，点击主题可跳到对应章节。

| 序号 | 主题 | 阅读要点 |
| :--- | :--- | :--- |
"""
    footer = """

---

[回到阅读导航](#阅读导航)

<details>
<summary>笔记维护与仓库说明</summary>

正文维护在 [study-notes/notes.md](./study-notes/notes.md)，插图放在同目录的 `images/` 中。
修改笔记后，在仓库根目录运行以下命令，将最新正文同步到首页：

```powershell
python scripts/update_readme.py
```

提交时同时包含 `study-notes/notes.md`、`README.md` 和新增的图片。

本仓库采用 [MIT License](./LICENSE)。

</details>
"""
    return header + "\n".join(rows) + "\n\n---\n\n" + body + footer


if __name__ == "__main__":
    TARGET.write_text(build_readme(SOURCE.read_text(encoding="utf-8")), encoding="utf-8")
    print("已同步 README.md：首页包含完整笔记、章节导航和图片。")
