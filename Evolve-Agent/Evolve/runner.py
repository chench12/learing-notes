"""候选和固定评测器的分阶段执行，保留有界产物。

本地后端不隔离文件系统或网络，适用于人工审查后的单次运行。
"""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from Evolve.contracts import Candidate, Evaluation, ExecutionLimits, TaskSpec
from Evolve.execution import run_process
from Evolve.docker_execution import run_docker_process


def _save_output(source, folder, name, maximum, *, truncated=False):
    """超大文件仅保存前缀，明确命名为 partial，不能当作完整解。"""
    if not source.is_file():
        return {"present": False}
    size = source.stat().st_size
    complete = size <= maximum and not truncated
    target = folder / (name if complete else name + ".partial")
    with source.open("rb") as stream:
        data = stream.read(maximum)
    target.write_bytes(data)
    return {
        "present": True, "file": target.name, "complete": complete,
        "original_bytes": None if truncated else size, "saved_bytes": len(data),
        "observed_bytes_at_least": size + 1 if truncated else size,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _phase_failure(phase, label, log, candidate_id):
    status = phase["status"]
    if status == "timeout":
        return Evaluation(candidate_id, "timeout", feedback=f"{label}超过运行时间限制")
    if status == "resource_limit":
        message = f"{label}触发资源限制：{phase['resource_limit']}"
    elif status == "launch_error":
        message = f"{label}启动/监控失败：{phase.get('error', phase.get('error_type', 'unknown'))}"
    elif status != "exited":
        message = f"{label}执行后端失败：{status}"
    elif phase["returncode"] != 0:
        message = f"{label}退出码：{phase['returncode']}"
    else:
        return None
    if log.is_file():
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 4000))
            message += "\n" + stream.read(4000).decode("utf-8", errors="replace")
    return Evaluation(candidate_id, "error", feedback=message)


def run_candidate(
    task: TaskSpec,
    candidate: Candidate,
    *,
    artifact_dir: Path | None = None,
    limits: ExecutionLimits | None = None,
) -> Evaluation:
    if candidate.task_id != task.task_id:
        raise ValueError("候选的 task_id 与当前任务不一致")
    limits = limits or ExecutionLimits()
    with tempfile.TemporaryDirectory(prefix="evolve-") as directory:
        root = Path(directory)
        folder = Path(artifact_dir).resolve() if artifact_dir is not None else root / "artifacts"
        # 独立目录防止覆盖其他评测的日志/输出。
        folder.mkdir(parents=True, exist_ok=False)
        metadata = {
            "candidate_id": candidate.candidate_id,
            "backend": limits.backend,
            "filesystem_isolated": False,
            "network_isolated": False,
            "environment_policy": "container-explicit-v1" if limits.backend == "docker" else "whitelist-v1",
            "limits": asdict(limits),
            "candidate_timeout_seconds": task.timeout_seconds,
            "source_sha256": hashlib.sha256(candidate.code.encode("utf-8")).hexdigest(),
            "evaluator_sha256": hashlib.sha256(task.evaluator.read_bytes()).hexdigest(),
            "phases": {},
        }
        result = None
        try:
            result = _execute(task, candidate, limits, root, folder, metadata)
            return result
        except Exception as exc:
            result = Evaluation(candidate.candidate_id, "error", feedback=f"{type(exc).__name__}: {exc}")
            return result
        finally:
            metadata["result"] = asdict(result) if result is not None else None
            (folder / "execution.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8",
            )


def _execute(task, candidate, limits, root, folder, metadata):
    worker = Path(__file__).with_name("worker.py").resolve()
    evaluator_worker = worker.with_name("evaluator_worker.py")
    candidate_dir = root / "candidate"
    candidate_dir.mkdir()
    program = candidate_dir / "candidate.py"
    solution = candidate_dir / "solution.json"
    program.write_text(candidate.code, encoding="utf-8")
    log = folder / "candidate.log"
    command = [sys.executable, "-I", "-X", "utf8", str(worker), str(program), str(solution)]
    if limits.backend == "docker":
        phase = run_docker_process(
            {"mode": "candidate", "code": candidate.code},
            log, solution, task.timeout_seconds, limits,
        )
        if phase["runtime"] is not None:
            metadata["filesystem_isolated"] = True
            metadata["network_isolated"] = True
    else:
        phase = run_process(command, candidate_dir, log, solution, task.timeout_seconds, limits)
    metadata["phases"]["candidate"] = phase
    saved = _save_output(solution, folder, "solution.json", limits.max_solution_bytes,
                         truncated=phase.get("output_truncated", False))
    metadata["solution"] = saved
    failure = _phase_failure(phase, "候选程序", log, candidate.candidate_id)
    if failure is not None:
        return failure
    if not saved["present"]:
        return Evaluation(candidate.candidate_id, "error", feedback="候选程序没有输出 solution.json")
    if not saved["complete"]:
        return Evaluation(candidate.candidate_id, "error", feedback="解的 JSON 超过大小限制")

    # 只让评测器消费保存的输出副本；候选进程及其后代已结束。
    evaluator_dir = root / "evaluator"
    evaluator_dir.mkdir()
    output = evaluator_dir / "result.json"
    log = folder / "evaluator.log"
    command = [
        sys.executable, "-I", "-X", "utf8", str(evaluator_worker),
        str(task.evaluator.resolve()), str(folder / "solution.json"),
        str(output), candidate.candidate_id,
    ]
    if limits.backend == "docker":
        phase = run_docker_process({
            "mode": "evaluator",
            "code": task.evaluator.read_text(encoding="utf-8"),
            "contracts": worker.with_name("contracts.py").read_text(encoding="utf-8"),
            "solution_json": (folder / "solution.json").read_text(encoding="utf-8"),
            "candidate_id": candidate.candidate_id,
        }, log, output, limits.evaluator_timeout_seconds, limits)
    else:
        phase = run_process(command, evaluator_dir, log, output, limits.evaluator_timeout_seconds, limits)
    metadata["phases"]["evaluator"] = phase
    saved = _save_output(output, folder, "evaluator_result.json", limits.max_solution_bytes,
                         truncated=phase.get("output_truncated", False))
    metadata["evaluator_result"] = saved
    failure = _phase_failure(phase, "固定评测器", log, candidate.candidate_id)
    if failure is not None:
        return failure
    if not saved["present"] or not saved["complete"]:
        return Evaluation(candidate.candidate_id, "error", feedback="固定评测器缺少完整结果")
    result = Evaluation(**json.loads((folder / "evaluator_result.json").read_text(encoding="utf-8")))
    if result.candidate_id != candidate.candidate_id:
        raise ValueError("评测结果的 candidate_id 不一致")
    if result.stage != "standard":
        raise ValueError("当前执行器仅接受 standard 阶段的固定评测结果")
    return result
