# 实验目录与快速入口

2026-11-12整理：experiments保留2次真实模型运行；22个历史开发目录完整迁入dev_runs，3385个文件逐一核对哈希一致。迁移记录在dev_runs/relocation_20261112.json。

```text
experiments/          真实模型生成实验（包括失败尝试）
  README.md          实验索引
  exp_.../
    README.md        人可直接阅读的摘要与链接
    summary.json     结构化摘要
    best.py          当前搜索最佳程序的副本
    best_solution.json 对应搜索评测实际输出的布局副本
    ...              完整的原始记录
dev_runs/            开发演示、手写夹具、种子检查、Docker验收
```

摘要文件存在于实验根目录，不搬动原始子目录。新建真实实验的命令保持不变。后续运行搜索、生成、恢复生成、重复复核或批处理时，在操作结束或失败后刷新摘要；不发送额外模型请求、不重跑候选。脚本初始化失败或进程被强制终止时，摘要可能尚未更新，可以手动重新导出。

```powershell
python -X utf8 -m Examples.summarize_experiment --experiment experiments/exp_实际编号
```

摘要工具只读取schema 9的已提交记录，在实验锁内导出；不依赖创建实验时的Python环境匹配。它会检查清单指纹、存储身份和原始文件完整性，不能用于修复损坏记录。继续搜索/评测仍由ExperimentSession执行完整环境和框架检查。

## 最佳结果的含义

best.py和best_solution.json来自同一个候选与同一次搜索评测。选择规则与搜索的best()一致：每个候选取最近一次已完成的standard/search评测，再从合法结果中选最高分，同分按候选ID排序。源码、布局和原始评测路径/哈希写入summary.json。

复核分数独立列在confirmations中，不挑出复核中最幸运的一次覆盖搜索成绩。后续搜索复测导致候选不再合法时，不保留过期的最佳副本。没有合法搜索结果时，不生成best.py和best_solution.json。

summary.json分别记录已知Token、用量未知的调用ID及其保守预算预留。未知调用不能当作免费调用；金额以平台账单为准。source_event_sequence指出摘要对应的事件位置。摘要是某次导出的视图，不是持续刷新的监控页面。

## 原始数据仍在哪里

| 位置 | 用途 |
|---|---|
| manifest.json / snapshot | 冻结配置、环境、框架与任务 |
| records.sqlite3 / .objects | 结构化记录、修订事件和原始内容 |
| candidates | 各候选源码、父子关系 |
| generations | 采样计划、完整提示词、响应、补丁和生成状态 |
| evaluations | 成绩、布局、候选/评测器日志和执行信息 |
| evaluation_groups | 重复复核的各次评测与统计 |
| archives | 岛屿/特征格视图及迁移记录 |
| calls / controls | 模型调用账本、预算和搜索控制状态 |
| meta_prompts | 可选的附加指令进化记录 |

摘要是可重新生成的副本，不参与父代选择和事务恢复。不要手动修改原始文件或只保留摘要；备份时应在实验停止后复制整个目录。CLI之外直接调用底层Python接口不会自动刷新摘要。

这次只调整Examples运行入口、展示文件及目录位置，没有修改Evolve核心源码、旧manifest、SQLite记录或快照，当前真实实验可以沿用原命令继续。

验证：新增6项测试覆盖移动后打开实验、离线CLI分流、摘要与原结果对应、复核分离、过期最佳结果清理、未知调用预算及失败时导出。完整回归196项：188项通过，8项Docker集成测试因本轮未设置测试镜像而跳过，耗时125.530秒。本次没有请求模型。Docker真实隔离验收沿用此前单独运行的8项全部通过记录。
