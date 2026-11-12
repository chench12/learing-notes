"""只读检查档案；不执行候选、不请求模型、不推进迁移。"""

import argparse
import json
from pathlib import Path

from Evolve.session import ExperimentSession


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--json", action="store_true", help="输出完整重建视图")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    session = ExperimentSession(project / args.experiment, project)
    snapshot = session.archive_snapshot()
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False))
        return
    best = session.best()
    print("全局最佳：", (best[0].candidate_id, best[1].score) if best else None)
    print("策略：", session.config["archive"]["strategy"])
    for island in snapshot["islands"]:
        print(f"岛屿 {island['island_id']}：{len(island['cells'])} 个非空格")
        for member in island["cells"]:
            print(" ", member["cell"], member["candidate_id"], "分数：", member["score"])
    print("迁移轮数：", len(snapshot["migrations"]))
    print("完整档案导出：", session.directory / "archives/current.json")
    cached = session.storage.get("archive", "current")
    if cached is None or cached["snapshot"] != snapshot:
        print("导出尚未同步；上面展示的是事实重建结果。可运行 recover_experiment --apply 同步。")


if __name__ == "__main__":
    main()
