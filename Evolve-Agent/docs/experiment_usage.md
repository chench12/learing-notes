# 实验记录与单次生成

已接入统一配置、实验内记录、固定骨架/补丁、结构化评测、独立复核和混合档案。当前已新增历史上下文、串行自动控制、统一预算、模型路由及可选附加指令进化，完整用法见 [agent_usage.md](agent_usage.md)。本页保留单次操作说明。本机已通过8项真实Docker验收；真实自动循环要求Docker，离线夹具允许本地演示。

P4 的数据库与文件职责、恢复命令和边界见 [storage_recovery.md](storage_recovery.md)。P5 的网格、父代选择、迁移、离线演示和代码阅读顺序见 [archive_selection.md](archive_selection.md)。存储保留事实，档案是可重建的活跃选择视图。

三种父代选择已经可切换：`archive.parent_sampling` 支持 `uniform_cell`、`softmax_cell`、`puct_cell`。匹配实验配置、离线机制对照和已运行实验的汇总命令见 [selection_comparison.md](selection_comparison.md)。这仅比较选择机制，不是完整 FunSearch 或 TTT-Discover 复现。

默认配置仍为 `execution.backend=local-reviewed`。Docker 准备、固定镜像配置和真实验收步骤见 [sandbox_setup.md](sandbox_setup.md)。没有 Docker 时，选择 Docker 后端会失败，不会自动改用本地执行。

## 离线检查

在项目根目录运行：

```powershell
python -X utf8 -m unittest discover -s tests -p test_experiment_flow.py -v
```

测试使用预设模型响应，真实运行小型手写候选；无网络请求，不需要密钥。测试目录自动清理。

要保留一份可以打开检查的种子评测，运行：

```powershell
python -X utf8 -m Examples.check_execution
```

该命令在dev_runs新建独立实验，不调用模型、不消耗生成次数；输出实验目录和本次评测目录。正常状态为 `ok`，种子半径和约为 `2.1666666666666665`。从输出路径打开 `artifacts/solution.json` 可见 26 个 `[x, y, radius]`；这只是初始网格，没有进行搜索优化。

骨架校验的直观示例：

```powershell
python -X utf8 -m Examples.check_blocks
```

运行全部离线测试（包含多块修改、骨架越界和标记解析）：

```powershell
python -X utf8 -m unittest discover -s tests -v
```

## 固定骨架和进化区域

每个任务初始程序必须完整可运行，并至少包含一组不缩进、独占一行的标记：

```python
# EVOLVE-BLOCK-START
# 此处可以放导入语句、多个函数或其他待进化代码。
# EVOLVE-BLOCK-END
```

圆装填种子有两块：标准库导入，以及 initialize()/optimize() 和辅助函数。固定 solve() 返回 optimize(initialize())。只限定文件末尾换行和 CRLF/LF 的差异，其他固定代码和标记原文必须保留，包括注释与空白。标记不允许嵌套。

构造候选后，每次都与实验快照中的初始骨架比较，而不是只与直接父代比较。语法错误记录为 syntax_error；语法正确但骨架改变记录为 skeleton_error。失败候选代码保留，不进入执行评测。

此约束检查修改范围，不保证块内程序行为安全；实际隔离需要选用并验证 Docker 等系统后端。字符串中的标记不作为真实边界。

添加进化标记及框架校验后，原实验不能直接用新框架继续。请新建实验，旧记录保留用于比较，不要修改历史清单或快照来绕过版本检查。

## 补丁与完整重写

配置中的 `search.generation_mode` 为 `patch`（默认）或 `rewrite`。必须在新建实验前设置；现有实验继续使用自己的清单。

补丁模式允许块外解释，修改必须采用下列格式：

```text
<<<<<<< SEARCH
        radius = 1 / 12
=======
        radius = 1 / 13
>>>>>>> REPLACE
```

这只是演示协议，缩小半径不代表改进。运行演示无需密钥，不调用模型或执行候选：

```powershell
python -X utf8 -m Examples.check_patches
```

实现约定：

