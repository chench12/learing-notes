# Evolve-Agent

本项目作为学习笔记仓库中的 `Evolve-Agent/` 目录维护。[返回仓库首页](../README.md) · [更新与使用说明](docs/github_setup.md)

任务可替换、模型参数冻结的代码进化实验框架。依据 AlphaEvolve 公开方法重建，并支持均匀抽格、FunSearch启发的温度采样、TTT-Discover启发的PUCT三种父代选择；不是这些系统的完整官方实现。

Python 3.12，远程模型接口依赖 openai SDK；离线手写演示无需API密钥或GPU。不要把API key写进配置或提交到记录。

真实模型实验位于本机experiments目录，开发演示与验收位于dev_runs目录，这些运行产物默认不上传Git。每个真实实验优先看 README.md、summary.json、best.py、best_solution.json，搜索和复核结束后自动更新。目录及摘要含义见 [实验目录说明](docs/experiment_layout.md)。

首次使用：Python3.12，运行 python -m pip install -r requirements.txt。上传GitHub及克隆后的使用步骤见 [GitHub说明](docs/github_setup.md)。

100步真实搜索配置：configs/circle_packing.docker.100.json；启动、预算和恢复方法见 [100步实验说明](docs/run_100_steps.md)。

```powershell
python -X utf8 -m unittest discover -s tests -q
python -X utf8 -m Examples.run_search --config configs/circle_packing.offline.json --offline-demo
python -X utf8 -m Examples.check_bin_packing
```

演示使用人工响应，执行/评测/入库真实运行，不能当成模型研究结果。真实模型单次调用：Examples.generate_candidate；自动循环要求Docker配置；本机已通过8项真实容器验收，可使用 configs/circle_packing.docker.json 开始小预算模型预实验，详见本机Docker指南。未知模型调用不自动重发，程序/元指令请求统一记账。

- [智能体完整用法与当前边界](docs/agent_usage.md)
- [单实验异步流水线与运行方法](docs/async_pipeline.md)
- [异步首版交付与验证产物](docs/delivery_async_20261112.md)
- [此前串行交付与三种策略接入说明](docs/delivery_20261112.md)
- [论文对应、阶段状态及研究路线](docs/alphaevolve_roadmap.md)
- [三种父代选择的对照](docs/selection_comparison.md)
- [档案与迁移](docs/archive_selection.md)
- [事务记录与恢复](docs/storage_recovery.md)
- [Docker准备及真实隔离验收](docs/sandbox_setup.md)
- [这台Windows电脑的D盘启动方法与验收记录](docs/docker_on_this_pc.md)

实验框架升级后需要新建实验，旧实验和快照保留原样。当前schema 9；不要编辑旧manifest绕过指纹验证。正式模型实验和费用结论需独立运行与核验。
