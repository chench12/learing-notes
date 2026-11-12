"""创建离线实验、执行种子并保存产物；不请求模型。"""

import argparse
import json
from pathlib import Path

from Examples.experiment_output import create_development_experiment as create_experiment
from Evolve.session import ExperimentSession


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/circle_packing.json")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    directory, _ = create_experiment(args.config, project)
    session = ExperimentSession(directory, project)
    candidate = session.parent()
    evaluation_id, result = session.evaluate(candidate, "seed")
    folder = directory / "evaluations" / evaluation_id
    print("状态：", result.status)
    print("分数：", result.score)
    print("反馈：", result.feedback)
    print("实验目录：", directory)
    print("本次评测：", folder / "evaluation.json")
    print("执行产物：", folder / "artifacts")
    execution = json.loads((folder / "artifacts/execution.json").read_text(encoding="utf-8"))
    for name, phase in execution["phases"].items():
        print(f"{name}：退出码={phase['returncode']}，耗时={phase['elapsed_seconds']:.3f}s，"
              f"内存上限生效={phase['memory_limit_enforced']}")
    print("后端：", execution["backend"])
    print("文件系统隔离：", execution["filesystem_isolated"])
    print("网络隔离：", execution["network_isolated"])


if __name__ == "__main__":
    main()
