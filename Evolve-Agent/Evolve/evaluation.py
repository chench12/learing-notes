"""同一候选、同一阶段的重复评测统计；不混合不同预算，不更新搜索选择。"""

from collections import Counter
from math import isfinite
import statistics

from Evolve.contracts import Evaluation


REPEAT_POLICY = "all-valid-median-v1"


def summarize_repeats(candidate_id, attempts, planned_repeats, *, stage="standard"):
    """attempts 为 (evaluation_attempt_id, Evaluation) 序列，保留失败和未完成分母。"""
    if type(planned_repeats) is not int or planned_repeats <= 0:
        raise ValueError("planned_repeats 必须为正整数")
    if len(attempts) > planned_repeats:
        raise ValueError("已完成次数不能超过计划次数")
    identifiers = set()
    for identifier, result in attempts:
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("重复评测必须使用不同的非空评测编号")
        identifiers.add(identifier)
        if not isinstance(result, Evaluation) or result.candidate_id != candidate_id:
            raise ValueError("不能汇总不同候选的评测")
        if result.stage != stage:
            raise ValueError("不能汇总不同阶段的评测")
    valid = [(identifier, result) for identifier, result in attempts if result.status == "ok"]
    scores = [result.score for _, result in valid]
    complete = len(attempts) == planned_repeats
    best = max(valid, key=lambda item: item[1].score, default=None)
    median = None
    unrepresentable = []
    stdev = None
    if scores:
        try:
            median = statistics.median(scores)
        except OverflowError:
            pass
        if median is None or not isfinite(median):
            # 两个有限大数求和可能溢出；先除以 2 再相加。
            median = statistics.median_low(scores) / 2 + statistics.median_high(scores) / 2
    if len(scores) >= 2:
        try:
            stdev = statistics.stdev(scores)
        except OverflowError:
            pass
        if stdev is None or not isfinite(stdev):
            stdev = None
            unrepresentable.append("sample_stdev")
    # 统计量仅条件于成功样本；同时报告合法率，不能把失败当 0 分或悄悄删掉。
    score_stats = {
        "count": len(scores),
        "mean": statistics.mean(scores) if scores else None,
        "median": median,
        "sample_stdev": stdev,
        "min": min(scores) if scores else None,
        "max": max(scores) if scores else None,
    }
    return {
        "candidate_id": candidate_id, "stage": stage, "policy": REPEAT_POLICY,
        "planned_repeats": planned_repeats, "completed_repeats": len(attempts),
        "successful_repeats": len(valid), "complete": complete,
        "status_counts": dict(Counter(result.status for _, result in attempts)),
        "valid_rate": len(valid) / planned_repeats if complete else None,
        "observed_valid_rate": len(valid) / len(attempts) if attempts else None,
        "conditional_score_stats": score_stats,
        "unrepresentable_statistics": unrepresentable,
        "all_valid_median": score_stats["median"] if complete and len(valid) == planned_repeats else None,
        "best_evaluation_attempt_id": best[0] if best else None,
        "evaluation_attempt_ids": [identifier for identifier, _ in attempts],
    }
