import argparse
from pathlib import Path

from Evolve.experiment import create_experiment
from Evolve.generation import generate_once
from Evolve.session import ExperimentSession
from Evolve.routing import ModelPool
from Examples.experiment_output import refresh_overview


def main():
    parser = argparse.ArgumentParser(description="创建实验或在现有实验中生成一个子代")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--config", help="新实验配置，默认 configs/circle_packing.json")
    choice.add_argument("--experiment", help="继续使用的实验目录")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]

    if args.experiment:
        directory = project_root / args.experiment
    else:
        directory, _ = create_experiment(
            args.config or "configs/circle_packing.json", project_root,
        )

    print("实验目录：", directory.resolve())
    session = ExperimentSession(directory, project_root)
    # 预算/计划通过后才创建所选路由客户端，密钥只在调用端读取。
    model = ModelPool()
    try:
        generate_once(session, model)
    finally:
        refresh_overview(directory)


if __name__ == "__main__":
    main()
