"""检查本地 Docker 镜像并生成独立配置；不安装、不拉取镜像、不请求模型。"""

import argparse
import json
from pathlib import Path
import sys

from Evolve.contracts import ExecutionLimits
from Evolve.docker_execution import DockerUnavailable, inspect_image, inspect_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.12-slim-bookworm")
    parser.add_argument("--config", default="configs/circle_packing.json")
    parser.add_argument("--write-config", help="保存固定镜像 ID 的新配置；不覆盖已有文件")
    args = parser.parse_args()
    try:
        runtime = inspect_runtime()
        image = inspect_image(args.image)
    except DockerUnavailable as exc:
        print(f"隔离后端未就绪：{exc}", file=sys.stderr)
        return 1
    print("本地镜像 ID：", image["id"])
    print("Docker daemon：", runtime["server_version"], "cgroup", runtime["cgroup_version"])
    print("镜像系统/架构：", image["os"], image["architecture"])
    print("镜像检查通过；尚未执行隔离验收测试。")
    if args.write_config:
        project = Path(__file__).resolve().parents[1]
        config = json.loads((project / args.config).read_text(encoding="utf-8"))
        config["execution"].update(backend="docker", docker_image=image["id"])
        ExecutionLimits(**config["execution"])
        target = project / args.write_config
        # x 模式拒绝覆盖，尤其不修改现有实验配置。
        with target.open("x", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        print("新配置：", target)
        print("离线种子检查：python -X utf8 -m Examples.check_execution --config", args.write_config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
