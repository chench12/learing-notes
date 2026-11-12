import json
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    # 父进程先将本进程加入资源组，再允许加载候选代码。
    if sys.stdin.buffer.readline() != b"START\n":
        raise RuntimeError("缺少执行握手")
    program_path = Path(sys.argv[1])
    solution_path = Path(sys.argv[2])

    # 加载候选文件，得到其中定义的函数和变量。
    namespace = runpy.run_path(str(program_path))

    # 执行候选提供的求解函数。
    solution = namespace["solve"]()

    # 只输出解，不在这里计算成绩。
    with solution_path.open("w", encoding="utf-8") as file:
        json.dump(solution, file, allow_nan=False)
