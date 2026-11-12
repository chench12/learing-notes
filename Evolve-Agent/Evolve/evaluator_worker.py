"""固定评测器入口，与候选执行使用不同的进程和工作目录。"""

from dataclasses import asdict
import json
from pathlib import Path
import runpy
import sys


if __name__ == "__main__":
    if sys.stdin.buffer.readline() != b"START\n":
        raise RuntimeError("缺少执行握手")
    # -I 不加入工作目录；这里只加载框架所在的固定目录。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from Evolve.contracts import Evaluation

    evaluator, solution, output = map(Path, sys.argv[1:4])
    candidate_id = sys.argv[4]

    def reject_constant(value):
        raise ValueError(f"JSON 不允许 {value}")

    value = json.loads(solution.read_text(encoding="utf-8"), parse_constant=reject_constant)
    result = runpy.run_path(str(evaluator))["evaluate"](value, candidate_id)
    if not isinstance(result, Evaluation):
        raise TypeError("评测器必须返回 Evaluation")
    if result.candidate_id != candidate_id:
        raise ValueError("评测结果的 candidate_id 不一致")
    output.write_text(
        json.dumps(asdict(result), ensure_ascii=False, allow_nan=False), encoding="utf-8",
    )
