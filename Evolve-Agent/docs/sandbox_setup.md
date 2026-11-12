# Docker 执行后端：准备、验证与边界

当前状态（2026-11-12更新）：本机 WSL 2 / Docker 已安装并启动，程序、虚拟磁盘及 Desktop 状态位于 D 盘。手写种子双容器执行成功，半径和 2.1666666666666665；8 项真实容器集成测试全部通过，0 跳过（12.507秒）。本次没有调用模型。启动请使用桌面 Docker Desktop - D Drive 专用入口，详细路径、启动兼容处理及原始证据见下方本机记录。

这台Windows 11家庭版电脑的实际安装、专用启动方法和验收证据见 [docker_on_this_pc.md](docker_on_this_pc.md)。

## 为什么分为两个容器

宿主只负责模型调用、预算与记录。候选容器只接收候选代码并返回 JSON；固定评测器的源代码、任务快照、实验目录和 API Key 都不传入候选容器。候选结束并移除后，宿主才把保存的 JSON、固定评测器和数据协议传入另一个容器。

两个容器均不挂载宿主目录，不使用 Docker socket。输入通过 stdin，结果通过 stdout，日志通过 stderr。宿主将文件保存到每次评测自己的 artifacts 目录。候选的 print 和直接向 fd 1 写日志都被转到 stderr，避免把日志混入正常的结果协议。故意破坏结果通道只会导致解析/评测失败，不能获取宿主权限。

与论文的关系：这是为本机实现选择的执行基础设施，不是论文公开的原始执行代码。

## 1. 准备运行时

在 Windows 上安装并启动 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)，使用 **WSL 2 的 Linux 容器后端**。按照安装器提示启用 WSL/虚拟化并在需要时重启；可参考 [Microsoft 的 WSL 安装说明](https://learn.microsoft.com/windows/wsl/install)。安装系统组件需要相应系统权限，本项目的检查脚本不会替你安装。

这项圆装填任务使用 CPU，容器不需要 CUDA 或 GPU 透传，3050 的 4GB 显存不影响本阶段。

重新打开 PowerShell，在项目根目录确认 daemon 可用并准备标准库镜像：

```powershell
docker version
docker pull python:3.12-slim-bookworm
```

只有这里的 pull 命令会下载镜像。运行实验时始终使用 `--pull never`，不会隐式下载。选择官方 Python 标准库镜像；不要为运行候选添加宿主挂载、服务凭据或第三方工具。执行镜像不能声明 VOLUME。

## 2. 检查并生成独立配置

```powershell
python -X utf8 -m Examples.check_sandbox --image python:3.12-slim-bookworm --write-config configs/circle_packing.docker.json
```

本机已生成 configs/circle_packing.docker.json，无需重复生成。下列生成说明供其它机器或新配置使用。脚本检查 Linux daemon、内存/swap/进程数/CPU 配额支持和 seccomp，再检查本地镜像。它把 tag 解析为 `sha256:...` 镜像 ID，生成新配置，拒绝覆盖已有文件。后续 tag 更新不会悄悄改变实验解释器。已有本地配置保持 `backend=local-reviewed`，不会被转换。

Docker 未安装、daemon 未启动、镜像未准备或必要限制不受支持时退出非零，并说明原因。配置本身也拒绝浮动 tag；必须使用完整镜像 ID。后端失败没有本地回退。

镜像检查通过只证明前置条件满足，尚不代表实际容器验收通过。

## 3. 先执行种子，不调用模型

```powershell
python -X utf8 -m Examples.check_execution --config configs/circle_packing.docker.json
```

预期：状态 ok，分数约 2.1666666666666665，backend 为 docker，两个隔离字段为 True。打开新评测目录的 execution.json，确认两阶段使用不同的 container_name、相同镜像 ID、Linux/Python 运行身份，以及 cleanup_succeeded=true。

启动失败时，脚本仍会保存评测状态和已有日志；不要只看“内存限制=True”或孤立的分数判断成功。以评测状态、阶段状态和清理记录共同判断。

## 4. 运行真实隔离验收

```powershell
$env:EVOLVE_DOCKER_TEST_IMAGE = (Get-Content configs/circle_packing.docker.json -Raw | ConvertFrom-Json).execution.docker_image
python -X utf8 -m unittest discover -s tests -p test_docker_integration.py -v
```

这些测试使用手写程序，不请求模型。覆盖：合法种子；宿主临时文件/固定评测器路径不可见；测试凭据不继承；禁止外部网络连接；非 root 身份与根文件系统只读；带后代进程的超时清理；日志/结果上限；tmpfs 配额；内存限额；评测器独立超时。每次执行后检查 UUID 容器已不存在。

未设置测试镜像时会明确显示 skipped；跳过不能计为隔离验证通过。设置镜像后运行时不可用会让测试失败，不自动跳过或回退。Docker 启动、管道和清理可能因 Windows/WSL 设置产生差异，实际测试失败后再据日志修正。

## 5. 再运行一代真实生成

确认前面的实际测试通过后，在已有 API Key 的终端运行：

```powershell
python -X utf8 -m Examples.generate_candidate --config configs/circle_packing.docker.json
```

这是付费模型调用；前面步骤没有模型调用。仍然一次只生成一代，不启动自动大规模搜索。Docker 框架变更后创建新实验，历史本地实验不混排。

## 资源与计时约定

- 容器以 UID/GID 65534 运行，network=none，根文件系统只读，丢弃所有 Linux capabilities，启用 no-new-privileges，并依赖 daemon 的默认 seccomp。
- 内存和 swap 上限相等，避免额外 swap；pids-limit 计入容器内所有进程/线程。当前 Docker 方案固定 CPU 配额为 1 个 CPU，不传入 GPU。
- /work 是限制大小的 tmpfs，nosuid/nodev/noexec；Python 仍可解释这里的候选源代码。/dev/shm 另设 1 MiB 上限，tmpfs 同时计入内存限制。这里不是把宿主目录作为工作目录。
- 日志和结果由宿主通过有界管道保存；Docker 使用 log-driver=none，避免 daemon 另外保存无界候选日志。输出被截断时保存为 .partial，并记 original_bytes=null、complete=false；不能拿它当完整解。
- 容器创建、启动等待和执行分开计时。startup_timeout_seconds 限制单次创建/握手等待；候选时限从 READY 开始，包含输入传输、代码加载、求解和结果序列化。elapsed_seconds 记录包含检查、创建和清理的总墙钟时间，execution_seconds 记录执行时段。固定评测器也有独立时限。
- Docker 内存峰值目前不采集，peak_memory_bytes 为 null；保留 daemon 的 OOMKilled 和退出码，不用瞬时占用冒充峰值。
- 退出、超时、输出超限和中断都尝试强制移除本次 UUID 容器。若 daemon 失联导致不能确认清理，记录 cleanup_succeeded=false，不报告成功，并保留容器名供排查。

本地 Windows 与 Docker Linux 的 Python 版本、CPU 配额和启动计时不同，成绩不能直接当作同一条件比较。正式实验应冻结 Docker 镜像与协议，重新建立基线。

容器依赖宿主 Docker/WSL 和内核的隔离能力，不等于形式化安全证明。这里的验收针对本项目所需的实际边界。结构化评测、自动同预算复核、事务恢复、混合档案和全局预算控制已有串行首版，见 [agent_usage.md](agent_usage.md)。真实自动循环入口为 Examples.run_search；应先完成实际隔离验收，再开展正式模型实验。
