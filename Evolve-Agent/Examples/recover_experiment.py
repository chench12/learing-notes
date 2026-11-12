"""检查或协调遗留状态；不请求模型，不执行候选。"""

import argparse
import json
from pathlib import Path

from Evolve.recovery import recover_experiment, recovery_report
from Evolve.session import ExperimentSession


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--apply", action="store_true", help="协调遗留状态；不重发请求或重跑评测")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    session = ExperimentSession(project / args.experiment, project)
    with session.storage.operation():
        report = recover_experiment(session) if args.apply else recovery_report(session)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
