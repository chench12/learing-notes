# 本机 Docker：D 盘启动与验收记录

检查日期：2026-11-12。已安装并启动 Docker，手写种子及 8 项真实容器集成测试均通过。本次没有调用模型 API。

## 启动方法

双击桌面 **Docker Desktop - D Drive** 快捷方式，等待引擎启动。也可在项目根目录运行：

```powershell
.\scripts\start_docker_d.cmd
docker version
```

docker version 应同时返回 Client 和 Server。专用脚本已在引擎运行时验证，尚未验证下一次系统重启后的冷启动。本机请使用上述专用入口，不要开启默认入口的自动启动。

## 已确认的环境和路径

| 项目 | 实际结果 |
|---|---|
| 系统 | Windows 11 家庭中文版 x64，build 26200.9457 |
| CPU / 内存 | Intel i7-12700H，约 16GB |
| WSL / 内核 | 3.0.5.0 / 6.18.58.1，WSL 2 |
| Docker Desktop | 4.99.0 (245822) |
| Docker Engine | 29.9.3，Linux amd64，cgroup v2 |
| 程序 | D:\Docker\Docker |
| 镜像和容器磁盘 | D:\DockerData\disk\docker_data.vhdx |
| WSL 运行磁盘 | D:\DockerData\main\ext4.vhdx |
| Docker 设置 | D:\DockerSettings\Roaming\Docker |
| Docker 日志及用户状态 | D:\DockerSettings\Local\Docker |
| 专用启动脚本 | D:\Evolve-Agent\scripts\start_docker_d.cmd |

无需安装 Ubuntu。圆装填计算使用 CPU，模型由远程 API 提供，不需要 CUDA 镜像或 GPU 透传。Windows 的 WSL 系统组件、安装缓存和 Docker CLI 用户配置仍可能占用 C 盘；主要程序、虚拟磁盘和 Desktop 状态已放到 D 盘。

## 为什么使用专用入口

默认启动时，Docker 在 C 盘用户设置目录原子替换文件失败，报 WinError 17（不能将文件移到不同磁盘）。同目录文件替换也能复现该问题；相关目录带 EFS 加密属性，但尚未证明加密是唯一原因。仅针对 Docker 目录的管理员解密尝试返回 Access denied，未改变原有加密设置。

启动脚本只为 Docker 进程及其子进程设置 APPDATA 和 LOCALAPPDATA，分别指向 D:\DockerSettings\Roaming 与 D:\DockerSettings\Local。没有修改系统全局环境变量、注册表或整个用户目录，也没有搬动原有 C 盘文件。正常开始菜单入口仍可能失败，因此保留专用快捷方式。不要手动移动运行中的 VHDX 文件。

## 真实验收证据

配置：[circle_packing.docker.json](../configs/circle_packing.docker.json)。镜像来自 python:3.12-slim-bookworm，已固定为本地 ID：

```text
sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
```

check_execution 的手写种子状态为 ok，半径和 2.1666666666666665。候选与固定评测器使用不同容器，均退出 0、清理成功；filesystem_isolated 与 network_isolated 均为 true。容器 Python 为 3.12.16。

原始记录：

```text
dev_runs/exp_895994062a164ddd9883b4b3bc827e01/evaluations/eval_693a3b9f78c1425bb6a966f6f6b833cb/artifacts/execution.json
```

test_docker_integration.py：8 项通过，0 失败、0 错误、0 跳过，耗时 12.507 秒。覆盖种子双容器、宿主文件/环境/网络隔离、评测器独立超时、结果超限、日志上限、内存限制、后代进程超时清理和 tmpfs 配额。验收后 docker ps -a --filter name=evolve- 无遗留容器。此前完整离线回归为 190 项，其中 182 项通过、8 项 Docker 测试跳过；此次单独补齐这 8 项，不表示重新运行了完整套件。

## 下一步：三代模型预实验

在设置好 RELAY_API_KEY 的终端、项目根目录运行以下命令；这一步会产生模型费用，本次没有替你执行：

```powershell
python -X utf8 -m Examples.run_search --config configs/circle_packing.docker.json --steps 3
```

先确认配置中的服务地址与模型名符合预期。新命令会建立新实验并冻结当前 Python 环境；不要用不同宿主解释器接着运行本页的历史验收实验。先看补丁成功率、合法率、父子分数差及 token 消耗，再扩大预算。三代用于检查流程，不足以比较策略胜负。

恢复自己创建的实验时，使用命令输出的目录：

```powershell
python -X utf8 -m Examples.run_search --experiment experiments/exp_实际编号 --steps 3
```

完整边界和验收重跑方法见 [sandbox_setup.md](sandbox_setup.md)。已有 Docker 配置不必重新生成；check_sandbox 的 --write-config 会拒绝覆盖。

## 安装溯源

WSL 命令下载曾返回 403，改用微软官方 WSL 3.0.5 x64 MSI，签名 Valid / Microsoft Corporation。用户启用 VirtualMachinePlatform 并重启后，安装 Docker 官方 x64 包，签名 Valid / Docker Inc.，使用 WSL 2 后端与安装器的 --installation-dir、--wsl-default-data-root 参数指定 D 盘。安装包和下载收据保留在 .tools/installers。没有替用户自动接受安装条款。

- [Docker 官方 Windows 安装及参数](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Microsoft WSL 安装](https://learn.microsoft.com/en-us/windows/wsl/install)
- [Microsoft WSL 发布页](https://github.com/microsoft/WSL/releases/tag/3.0.5)
