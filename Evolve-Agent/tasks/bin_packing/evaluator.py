"""固定整数容量验证；质量分数与装箱下界的比值，不称下界为已知最优。"""

import math
import random
from Evolve.contracts import Evaluation


def evaluate(solution, candidate_id):
    def invalid(message):
        return Evaluation(candidate_id, "invalid", feedback=message)
    if not isinstance(solution, list) or len(solution) != 12:
        return invalid("需要12个实例的箱子分配")
    totals, ratios, bins_used, wastes = [], [], [], []
    for seed, assignment in enumerate(solution):
        rng = random.Random(seed)
        if seed % 3 == 0:
            items = [rng.randint(10, 90) for _ in range(120)]
        elif seed % 3 == 1:
            items = [rng.choice([20, 30, 40, 60, 70, 80]) for _ in range(120)]
        else:
            items = [rng.randint(25, 50) for _ in range(120)]
        if not isinstance(assignment, list) or len(assignment) != len(items):
            return invalid("每个物品必须恰好有一个箱子编号")
        loads = []
        for item, index in zip(items, assignment):
            if type(index) is not int or index < 0 or index > len(loads):
                return invalid("箱子编号必须按在线顺序创建")
            if index == len(loads):
                loads.append(0)
            loads[index] += item
            if loads[index] > 100:
                return invalid("箱子容量超过100")
        lower = math.ceil(sum(items) / 100)
        ratios.append(lower / len(loads))
        totals.append(lower)
        bins_used.append(len(loads))
        wastes.extend((100 - load) / 100 for load in loads)
    score = math.fsum(ratios) / len(ratios)
    mean_waste = math.fsum(wastes) / len(wastes)
    waste_cv = math.sqrt(math.fsum((value - mean_waste) ** 2 for value in wastes) / len(wastes)) / mean_waste if mean_waste else 0
    return Evaluation(candidate_id, "ok", score=score,
        metrics={"total_bins": sum(bins_used), "capacity_lower_bound": sum(totals)},
        objectives={"mean_lower_bound_ratio": score}, constraints={"over_capacity": 0.0},
        features={"mean_waste": mean_waste, "waste_cv": waste_cv},
        feedback="全部公开实例通过容量与完整性检查；容量下界不等于已证明最优解")
