"""多个独立实验的有限并发入口，默认评测串行避免同机争用。"""

import argparse
import asyncio
from pathlib import Path

from Evolve.batch import run_batch
from Evolve.session import ExperimentSession
from Examples.experiment_output import refresh_overview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiments", nargs="+", required=True)
    parser.add_argument("--steps", type=int)
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    sessions = [ExperimentSession(project / item, project) for item in args.experiments]
    try:
        results = asyncio.run(run_batch(sessions, concurrency=args.concurrency, steps=args.steps))
    finally:
        for session in sessions:
            refresh_overview(session.directory)
    for result in results:
        print(result["experiment_id"], result["status"], result.get("error_type", ""))


if __name__ == "__main__":
    main()
