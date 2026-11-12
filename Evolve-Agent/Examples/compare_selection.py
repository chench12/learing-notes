"""三种父代选择的离线机制演示、匹配配置准备及只读结果比较。"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

from Evolve.comparison import METHODS, compare_sessions, prepare_configs, write_comparison
from Evolve.selection import sample_parent, selection_weights
from Evolve.session import ExperimentSession
from Evolve.storage import encode_json


def mechanism_demo(config, draws):
    # 明确为假设数值；没有运行 LLM 或圆装填优化，不作为质量实验。
    cells = [{"candidate_id": identifier, "score": score, "cell": [i],
              "evaluation_attempt_id": f"illustrative_{identifier}"}
             for i, (identifier, score) in enumerate([("A", 2.60), ("B", 2.50), ("C", 2.30)])]
    scenarios = [
        ("fresh", "三个父代尚未扩展", 0, {}),
        ("productive_branch", "A已扩展60次，B扩展5次且产生过2.64分子代", 65, {
            "A": {"visits": 60, "direct_expansions": 60, "best_child_score": 2.60},
            "B": {"visits": 5, "direct_expansions": 5, "best_child_score": 2.64}}),
        ("neglected_branch", "A/B各扩展50次，C尚未扩展", 100, {
            "A": {"visits": 50, "direct_expansions": 50, "best_child_score": 2.60},
            "B": {"visits": 50, "direct_expansions": 50, "best_child_score": 2.64}}),
    ]
    rows = []
    snapshot = {"islands": [{"island_id": 0, "cells": cells}]}
    for name, description, expansions, records in scenarios:
        statistics = {"islands": [{"expansions": expansions, "candidates": records}]}
        for method in METHODS:
            selected_config = deepcopy(config)
            selected_config["parent_sampling"] = method
            probabilities, diagnostics = selection_weights(cells, selected_config, statistics["islands"][0])
            counts = {identifier: 0 for identifier in ("A", "B", "C")}
            for index in range(draws):
                selected = sample_parent(snapshot, seed=42, selection_index=index,
                                         config=selected_config, statistics=statistics)
                counts[selected["parent_id"]] += 1
            rows.append({"scenario": name, "description": description, "method": method,
                         "probabilities": dict(zip(counts, probabilities)),
                         "frozen_snapshot_query_counts": counts, "diagnostics": diagnostics})
    return {"kind": "illustrative-selection-mechanisms-v1", "model_calls": 0,
            "draws_per_frozen_snapshot": draws, "rows": rows,
            "interpretation": "假设父代/奖励/反馈，只比较选择机制。重复查询中不更新反馈，不是多轮搜索性能实验。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="默认：假设数据的离线选择机制演示")
    mode.add_argument("--prepare", action="store_true", help="仅生成三策略配置，不创建或运行实验")
    mode.add_argument("--experiments", nargs="+", help="只读比较已运行的匹配实验")
    parser.add_argument("--config", default="configs/circle_packing.json")
    parser.add_argument("--seeds", type=int, nargs="+", default=[42, 43, 44])
    parser.add_argument("--generations", type=int, default=100)
    parser.add_argument("--draws", type=int, default=1000)
    parser.add_argument("--output", help="必须是尚不存在的输出目录")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = project / (args.output or f"comparisons/selection_{uuid4().hex}")
    if args.experiments:
        sessions = [ExperimentSession(project / path, project) for path in args.experiments]
        report = compare_sessions(sessions)
        write_comparison(report, output)
        for row in report["aggregate"]:
            print(row["method"], "平均最终最佳：", row["mean_best_latest_score"],
                  "平均覆盖率：", row["mean_coverage_fraction"], "已知平均token：", row["mean_known_tokens"])
        if not report["all_budgets_finished"]:
            print("有实验未完成计划预算，目前只能作为进度快照比较。")
        if not report["equal_requested_generations"]:
            print("各实验已发起的请求数不相等，不能按等调用预算解释当前差异。")
    else:
        config = json.loads((project / args.config).read_text(encoding="utf-8"))
        if args.prepare:
            plan = prepare_configs(config, output, seeds=args.seeds, generations=args.generations)
            print("已生成配置：", len(plan["runs"]), "份；每份最多", args.generations, "次生成计划。")
            print("没有创建实验或调用模型。单次用 Examples.generate_candidate；Docker准备并验收后可用 Examples.run_search。")
        else:
            if args.draws <= 0:
                parser.error("--draws 必须为正整数")
            report = mechanism_demo(config["archive"], args.draws)
            output.mkdir(parents=True, exist_ok=False)
            (output / "mechanism_demo.json").write_bytes(encode_json(report))
            for row in report["rows"]:
                print(row["scenario"], row["method"],
                      "A/B/C选择概率：", [round(value, 4) for value in row["probabilities"].values()])
            print(report["interpretation"])
            print("真实 API 调用：0")
    print("输出目录：", output.resolve())


if __name__ == "__main__":
    main()
