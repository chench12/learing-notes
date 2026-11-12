from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4


@dataclass(frozen=True)
class ExecutionLimits:
    """执行后端及资源设置；docker 必须使用本地已存在的固定镜像 ID。"""

    backend: str = "local-reviewed"
    docker_image: str | None = None
    startup_timeout_seconds: float = 30.0
    memory_mb: int = 512
    max_processes: int = 8
    max_log_bytes: int = 1_000_000
    max_solution_bytes: int = 5_000_000
    max_workdir_bytes: int = 10_000_000
    evaluator_timeout_seconds: float = 5.0

    def __post_init__(self):
        if self.backend not in ("local-reviewed", "docker"):
            raise ValueError("backend 必须为 local-reviewed 或 docker")
        if self.backend == "docker":
            if not isinstance(self.docker_image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.docker_image):
                raise ValueError("docker_image 必须为 sha256: 加 64 位小写十六进制镜像 ID")
        elif self.docker_image is not None:
            raise ValueError("本地后端的 docker_image 必须为 null")
        for name in ["memory_mb", "max_processes", "max_log_bytes",
                     "max_solution_bytes", "max_workdir_bytes"]:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须为正整数")
        for name in ("evaluator_timeout_seconds", "startup_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须为有限正数")


@dataclass(frozen=True)
class TaskSpec:
    """任务的固定配置。"""
    task_id: str
    description: str
    initial_program: Path
    evaluator: Path
    timeout_seconds: float = 30.0

    def __post_init__(self):
        if not self.task_id.strip():
            raise ValueError("task_id 不能为空")

        if (
                not isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0
        ):
            raise ValueError("超时时间必须是有限正数")

@dataclass(frozen=True)
class Candidate:
    """模型生成或用户提供的一份候选程序。"""
    task_id: str
    code: str
    parent_id: str | None = None
    candidate_id: str = field(
        default_factory=lambda: uuid4().hex
    )

EvaluationStatus = Literal[
    "ok",
    "invalid",
    "error",
    "timeout",
]

@dataclass(frozen=True)
class Evaluation:
    """外部评测器对候选程序的评价。"""
    candidate_id: str
    status: EvaluationStatus
    score: float | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    feedback: str = ""
    objectives: dict[str, float] = field(default_factory=dict)
    # 有符号约束残差：每个值 <= 0 表示满足相应约束。
    constraints: dict[str, float] = field(default_factory=dict)
    features: dict[str, float] = field(default_factory=dict)
    stage: str = "standard"

    def __post_init__(self):
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ValueError("评测必须包含非空 candidate_id")
        if not isinstance(self.stage, str) or not self.stage.strip():
            raise ValueError("评测必须包含非空阶段标识")
        if not isinstance(self.feedback, str):
            raise ValueError("feedback 必须为字符串")
        if not isinstance(self.status, str) or self.status not in {
            "ok", "invalid", "error", "timeout"
        }:
            raise ValueError("未知的评测状态")

        if self.status == "ok":
            if not _finite_number(self.score):
                raise ValueError("成功评测必须提供有限分数")
        elif self.score is not None:
            raise ValueError("失败候选不能拥有有效分数")

        for name in ("metrics", "objectives", "constraints", "features"):
            values = getattr(self, name)
            if not isinstance(values, dict) or not all(
                isinstance(key, str) and key.strip() and _finite_number(value)
                for key, value in values.items()
            ):
                raise ValueError(f"{name} 必须使用非空字符串名称和有限数值（不允许 bool）")
        if self.status == "ok" and any(value > 0 for value in self.constraints.values()):
            raise ValueError("成功评测的所有约束残差必须 <= 0")
        if self.status != "ok" and (self.objectives or self.features):
            raise ValueError("失败候选不能拥有有效优化目标或档案特征")


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False
