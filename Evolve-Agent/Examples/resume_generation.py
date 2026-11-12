"""处理已持久化的模型响应；不会读取 API Key 或发出模型请求。"""

import argparse
from pathlib import Path

from Evolve.generation import process_saved_response
from Evolve.session import ExperimentSession
from Examples.experiment_output import refresh_overview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--reevaluate-interrupted", action="store_true",
                        help="明确允许为中断/失败的子代评测新建一次执行；保留旧评测")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    session = ExperimentSession(project / args.experiment, project)
    try:
        candidate, result = process_saved_response(
            session, args.generation, reevaluate_interrupted=args.reevaluate_interrupted,
        )
    finally:
        refresh_overview(session.directory)
    print("恢复结果：", candidate.candidate_id, result.status, result.score)


if __name__ == "__main__":
    main()
