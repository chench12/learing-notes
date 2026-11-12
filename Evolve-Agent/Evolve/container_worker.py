"""由 docker 的 python -c 启动；仅使用镜像标准库和 stdin 中的本次输入。

stdout 专门传输结果，stderr 传输候选/评测器日志。容器不接触宿主目录。
"""

from dataclasses import asdict
import json
import os
from pathlib import Path
import platform
import runpy
import sys


def main():
    # 镜像/容器启动在限时外，收到 READY 后开始本阶段计时。
    print(json.dumps({"protocol": "evolve-container-v1", "python": sys.version,
                      "platform": platform.platform()}), flush=True)
    # Docker attach 不保证 stdin EOF 立即传到容器；用单行 JSON 定界。
    payload = json.loads(sys.stdin.buffer.readline())
    result_stream = os.fdopen(os.dup(1), "w", encoding="utf-8")
    # 包括候选直接向 fd 1 写入及其后代输出，都进入日志管道。
    sys.stdout.flush()
    os.dup2(2, 1)
    os.chdir("/work")
    if payload["mode"] == "candidate":
        program = Path("candidate.py")
        program.write_text(payload["code"], encoding="utf-8")
        namespace = runpy.run_path(str(program))
        result = namespace["solve"]()
    elif payload["mode"] == "evaluator":
        package = Path("Evolve")
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "contracts.py").write_text(payload["contracts"], encoding="utf-8")
        sys.path.insert(0, "/work")
        from Evolve.contracts import Evaluation

        evaluator = Path("evaluator.py")
        evaluator.write_text(payload["code"], encoding="utf-8")

        def reject_constant(value):
            raise ValueError(f"JSON 不允许 {value}")

        solution = json.loads(payload["solution_json"], parse_constant=reject_constant)
        result = runpy.run_path(str(evaluator))["evaluate"](solution, payload["candidate_id"])
        if not isinstance(result, Evaluation):
            raise TypeError("评测器必须返回 Evaluation")
        if result.candidate_id != payload["candidate_id"]:
            raise ValueError("评测结果的 candidate_id 不一致")
        result = asdict(result)
    else:
        raise ValueError("未知容器执行模式")
    with result_stream:
        json.dump(result, result_stream, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    main()
