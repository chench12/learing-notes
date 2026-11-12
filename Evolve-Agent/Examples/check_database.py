from pathlib import Path

from Evolve.database import ProgramDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]

database = ProgramDatabase(
    runs_dir=PROJECT_ROOT / "runs",
    task_id="circle_packing_26",
)

for candidate, result in database.records():
    print(
        "候选：", candidate.candidate_id,
        "状态：", result.status,
        "分数：", result.score,
    )

selected = database.best()

if selected is None:
    print("没有可用父代，需要使用初始程序")
else:
    parent, evaluation = selected
    print("\n选中的父代：", parent.candidate_id)
    print("历史分数：", evaluation.score)