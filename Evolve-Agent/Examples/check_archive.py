"""四次手写响应的离线演示，验证多样性保留及一次环形迁移。"""

from pathlib import Path

from Evolve.blocks import parse_blocks
from Examples.experiment_output import create_development_experiment as create_experiment
from Evolve.generation import generate_once
from Evolve.selection import sample_parent
from Evolve.session import ExperimentSession


class FixtureModel:
    """本地响应夹具；没有网络客户端，也没有模型搜索能力。"""

    def __init__(self, text):
        self.text = text

    def generate(self, **kwargs):
        return {"text": self.text, "finish_reason": "stop", "model": "handwritten-fixture",
                "usage": {"total_tokens": 0}}


def demo_programs(initial):
    small = initial.replace("radius = 1 / 12", "radius = 1 / 13")
    high_algorithm = (
        "def initialize():\n"
        "    circles = [[(i % 5 + 0.5) / 5, (i // 5 + 0.5) / 5, 0.1]\n"
        "               for i in range(25)]\n"
        "    circles.append([0.2, 0.2, (math.sqrt(2) - 1) / 10])\n"
        "    return circles\n\n"
        "def optimize(circles):\n    return circles\n"
    )
    high = initial.replace(parse_blocks(initial).editable[1], high_algorithm)
    unequal = small.replace("radius = 1 / 13", "radius = 1 / 24 if index == 0 else 1 / 13")
    return [small, high, unequal, initial.replace("radius = 1 / 12", "radius = 1 / 14")]


def run_demo(project):
    directory, _ = create_experiment("configs/circle_packing.json", project)
    session = ExperimentSession(directory, project)
    initial = session.task.initial_program.read_text(encoding="utf-8")
    children = []
    for program in demo_programs(initial):
        search = parse_blocks(initial).editable[1]
        replacement = parse_blocks(program).editable[1]
        response = f"<<<<<<< SEARCH\n{search}=======\n{replacement}>>>>>>> REPLACE\n"
        children.append(generate_once(session, FixtureModel(response))[0])
    snapshot = session.archive_snapshot()
    ids = lambda island: {member["candidate_id"] for member in snapshot["islands"][island]["cells"]}
    assert children[0].candidate_id in ids(0)  # 分数 2.0，因不同格保留。
    assert children[1].candidate_id in ids(1) & ids(2)  # 岛1的高分程序迁移到岛2。
    assert children[1].candidate_id not in ids(0) | ids(3)  # 同轮不连续传递。
    assert session.best()[0] == children[1]
    assert len(snapshot["migrations"]) == 1
    # 对冻结档案做抽样演示；这些只是选择查询，不执行额外代数。
    draws = [sample_parent(snapshot, seed=42, selection_index=i)["parent_id"] for i in range(40)]
    assert children[0].candidate_id in draws
    assert session.storage.get("archive", "current")["snapshot"] == snapshot
    counts = session.storage.stats()
    restored = ExperimentSession(directory, project)
    assert restored.archive_snapshot() == snapshot
    restored.sync_archive()
    assert restored.storage.stats() == counts
    return directory, snapshot


def main():
    print("离线演示：4 份人工编写的补丁；真实模型/API 调用为 0。")
    project = Path(__file__).resolve().parents[1]
    directory, snapshot = run_demo(project)
    for island in snapshot["islands"]:
        print("岛屿", island["island_id"], "格子/分数：",
              [(member["cell"], round(member["score"], 6)) for member in island["cells"]])
    print("迁移轮数：", len(snapshot["migrations"]))
    print("低分不同格保留、单跳迁移、抽样及重放一致性均通过。")
    print("实验目录：", directory)
    print("档案：", directory / "archives/current.json")
    print("迁移记录：", directory / "archives/migrations.json")
    print("手写布局仅验证框架，不代表模型发现了改进，也不代表该策略优于贪心。")


if __name__ == "__main__":
    main()
