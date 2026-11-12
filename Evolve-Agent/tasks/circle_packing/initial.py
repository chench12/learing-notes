"""圆装填：固定调用接口，可进化初始化与优化算法。"""

# EVOLVE-BLOCK-START
import math
import random
import time
# EVOLVE-BLOCK-END


# EVOLVE-BLOCK-START
def initialize():
    """从 6×6 网格中取 26 个位置，放置相同大小的圆。"""
    circles = []

    for index in range(26):
        column = index % 6
        row = index // 6

        x = (column + 0.5) / 6
        y = (row + 0.5) / 6
        radius = 1 / 12

        circles.append([x, y, radius])

    return circles


def optimize(circles):
    """初始算法不进行优化；模型可以重写或增加辅助函数。"""
    return circles
# EVOLVE-BLOCK-END


def solve():
    return optimize(initialize())
