# SQLite 记录与中断恢复（P4）

P4 把记录事实来源从目录扫描改为 SQLite，保存候选、生成计划/响应/状态、评测、复核组、文件哈希及修订事件。后续 P5 已接入岛屿/特征网格档案；选择规则与物化视图恢复见 [archive_selection.md](archive_selection.md)，本页重点说明底层记录。

2026-11-12目录整理：真实模型实验保留在experiments，开发检查与离线演示进入dev_runs。实验根目录新增README.md、summary.json、best.py和best_solution.json作为阅读入口，原始记录及恢复规则保留。详见 [实验目录说明](experiment_layout.md)。

## 先运行离线演示

```powershell
python -X utf8 -m Examples.check_recovery
```

不需要 API Key，不连接模型服务。演示使用预设补丁，模拟“响应已提交、子代尚未构造”时中断；重新打开实验后继续解析、校验和评测。再次恢复同一生成编号，数据库统计保持不变，不新增候选或评测。

预期：预设响应只读取 1 次，真实模型请求为 0；数据库有 2 个候选、1 次生成、2 次评测（父代和子代）。子代半径和为 2.0，低于种子；缩小半径只是便于验证恢复路径，不是一次优化成果。

## 哪些文件负责什么

```text
experiments/exp_.../
  manifest.json
  records.sqlite3          # 当前记录与事件的事实来源
  .operation.lock          # OS 锁载体，文件存在不表示进程仍存活
  .objects/ab/<sha256>     # 内容寻址文件，保留已提交的当前/历史版本
  snapshot/
  candidates/
  generations/
  evaluations/
  evaluation_groups/
  archives/                # P5 可重建视图及迁移历史
```

SQLite 的 records 表按 kind 区分 candidate/generation/evaluation/group/archive，保存当前修订；events 表保留每次修订的状态、内容、文件哈希与顺序。files 表保存可查看文件的路径、大小、哈希与是否待导出，identity 表关联实验清单指纹。大文件内容位于 .objects，SQLite 保存引用；查看用的 candidate.py、JSON 和日志仍导出到原来的目录。

一次保存的顺序：

1. 将本次内容写入 .objects，计算哈希，刷新文件。
2. 在单个 SQLite 事务中提交记录、事件与待导出清单。
3. 原子替换查看用文件，再标记导出完成。

第二步之后中断，下次打开实验会补齐待导出项。第二步之前中断，未提交内容不会成为候选、成绩或生成计划；孤立对象保留，不猜测归属。未注册目录在恢复报告中列为 orphan_directories，不导入、不删除。

已完成导出的文件被修改或丢失时会拒绝继续，不把外部编辑当成新事实。数据库缺失也不会从 JSON 静默重建。请保留整个实验目录，包括 .objects；停止实验操作后再完整备份，不能只复制 SQLite 或只留查看用 JSON。当前验证覆盖进程异常退出和中断，不保证硬件损坏、文件系统丢失或任意断电情形都可恢复。

## 请求前冻结计划

generation 的 plan.json 在请求前提交，包含父代及源码哈希、选择前候选池/档案快照、父代复测编号、模型路由、完整消息、模板ID、历史参考ID与取舍、随机seed和预分配的子代/评测/call编号。统一calls账本先预留再请求，响应已提交但生成记录未保存时可从call响应恢复。

子代源码在恢复后重新从原父代和原响应构造；使用计划里的同一候选编号。已经保存的同一子代幂等复用，不产生新的谱系节点。若原评测已经完成，也复用其结果。search_seed 参与 P5 的确定性父代采样，但不能保证模型或 solve() 的随机性可复现。

| 状态 | 含义 | 恢复行为 |
|---|---|---|
| planned | 计划已提交，尚未进入请求阶段 | 不自动发送 |
| requesting | 进入请求阶段，未提交响应 | 遗留时标记 uncertain |
| uncertain | 服务端是否完成/计费未知 | 不重发，不编造响应或用量 |
| api_error | 收到明确的 HTTP 拒绝（保守排除 408/429） | 保留失败，不自动重发 |
| response_received | 响应已提交 | 可从本地继续解析 |
| candidate_ready | 子代已通过语法与骨架检查 | 可继续原计划评测 |
| evaluating | 子代评测已计划/执行 | 协调已提交或遗留执行结果 |
| evaluation_interrupted | 子代评测没有完整结果 | 显式选择新评测编号后才重跑 |
| completed | 本次生成链结束，可能得到合法或失败子代 | 返回原结果，不再执行 |

