import json
from pathlib import Path

from Evolve.contracts import Candidate, Evaluation


class ProgramDatabase:
    def __init__(self,runs_dir,task_id):
        self.runs_dir = Path(runs_dir)
        self.task_id = task_id

    def records(self):
        """读取当前任务已完成评测的候选。"""
        records = []

        for candidate_path in sorted(
                self.runs_dir.glob("*/candidate.json")
        ):
            evaluation_path = candidate_path.with_name(
                "evaluation.json"
            )
            # 生成完成但尚未评测的候选，暂不参与选择。
            if not evaluation_path.is_file():
                continue

            candidate = Candidate(
                **json.loads(
                    candidate_path.read_text(encoding="utf-8")
                )
            )
            # 不混用不同任务的程序。
            if candidate.task_id != self.task_id:
                continue

            evaluation = Evaluation(
                **json.loads(
                    evaluation_path.read_text(encoding="utf-8")
                )
            )

            if evaluation.candidate_id != candidate.candidate_id:
                raise ValueError(
                    f"候选与评测编号不一致：{candidate_path.parent}"
                )

            records.append((candidate, evaluation))

        return records

    def best(self):
        """返回最高分合法候选；没有时返回 None。"""
        successful = [
            (candidate, result)
            for candidate, result in self.records()
            if result.status == "ok"
        ]

        if not successful:
            return None

        return max(
            successful,
            key=lambda record: record[1].score,
        )