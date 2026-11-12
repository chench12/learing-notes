# 在学习笔记仓库中更新项目

当前项目已准备.gitignore，源码、任务、测试、文档及JSON配置可以上传。experiments、dev_runs、runs、comparisons、.tools、编辑器配置、Python缓存、虚拟环境、.env和运行数据库默认不上传；这些文件仍完整保留在本机。

配置目前只保存服务地址、模型名称和密钥环境变量名，不保存密钥值。requirements.txt记录当前pytorch环境实际使用的远程接口依赖；不需要安装PyTorch或CUDA即可运行本框架。

## 提交和更新

本项目位于 `learning-notes/Evolve-Agent/`，使用外层学习笔记仓库的 Git 配置和远程地址。修改代码后，在外层仓库根目录执行：

```powershell
cd D:\CodeBuddy\learning-notes
git status --short
git add Evolve-Agent/
git diff --cached --stat
git commit -m "更新 Evolve-Agent 项目"
git push
```

`git diff --cached --stat` 用于确认暂存内容。实验目录和安装包由项目的 `.gitignore` 排除。新增源码、文档和任务仍放在 `Evolve-Agent/` 下。

如果同时修改了学习笔记，在外层根目录先运行以下命令，再一起提交：

```powershell
python scripts/update_readme.py
git add README.md study-notes/notes.md study-notes/images/ scripts/update_readme.py
```

原项目内部的 `.git` 已备份到外层仓库的 `.local-git-backups/`，该备份只保存在本机。

## 克隆后如何使用

```powershell
git clone https://github.com/chench12/learing-notes.git
cd learing-notes/Evolve-Agent
python -m pip install -r requirements.txt
python -X utf8 -m Examples.run_search --config configs/circle_packing.offline.json --offline-demo
```

使用Python3.12。最后一条使用手写响应，不请求模型。真实模型调用前自行检查configs中的服务地址和模型名，并设置RELAY_API_KEY。JSON配置不能直接包含真实密钥。GitHub不保存当前PowerShell里的环境变量。

Docker配置中的固定镜像ID来自本机，不保证其它电脑已有该镜像；换电脑时按sandbox_setup.md准备镜像、生成新的Docker配置并验收。D盘专用启动脚本也只是本机启动辅助。

## 实验结果和许可证

当前Git仓库用于版本管理代码和方法。实验数据库、完整提示词、模型响应及布局都保留在本机，GitHub不能替代它们的备份。以后分享结果时，可另建results目录，只导出需要公开的图、表、汇总与对应最佳程序，并说明预算和复核条件。

仓库许可证见外层的 [LICENSE](../../LICENSE)。
