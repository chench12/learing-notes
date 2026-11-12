from dataclasses import asdict
from pathlib import Path
from pprint import pprint

from Evolve.contracts import Candidate
from tasks.circle_packing.initial import solve
from tasks.circle_packing.evaluator import evaluate

PROJECT_ROOT = Path(__file__).resolve().parents[1]

program_path = (
    PROJECT_ROOT / "tasks" / "circle_packing" / "initial.py"
)

candidate = Candidate(
    task_id="circle_packing_26",
    code=program_path.read_text(encoding="utf-8"),
)

solution = solve()
result = evaluate(solution, candidate.candidate_id)

pprint(asdict(result), sort_dicts=False)