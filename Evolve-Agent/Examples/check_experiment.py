from pathlib import Path

from Examples.experiment_output import create_development_experiment as create_experiment


def main():
    project_root = Path(__file__).resolve().parents[1]
    directory, manifest = create_experiment(
        config_path="configs/circle_packing.json",
        project_root=project_root,
    )

    print("实验编号：", manifest["experiment_id"])
    print("配置指纹：", manifest["fingerprint"])
    print("任务：", manifest["config"]["task"]["task_id"])
    print("模型：", manifest["config"]["model"]["model"])
    print("Python：", manifest["environment"]["executable"])
    print("保存目录：", directory)


if __name__ == "__main__":
    main()
