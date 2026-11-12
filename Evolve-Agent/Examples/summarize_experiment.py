"""从已保存记录导出实验摘要、最佳程序和对应布局，不调用模型或执行候选。"""

import argparse
from pathlib import Path

from Examples.experiment_output import write_overview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / args.experiment
    summary = write_overview(directory)
    print("摘要：", directory / "README.md")
    print("搜索最佳：", summary["best"]["score"] if summary["best"] else None)
    print("用量：", summary["usage"])


if __name__ == "__main__":
    main()
