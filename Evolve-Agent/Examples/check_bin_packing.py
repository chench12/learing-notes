"""第二任务手写 first-fit/best-fit 基线；不调用模型。"""

from pathlib import Path

from Evolve.contracts import Candidate
from Examples.experiment_output import create_development_experiment as create_experiment
from Evolve.session import ExperimentSession


def main():
    project = Path(__file__).resolve().parents[1]
    directory, _ = create_experiment("configs/bin_packing.json", project)
    session = ExperimentSession(directory, project)
    first = session.parent()
    _, first_result = session.evaluate(first, "seed")
    code = first.code.replace(
        "    for index, space in enumerate(remaining):\n        if space >= item:\n            return index\n    return len(remaining)",
        "    feasible = [i for i, space in enumerate(remaining) if space >= item]\n"
        "    return min(feasible, key=lambda i: remaining[i] - item) if feasible else len(remaining)")
    best = Candidate(task_id=session.task.task_id, code=code)
    session.save_candidate(best)
    _, best_result = session.evaluate(best, "handwritten_baseline")
    print("first-fit：", first_result.score, first_result.metrics)
    print("best-fit：", best_result.score, best_result.metrics)
    print("两者都是人工基线，不是模型搜索；容量下界不代表已知最优。")
    print("实验目录：", directory)


if __name__ == "__main__":
    main()
