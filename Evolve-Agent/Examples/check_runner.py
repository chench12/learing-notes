from dataclasses import asdict
from pathlib import Path
from pprint import pprint

from Evolve.contracts import Candidate
from Evolve.loader import load_task
from Evolve.runner import run_candidate


PROJECT_ROOT = Path(__file__).resolve().parents[1]

task = load_task(
    task_dir=PROJECT_ROOT / "tasks" / "circle_packing",
    task_id="circle_packing_26",
    timeout_seconds=3.0,
)

candidate = Candidate(
    task_id=task.task_id,
    code=task.initial_program.read_text(encoding="utf-8"),
)

result = run_candidate(task, candidate)

pprint(asdict(result), sort_dicts=False)