- 搜索片段是完整代码行，包含原有缩进和空白；必须在当前工作副本中精确匹配一次。
- 多个补丁按顺序应用；后续搜索看到的是前面修改后的代码。
- 每次替换必须位于单个进化块内，搜索片段不能包含边界标记。新增代码通过替换已有片段完成。
- 允许空替换来删除代码，不允许空搜索或完全不修改的补丁。
- 应用前需能解析当前进化块；不要让前一补丁产生无法解析的标记/词法结构再指望后续修复。最终语法和骨架检查全部通过后才执行。
- 分隔符必须独占一行且不含多余空白，它们是保留的协议行。块外允许解释，块内不自动纠正缩进或模糊匹配。
- 任一补丁应用失败，不构造部分修改的候选，不修改父代，也不自动切换重写或发送修复请求。

`parse_error` 表示补丁格式错误，`patch_error` 表示匹配或范围检查失败；原始响应保存于 response.json，成功解析的补丁保存于 patches.json。所有补丁应用成功后才保存 candidate.py；即使其最终语法或骨架校验失败，也保留该代码供排查。

`rewrite` 则要求返回完整文件，并继续保护固定骨架。两种模式都不自动放宽运行或评测规则。

## 新建实验并生成一代

先在当前终端设置 `RELAY_API_KEY`，再运行：

```powershell
python -X utf8 -m Examples.generate_candidate --config configs/circle_packing.json
```

该命令会调用模型。程序先输出实验目录；每次不带 `--experiment` 都会创建独立实验，以配置指定的初始程序开始。旧 `runs` 中的成绩不会自动导入，旧记录保持原样。

## 在同一实验中再生成一代

把下面路径替换为上一次输出的实际目录：

```powershell
python -X utf8 -m Examples.generate_candidate --experiment experiments/exp_实际编号
```

继续实验使用其快照任务与清单配置，不读取后来修改的配置文件。若当前框架或已记录运行环境与实验不一致，会拒绝继续；可以恢复原环境，或创建新实验。

这表示在同一实验中发起一次新的生成，不表示重放中断请求。已保存响应使用独立的 resume_generation 命令继续；遗留状态使用 recover_experiment 检查/协调。未知服务端结果记为 uncertain，不自动重发。

## 记录结构

```text
experiments/exp_.../
  manifest.json
  records.sqlite3
  .objects/              # 已提交文件的内容寻址存储，不要删除
  .operation.lock        # OS 锁载体，不以是否存在判断活跃状态
  snapshot/
  archives/
    current.json          # 每岛活跃格、成员资格及排除原因
    migrations.json       # 从完成事件派生的迁移历史
  candidates/<候选编号>/
    candidate.py
    candidate.json
  generations/gen_.../
    plan.json
    state.json
    response.json
    patches.json          # 补丁模式且解析成功时
  evaluations/eval_.../
    evaluation.json
    artifacts/
      execution.json
      candidate.log
      solution.json
      evaluator.log          # 进入固定评测阶段才有
      evaluator_result.json  # 固定评测器正常返回才有
  evaluation_groups/group_.../
    plan.json
    summary.json
    best_solution_eval_....json  # 出现合法结果时，summary 指向实际最佳版本
```

- `plan.json`：模型配置、消息、父代及其本次评测编号；在请求前保存。
- `state.json`：生成阶段状态；请求失败、截断和语法错误同样有记录。
- `candidate.json`：候选源码、源码哈希、父代、所属实验与生成编号。
- `evaluation.json`：本次执行的候选编号、角色、时间、状态和 `result`。
- `artifacts/solution.json`：候选输出，合法和非法布局都保留。异常退出时也可能存在不完整或无效 JSON，应结合执行状态查看。
- `artifacts/candidate.log`、`evaluator.log`：各进程的标准输出与错误合并日志，上限由配置指定。
- `artifacts/execution.json`：源码/评测器哈希、运行限制、阶段耗时与退出码、Windows Job 内存峰值、输出文件大小/哈希，以及实际隔离能力。`evaluation.json.artifacts` 保存产物相对路径与哈希。

