"""创建实验清单与源代码快照，不调用模型或执行候选。"""

import hashlib
import json
import math
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import platform
import sys
from uuid import uuid4

from Evolve.loader import load_task
from Evolve.contracts import ExecutionLimits
from Evolve.storage import Storage
from Evolve.archive import validate_archive_config
from Evolve.settings import DEFAULTS, expanded_config, validate_extensions


def new_id(kind):
    return f"{kind}_{uuid4().hex}"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def environment_info():
    packages = {}
    for package in ["openai", "httpx"]:
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = None
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": packages,
    }


def _validate_config(config):
    config = expanded_config(config)
    fields = {
        "task": {"task_id", "directory", "timeout_seconds"},
        "model": {"base_url", "model", "key_env", "max_tokens"},
        "search": {"seed", "max_generations", "generation_mode"},
        "archive": {"strategy", "islands", "axes", "cell_capacity", "island_selection", "parent_sampling", "seed_assignment", "tie_break", "migration_interval", "migration_count", "migration_topology", "restart", "temperature_initial", "temperature_period", "puct_c"},
        "execution": set(ExecutionLimits.__dataclass_fields__),
    }
    fields.update({name: set(values) for name, values in DEFAULTS.items()})
    if not isinstance(config, dict) or set(config) != set(fields):
        raise ValueError("配置必须包含且仅包含 task、model、search、execution、archive")

    for section, expected in fields.items():
        values = config[section]
        if not isinstance(values, dict) or set(values) != expected:
            raise ValueError(f"{section} 配置字段与约定不一致")

    ExecutionLimits(**config["execution"])

    for section, names in {
        "task": ["task_id", "directory"],
        "model": ["base_url", "model", "key_env"],
    }.items():
        for name in names:
            value = config[section][name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{section}.{name} 必须为非空字符串")

    for section, name in [
        ("model", "max_tokens"),
        ("search", "max_generations"),
    ]:
        value = config[section][name]
        if type(value) is not int or value <= 0:
            raise ValueError(f"{section}.{name} 必须为正整数")

    if type(config["search"]["seed"]) is not int:
        raise ValueError("search.seed 必须为整数")
    if config["search"]["generation_mode"] not in ("patch", "rewrite"):
        raise ValueError("search.generation_mode 必须为 patch 或 rewrite")
    validate_archive_config(config["archive"])
    validate_extensions(config)

    timeout = config["task"]["timeout_seconds"]
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("task.timeout_seconds 必须为有限正数")


def create_experiment(config_path, project_root):
    """创建独立实验目录；相对配置路径以项目根目录为基准。"""
    project_root = Path(project_root).resolve()
    config_path = Path(config_path)
    if not config_path.is_absolute():
        config_path = project_root / config_path

    config = expanded_config(json.loads(config_path.read_text(encoding="utf-8")))
    _validate_config(config)
    task_config = config["task"]
    task_dir = (project_root / task_config["directory"]).resolve()

    task = load_task(
        task_dir,
        task_config["task_id"],
        task_config["timeout_seconds"],
    )

    paths = {
        "task/description.md": task_dir / "description.md",
        "task/initial.py": task.initial_program,
        "task/evaluator.py": task.evaluator,
    }
    if (task_dir / "references.md").is_file():
        paths["task/references.md"] = task_dir / "references.md"
    for name in [
        "contracts", "loader", "runner", "worker",
        "prompts", "llm", "database", "experiment",
        "session", "generation",
        "blocks",
        "patches",
        "execution", "process_group", "evaluator_worker",
        "docker_execution", "container_worker",
        "evaluation",
        "storage", "recovery", "archive", "selection", "settings", "context", "routing", "budget", "calls", "controller", "meta_prompts", "batch", "async_controller",
    ]:
        paths[f"framework/{name}.py"] = (
            project_root / "Evolve" / f"{name}.py"
        )
    paths["examples/generate_candidate.py"] = (
        project_root / "Examples" / "generate_candidate.py"
    )

    # 先一次性读入，用相同字节计算哈希并写快照。
    contents = {name: path.read_bytes() for name, path in paths.items()}
    hashes = {name: digest(data) for name, data in contents.items()}

    identity = {
        "schema_version": 9,
        "config": config,
        "files": hashes,
        "environment": environment_info(),
    }
    encoded = json.dumps(
        identity, sort_keys=True, ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")

    experiment_id = new_id("exp")
    directory = project_root / "experiments" / experiment_id
    directory.mkdir(parents=True, exist_ok=False)

    for name, data in contents.items():
        target = directory / "snapshot" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    for name in ["candidates", "generations", "evaluations", "evaluation_groups", "archives", "calls", "controls", "meta_prompts"]:
        (directory / name).mkdir()

    manifest = {
        **identity,
        "experiment_id": experiment_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "fingerprint": digest(encoded),
    }
    # 清单与 SQLite 身份都创建成功后，实验才可打开。
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    Storage(directory, experiment_id, manifest["fingerprint"], create=True)
    return directory, manifest
