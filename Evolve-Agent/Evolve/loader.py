from pathlib import Path

from Evolve.contracts import TaskSpec
from Evolve.blocks import parse_blocks

def load_task(task_dir, task_id, timeout_seconds=30.0):
    task_dir = Path(task_dir).resolve()
    description = task_dir / "description.md"
    initial = task_dir / "initial.py"
    evaluator = task_dir / "evaluator.py"

    for path in (description, initial, evaluator):
        if not path.is_file():
            raise FileNotFoundError(f"缺少任务文件：{path}")

    parse_blocks(initial.read_text(encoding="utf-8"))

    return TaskSpec(
        task_id=task_id,
        description=description.read_text(encoding="utf-8"),
        initial_program=initial,
        evaluator=evaluator,
        timeout_seconds=timeout_seconds,
    )