超过输出上限时仅保存 `solution.json.partial`，同时记录 `complete=false`，不进入评测。`complete=true` 仅表示文件完整复制，不能代替 JSON 解析或几何合法性检查。超时和运行失败也保存已有日志与输出；未执行的阶段不会伪造日志或结果。重复评测有各自的产物目录。

半径和位于 `result.score`（也可看 `result.metrics.sum_radii`）。`role=parent` 表示父代复测，`role=child` 表示新子代评测。候选编号可用于关联程序，每次复测产生新的评测编号，保留旧文件。

默认父代策略：`archive.strategy=island_grid`，每岛按布局特征分格，同格按质量选代表，再从本岛非空格均匀抽取父代。子代进入原计划指定岛屿；每完成 4 条生成链同时进行一次单跳环形迁移。`best()` 单独查询全局最佳。配置 `archive.strategy=greedy` 可在新实验中使用最高分对照并关闭迁移。

两种策略都使用每个候选最近一次已完成的 `purpose=search`、`stage=standard` 评测，最近按事务提交顺序判断。不是对全部历史尝试取最大值，也不是最终重复复核统计。最近已完成搜索结果非法/超时/错误时不参与选择，运行中结果不抢占旧结果；独立复核不会覆盖搜索成绩。完整规则及自定设计说明见 archive_selection.md。

`max_generations` 限制本实验的生成计划数量，失败和未完成尝试也占用次数。单次脚本仍只生成一代；串行多轮使用 run_search，异步配置使用 run_async_search（见 async_pipeline.md），均受调用/token/评测时间/累计墙钟及可选参考费用预算约束。同一实验目录不支持并发CLI。

## 结构化评测：目标、约束与布局特征

`Evaluation` 保留 `score`、`metrics`、`feedback`，新增 `objectives`、`constraints`、`features` 和 `stage`。圆装填仍只有一个优化目标，多个字典不代表多目标选择已经实现。

| 字段 | 当前含义 | 是否决定合法得分 |
|---|---|---|
| score / objectives.sum_radii | 26 圆的半径和 | 合法时唯一优化目标 |
| constraints.boundary | 最大边界违反量减 1e-9 | 必须 <= 0 |
| constraints.non_overlap | 最大重叠量减 1e-9 | 必须 <= 0 |
| metrics.max_violation | 扣除容差前的最大违反量 | 诊断量 |
| features.radius_cv | 半径总体标准差 / 平均半径 | 不加分，用于档案分格 |
| features.boundary_fraction | 距至少一条边不超过 1e-6 的圆所占比例 | 不加分，用于档案分格 |
| stage | 当前固定为 standard | 防止未来不同预算混排 |

几何容差 1e-9 与边界接触阈值 1e-6 分别定义。特征描述的是输出布局，不识别 Adam、L-BFGS 等程序类别。结构错误、运行失败或几何非法的结果不提供有效 score/objectives/features；非法布局可以保留约束诊断。所有数值必须有限，bool 不当作数值接受。任务专有的几何计算仍全部位于任务包。

## 重复复核一个程序

先用已审查的种子验证流程，不需要密钥，不请求模型：

```powershell
python -X utf8 -m Examples.repeat_evaluation --repeats 5
```

这会新建实验、保存种子，并按同一标准预算串行运行 5 次，输出各次状态和汇总路径。初始程序是确定性网格，预期合法率 1，平均/中位/最高分均约 2.1666666666666665，样本标准差 0；这只验证统计流程，不能据此证明搜索算法稳定。Docker 配置准备好后可以增加 `--config configs/circle_packing.docker.json`。

对当前框架创建的已有实验，指定实际候选编号：

```powershell
python -X utf8 -m Examples.repeat_evaluation --experiment experiments/exp_实际编号 --candidate 实际候选编号 --repeats 5
```

不指定候选编号时使用当前搜索父代，尚无搜索记录时使用已保存种子。命令要求实验身份/环境/框架匹配，不会把旧格式成绩搬进新实验。每次复核重新执行完整 solve()，候选时限和执行限制不变，不以缩短预算模拟重复评测。

