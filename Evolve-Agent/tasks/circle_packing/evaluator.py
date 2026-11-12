import math

from Evolve.contracts import Evaluation

NUM_CIRCLES = 26
TOLERANCE = 1e-9
BOUNDARY_CONTACT_TOLERANCE = 1e-6

def evaluate(solution, candidate_id: str) -> Evaluation:
    """评测求解程序的输出，不负责执行求解程序。"""

    def invalid(message, metrics=None, constraints=None):
        return Evaluation(
            candidate_id=candidate_id,
            status="invalid",
            metrics=metrics if metrics is not None else {},
            feedback=message,
            constraints=constraints if constraints is not None else {},
        )
    # 先检查输出结构，避免非法数据进入几何计算。
    if not isinstance(solution, (list, tuple)):
        return invalid("输出必须是圆的列表")

    if len(solution) != NUM_CIRCLES:
        return invalid(
            f"需要 {NUM_CIRCLES} 个圆，实际为 {len(solution)} 个"
        )

    for index, circle in enumerate(solution):
        if (
                not isinstance(circle, (list, tuple))
                or len(circle) != 3
        ):
            return invalid(f"第 {index} 个圆必须是 [x, y, radius]")

        for value in circle:
            if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
            ):
                return invalid(f"第 {index} 个圆包含非数值数据")

            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False

            if not finite:
                return invalid(f"第 {index} 个圆包含非有限数值")

        if circle[2] <= 0:
            return invalid(f"第 {index} 个圆的半径必须为正")

        # 明显越出单位尺度的输入直接拒绝，避免有限大数在后续加法中溢出。
        if any(abs(value) > 2 for value in circle):
            return invalid(f"第 {index} 个圆的坐标或半径明显超出单位正方形范围")

    boundary_violation = 0.0
    overlap_violation = 0.0

    # 检查正方形边界。
    for x, y, radius in solution:
        boundary_violation = max(
            boundary_violation,
            radius - x,
            radius - y,
            x + radius - 1,
            y + radius - 1,
        )

    # 检查圆之间的重叠。
    for index, (x1, y1, r1) in enumerate(solution):
        for x2, y2, r2 in solution[index + 1:]:
            distance = math.hypot(x1 - x2, y1 - y2)

            overlap_violation = max(
                overlap_violation,
                r1 + r2 - distance,
            )

    max_violation = max(boundary_violation, overlap_violation)
    constraints = {
        "boundary": boundary_violation - TOLERANCE,
        "non_overlap": overlap_violation - TOLERANCE,
    }
    if max_violation > TOLERANCE:
        return invalid(
            f"布局存在越界或重叠，最大违反量为 {max_violation:.12g}",
            metrics={"max_violation": max_violation},
            constraints=constraints,
        )

    score = math.fsum(circle[2] for circle in solution)
    mean_radius = score / NUM_CIRCLES
    # 先按均值归一化，避免极小半径平方时下溢。
    radius_cv = math.sqrt(math.fsum(
        (circle[2] / mean_radius - 1) ** 2 for circle in solution
    ) / NUM_CIRCLES)
    near_boundary = sum(
        min(abs(x - radius), abs(y - radius),
            abs(1 - x - radius), abs(1 - y - radius)) <= BOUNDARY_CONTACT_TOLERANCE
        for x, y, radius in solution
    )

    return Evaluation(
        candidate_id=candidate_id,
        status="ok",
        score=score,
        metrics={
            "sum_radii": score,
            "max_violation": max_violation,
        },
        feedback="布局通过合法性检查",
        objectives={"sum_radii": score},
        constraints=constraints,
        features={
            "radius_cv": radius_cv,
            "boundary_fraction": near_boundary / NUM_CIRCLES,
        },
    )
