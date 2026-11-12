"""有限预算串行搜索；离线夹具可本地演示，真实自动运行要求Docker。"""

import argparse
import json
from pathlib import Path

from Evolve.blocks import normalize_source, parse_blocks
from Evolve.controller import run_search
from Evolve.experiment import create_experiment
from Evolve.session import ExperimentSession
from Evolve.storage import encode_json
from Examples.check_archive import demo_programs
from Examples.experiment_output import create_development_experiment, refresh_overview


class ReviewedFixtureModel:
    """可恢复的手写夹具，读取请求中的父代；不连接任何服务。"""
    def __init__(self, initial):
        self.programs = demo_programs(initial)
        self.initial = initial

    def generate(self, *, messages, max_tokens):
        user = messages[-1]["content"]
        if "parent_instruction" in user and "父代程序：" not in user:
            text = json.dumps({"instruction": "先检查布局的活动约束，再尝试不同初始化并比较同预算下的合法半径和。"}, ensure_ascii=False)
        else:
            marker = "父代程序：\n```python\n"
            start = user.index(marker) + len(marker)
            end = user.index("\n```", start)
            parent = normalize_source(user[start:end])
            target = next(program for program in self.programs if normalize_source(program) != parent)
            search, replacement = parse_blocks(parent).editable[1], parse_blocks(target).editable[1]
            text = f"<<<<<<< SEARCH\n{search}=======\n{replacement}>>>>>>> REPLACE\n"
        # 真实token未知；离线夹具返回0仅表示没有外部模型费用。
        return {"text": text, "finish_reason": "stop", "model": "reviewed-handwritten-fixture", "usage": {"total_tokens": 0}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", default=None)
    source.add_argument("--experiment")
    parser.add_argument("--steps", type=int, help="本次启动最多新增几代；不放宽实验总预算")
    parser.add_argument("--offline-demo", action="store_true", help="只用已审查的圆装填手写夹具，无API调用")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if args.experiment:
        directory = project / args.experiment
    else:
        create = create_development_experiment if args.offline_demo else create_experiment
        directory, _ = create(args.config or "configs/circle_packing.json", project)
    session = ExperimentSession(directory, project)
    print("实验目录：", directory)
    model = None
    if args.offline_demo:
        if session.task.task_id != "circle_packing_26" or session.config["search"]["generation_mode"] != "patch":
            parser.error("离线演示仅支持标准圆装填任务的patch模式")
        model = ReviewedFixtureModel(session.task.initial_program.read_text(encoding="utf-8"))
        print("本地手写夹具，不连接模型服务，不代表搜索质量实验。")
    try:
        summary = run_search(session, model=model, steps=args.steps,
                             reviewed_fixtures=args.offline_demo,
                             stop_requested=lambda: (session.directory / "STOP").exists())
    finally:
        refresh_overview(directory)
    print("停止原因：", summary["stop_reason"])
    print("本次新增代数：", summary["new_generations"])
    print("用量：", summary["usage"])
    print("控制记录：", directory / "controls" / summary["control_id"] / "control.json")


if __name__ == "__main__":
    main()
