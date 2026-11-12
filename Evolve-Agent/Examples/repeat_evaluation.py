"""按固定标准预算重复复核一个已保存候选，不调用模型。默认新建实验复核种子。"""

import argparse
from pathlib import Path

from Evolve.session import ExperimentSession
from Examples.experiment_output import create_development_experiment, refresh_overview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--config", help="新建实验配置")
    choice.add_argument("--experiment", help="已有实验目录（框架版本须一致）")
    parser.add_argument("--candidate", help="已有候选编号；不指定时使用当前父代或种子")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats <= 0:
        parser.error("--repeats 必须为正整数")
    if args.candidate and not args.experiment:
        parser.error("--candidate 必须与 --experiment 一起使用")
    project = Path(__file__).resolve().parents[1]
    if args.experiment:
        directory = project / args.experiment
    else:
        directory, _ = create_development_experiment(args.config or "configs/circle_packing.json", project)
    session = ExperimentSession(directory, project)
    if args.candidate:
        candidate = session.candidates().get(args.candidate)
        if candidate is None:
            parser.error("实验中不存在指定候选")
    else:
        candidate = session.parent()
    print("实验目录：", directory.resolve(), flush=True)
    print("复核候选：", candidate.candidate_id, flush=True)
    print("每次候选时限：", session.task.timeout_seconds, "秒；不会请求模型", flush=True)

    def progress(index, identifier, result):
        print(f"[{index}/{args.repeats}] {identifier}：{result.status}，分数={result.score}", flush=True)

    try:
        group_id, summary = session.evaluate_repeated(candidate, args.repeats, progress=progress)
    finally:
        refresh_overview(directory)
    print("合法率：", summary["valid_rate"])
    print("成功样本统计：", summary["conditional_score_stats"])
    print("全部合法时的中位分数：", summary["all_valid_median"])
    folder = directory / "evaluation_groups" / group_id
    print("统计记录：", folder / "summary.json")
    if summary["best_solution"]:
        print("本组最佳合法布局：", folder / summary["best_solution"]["file"])
    print("复核记录独立保存，不改变搜索父代选择。")


if __name__ == "__main__":
    main()
