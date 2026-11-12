from Evolve.contracts import Candidate, Evaluation

candidate = Candidate(
    task_id="circle_packing_26",
    code="def solve():\n    return []\n",
)

result = Evaluation(
    candidate_id=candidate.candidate_id,
    status="invalid",
    feedback="需要输出 26 个圆，实际输出为空。",
)

print("候选编号：", candidate.candidate_id)
print("任务：", candidate.task_id)
print("状态：", result.status)
print("分数：", result.score)
print("反馈：", result.feedback)