每组先保存 plan.json，固定候选/源码哈希、次数、预算、执行配置和聚合规则。每次运行仍有独立 evaluation.json 和 artifacts，通过 evaluation_group_id/repeat_index 关联。每次保存最新 summary.json；最佳合法布局单独保存并带原始评测编号与哈希。后续更高分写新文件，不覆盖旧 summary 指向的布局。

汇总规则 `all-valid-median-v1` 是本项目的明确设计，不是论文公开超参数：

- `valid_rate`：组全部执行完成后的合法次数 / 计划次数。中断组为 null，并另报已完成次数与 `observed_valid_rate`。
- `conditional_score_stats`：只对合法样本报告均值、中位数、最小/最大值、样本标准差。失败次数同时保留；不把失败当作 0 分，也不省略合法率。
- `all_valid_median`：仅组正常完成且所有运行均合法时提供中位分数，其他情况为 null。目前是报告值，不参与搜索选择。
- `sample_stdev`：至少两个合法样本才定义；没有合法样本时所有分数统计为 null。极端数值使统计量无法用有限浮点数表示时也记 null，并在 `unrepresentable_statistics` 说明。
- `best_evaluation_attempt_id` / `best_solution`：本组最高合法分对应的实际布局与验证记录；它代表最佳观测布局，不是算法通常能达到的质量。

`purpose=confirmation` 表示复核用途；`stage=standard` 表示物理预算仍是标准阶段。复核不调用模型、不占生成次数，但真实评测时间计入统一预算。控制器可配置对超过本次调用前全局最佳的子代自动复核；不影响搜索档案选择。

异常或 Ctrl+C 时保留已有结果，summary 标为 failed/interrupted，不虚构剩余结果、不自动重跑。强制结束可能留下 running 状态，可用 P4 的 recover_experiment --apply 从已提交评测重建；不会补跑缺失次数。当前 solve() 没有随机种子参数，重复运行不保证统计独立或完全可复现；确定性种子得到零波动尤其不能代替随机优化器的复核。

## 本地执行限制与当前边界

默认 `execution` 为 `backend=local-reviewed`、`docker_image=null`、`startup_timeout_seconds=30.0`。资源默认值：Job 总提交内存 512 MiB、最多 8 个活动进程（包括根进程）、每阶段日志 1,000,000 字节、输出 5,000,000 字节、临时工作目录 10,000,000 字节、固定评测器独立限时 5 秒。候选仍使用 `task.timeout_seconds=3.0`；本地后端计入进程启动及监控开销，不是仅求解函数的时间。startup_timeout_seconds 专用于 Docker 的创建/握手阶段。

候选和评测器都用当前 Python 的 `-I` 模式启动，环境变量使用白名单，不直接继承 `RELAY_API_KEY` 等调用端凭据。Windows Job Object 在加载候选前关联进程，限制总内存和进程数；成功、失败或超时都清理同一 Job 的后代进程。关联失败则不加载候选。内存分配失败可能表现为非零退出码和 `MemoryError`，不凭退出码推断精确原因。

日志通过管道有界保存；输出和工作目录大小定期采样，超过限额会终止该阶段。后两者不是硬磁盘配额，短时磁盘写入仍可能超过设定值。POSIX 回退只提供进程组清理，没有实现内存/进程数配额；元数据会如实标为未生效。当前测试覆盖 Windows 本机行为。

**本地后端不是文件系统/网络安全沙箱。** 候选以当前用户权限运行，仍可能读取宿主文件、访问网络或篡改评测文件；环境白名单只阻止直接继承变量。`local-reviewed` 适用于人工审查后的候选。骨架检查也不能阻止进化块执行任意 Python 行为。已提供不挂载宿主目录、禁用网络的 Docker 后端和独立验收脚本；当前机器没有可用运行时，尚未通过真实容器测试。重复复核命令可先用于已审查种子，不据此启用无人值守大批量搜索。

本次框架/清单版本更新后，请创建新实验；旧实验、旧 `runs` 和快照均保留，不修改它们以绕过版本检查。此阶段只运行离线测试和种子检查，真实生成由你手动启动。
