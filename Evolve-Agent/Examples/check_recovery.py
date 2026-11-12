"""用预设响应模拟生成中断，再离线恢复；不会调用模型服务。"""

from pathlib import Path
from unittest.mock import patch

from Examples.experiment_output import create_development_experiment as create_experiment
from Evolve.generation import generate_once, process_saved_response
from Evolve.recovery import recovery_report
from Evolve.session import ExperimentSession


class OfflineModel:
    def __init__(self):
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        return {
            "text": "<<<<<<< SEARCH\n        radius = 1 / 12\n=======\n        radius = 1 / 13\n>>>>>>> REPLACE\n",
            "finish_reason": "stop", "model": "offline-fixture", "usage": {"total_tokens": 0},
        }


def main():
    print("离线演示：只读取预设响应，不连接模型服务。")
    project = Path(__file__).resolve().parents[1]
    directory, _ = create_experiment("configs/circle_packing.json", project)
    session = ExperimentSession(directory, project)
    model = OfflineModel()
    try:
        with patch("Evolve.generation.process_saved_response", side_effect=KeyboardInterrupt):
            generate_once(session, model)
    except KeyboardInterrupt:
        print("已模拟：响应提交后、子代构造前中断。")
    restored = ExperimentSession(directory, project)
    identifier = recovery_report(restored)["generations"][0]["generation_id"]
    candidate, result = process_saved_response(restored, identifier)
    # 重复恢复完成尝试应当没有新评测或新记录。
    counts = restored.storage.stats()
    process_saved_response(restored, identifier)
    assert restored.storage.stats() == counts
    assert model.calls == 1
    print("实验目录：", directory)
    print("SQLite：", restored.storage.path)
    print("生成尝试：", identifier)
    print("预设模型响应读取次数：", model.calls, "（真实 API 调用为 0）")
    print("恢复后的候选：", candidate.candidate_id, result.status, result.score)
    print("数据库统计：", counts)
    print("本例缩小半径只用于验证恢复流程，不代表搜索改进。")


if __name__ == "__main__":
    main()
