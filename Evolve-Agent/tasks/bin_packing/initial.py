"""在线一维装箱；只进化每一步的箱子选择启发式。"""

# EVOLVE-BLOCK-START
def choose_bin(item, remaining):
    for index, space in enumerate(remaining):
        if space >= item:
            return index
    return len(remaining)
# EVOLVE-BLOCK-END


def solve():
    # 种子和实例为公开基准，不宣称隐藏测试；所有策略共用这些整数实例。
    import random
    datasets = []
    for seed in range(12):
        rng = random.Random(seed)
        if seed % 3 == 0:
            items = [rng.randint(10, 90) for _ in range(120)]
        elif seed % 3 == 1:
            items = [rng.choice([20, 30, 40, 60, 70, 80]) for _ in range(120)]
        else:
            items = [rng.randint(25, 50) for _ in range(120)]
        remaining, assignments = [], []
        for item in items:
            index = choose_bin(item, tuple(remaining))
            if type(index) is not int or index < 0 or index > len(remaining):
                raise ValueError("choose_bin must return an existing or one new bin index")
            if index == len(remaining):
                remaining.append(100)
            remaining[index] -= item
            assignments.append(index)
        datasets.append(assignments)
    return datasets
