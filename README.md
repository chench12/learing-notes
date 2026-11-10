# Learning Notes

学习笔记与配套代码实现。笔记写"为什么"，代码放 `examples/` 里能直接跑。

## 目录

| 序号 | 主题 | 笔记 | 代码 |
| :--- | :--- | :--- | :--- |
| 01 | 生成式优化（Self-Refine / FunSearch / AlphaEvolve / Frontier-Eng） | [notes.md](./01-python-basics/notes.md) | [examples/](./01-python-basics/examples/) |

新增主题时：复制 `_template/` 为 `NN-主题名/`，再在表格里补一行。

## 组织约定

一个主题一个目录，笔记和代码就近存放：

```
NN-主题名/
├── notes.md        # 概念 / 要点 / 踩坑 / 自测题
├── examples/       # 可直接运行的最小示例
└── exercise/       # 自己动手的实现（可选）
```

- 目录名：`两位序号-英文短横线`，如 `03-binary-search`
- 笔记内容用中文，文件名保持英文
- 每个示例要能独立运行，运行方式写在 `notes.md` 里
- 大段代码只放 `examples/`，笔记里不复制

## 笔记骨架

`_template/note.md` 提供的结构：

1. **一句话总结** —— 用自己的话复述
2. **核心概念** —— 定义 + 关键点
3. **关键细节** —— 表格化对比
4. **代码示例** —— 链接到 `examples/`
5. **踩坑记录** —— 最有价值的部分
6. **自测问题** —— 3 个检验掌握度的问题
7. **参考链接** —— 原始出处

## 本地使用

```bash
git clone https://github.com/<用户名>/learning-notes.git
cd learning-notes
python 01-python-basics/examples/hello.py
```

## License

[MIT](./LICENSE)
