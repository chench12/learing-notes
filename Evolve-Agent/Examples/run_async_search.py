"""同一实验内有界异步搜索；默认不使用离线夹具，真实生成必须Docker。"""

import argparse
import asyncio
from pathlib import Path

from Evolve.async_controller import run_search_async
from Evolve.experiment import create_experiment
from Evolve.session import ExperimentSession
from Examples.run_search import ReviewedFixtureModel
from Examples.experiment_output import create_development_experiment, refresh_overview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config")
    source.add_argument("--experiment")
    parser.add_argument("--steps", type=int)
    parser.add_argument("--offline-demo", action="store_true", help="已审查的圆装填手写响应，无API调用")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if not args.experiment and not args.config:
        parser.error("必须指定 --config 或 --experiment；离线示例配置为 configs/circle_packing.async.offline.json")
    create = create_development_experiment if args.offline_demo else create_experiment
    directory = project / args.experiment if args.experiment else create(args.config, project)[0]
    session = ExperimentSession(directory, project)
    model = None
    if args.offline_demo:
        if session.task.task_id != "circle_packing_26" or session.config["search"]["generation_mode"] != "patch":
            parser.error("手写演示只支持标准圆装填任务的patch模式")
        model = ReviewedFixtureModel(session.task.initial_program.read_text(encoding="utf-8"))
    print("实验目录：", directory)
    try:
        result = asyncio.run(run_search_async(session, model, steps=args.steps, reviewed_fixtures=args.offline_demo,
                                             stop_requested=lambda: (directory / "STOP").exists()))
    finally:
        refresh_overview(directory)
    print("状态：", result["state"], "停止原因：", result["stop_reason"])
    print("本次新增代数：", result["new_generations"])
    print("在途/模型/评测峰值：", result["peak_inflight"], result["peak_model_calls"], result["peak_evaluations"])
    print("用量：", result["usage"])
    print("控制记录：", directory / "controls" / result["control_id"] / "control.json")
    if result["state"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