incomplete/parse_error/patch_error/syntax_error/skeleton_error 仍是明确终态；恢复不会悄悄改协议、修补输出或请求新响应。

requesting 在外部调用前提交，因此极窄的“状态已提交但尚未发送”窗口也可能被保守标为 uncertain。相反，服务端已返回但响应尚未持久化时退出，也无法恢复内存里那份内容。本项目没有提供方的端到端幂等保证，不能证明没有产生账单；能保证的是恢复工具不重发模型请求。所有计划仍占用生成次数。

## 检查和协调遗留状态

把路径替换为当前框架创建的实验：

```powershell
python -X utf8 -m Examples.recover_experiment --experiment experiments/exp_实际编号
```

查看各生成的 state/action、遗留评测/复核组、数据库统计和孤立目录。打开实验时会自动补齐已提交但尚未导出的文件；不带 --apply 不推进业务状态。

```powershell
python -X utf8 -m Examples.recover_experiment --experiment experiments/exp_实际编号 --apply
```

这条命令协调状态，不调用模型、不执行候选：

- 遗留 requesting 且没有响应，记 uncertain。
- 遗留 running 评测若存在完整 runner 结果，核对候选、评测器、预算、输出哈希与阶段后采纳；没有可信完整结果时记 interrupted，不给分。
- 评测已提交而生成状态尚未结束，补上 completed。
- 从已提交的复核评测重建遗留组的统计与最佳布局；剩余次数不自动补跑。
- 从事实事件重建 P5 档案，补齐档案缓存和派生迁移历史，不额外推进迁移轮次。

重复协调已经完成的状态不会新增事件。这里的 runner 文件恢复是执行记录核对，不是再运行一次独立几何验证；原始结果和恢复来源都会保留。默认本地后端仍缺系统隔离，不能把文件哈希视作抵御恶意宿主访问的安全边界。

## 继续已保存的响应

```powershell
python -X utf8 -m Examples.resume_generation --experiment experiments/exp_实际编号 --generation gen_实际编号
```

该入口不构造模型客户端、不读取 API Key。它可能解析并执行尚未评测的子代；已有完整结果则直接返回。没有持久化响应时拒绝继续。查看生成记录的 response.json 和 candidate.py 后，再运行本地候选；Docker 后端的实际验收仍按 sandbox_setup.md 进行。

若旧子代评测已标为 failed/interrupted，明确新建一次评测：

```powershell
python -X utf8 -m Examples.resume_generation --experiment experiments/exp_实际编号 --generation gen_实际编号 --reevaluate-interrupted
```

旧评测不覆盖，新评测编号写入生成状态，previous_evaluation_attempt_ids 保留原编号。running 状态必须先协调，不能加标志绕过。不管是否重新评测，都不会重新请求模型。

## 使用边界

每个实验使用 OS 文件锁，覆盖生成、评测、复核及恢复；进程退出后锁自动释放。不要删除锁文件解除正在运行的操作，也不要在同一个实验并发开多个 CLI。同实验异步入口也由单一协调器持有整段OS锁，只有协调器写库；线程不写记录。支持的本机在途任务与预算见 async_pipeline.md。跨机租约尚未实现，当前锁不是分布式调度器。

恢复工具不会自动清理 Docker/WSL 崩溃后可能遗留的容器或宿主子进程；运行器正常异常处理仍会清理本次进程树，强制终止宿主时需检查运行后端。本机Docker现已通过8项真实执行验收；这不代表验证了Docker/WSL崩溃后的自动回收。

SQLite当前采用同步事务，打开实验会核对已导出文件。大实验的增量查询/压缩/垃圾回收未完成。统一calls预算已覆盖程序与元指令，统计按唯一call_id去重，不能把修订事件中的重复响应累加。controls记录控制器跨启动时间；未知调用阻止自动续跑，原始响应可恢复后继续解析。更多边界见agent_usage.md。

当前清单版本为9；旧版本实验保持原样，不自动迁移，不修改清单绕过框架版本检查。这是本项目的工程实现选择，不是 AlphaEvolve 的官方数据库源码。
