"""实验内的事务记录及文件导出；单实验操作串行。"""

from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
import json
from pathlib import Path
from time import perf_counter

from Evolve.contracts import Candidate, Evaluation, ExecutionLimits
from Evolve.blocks import validate_skeleton
from Evolve.experiment import digest, environment_info, new_id
from Evolve.evaluation import REPEAT_POLICY, summarize_repeats
from Evolve.archive import rebuild_archive
from Evolve.selection import rebuild_search_statistics, sample_parent
from Evolve.loader import load_task
from Evolve.runner import run_candidate
from Evolve.storage import Storage, encode_json
from Evolve.budget import check_budget, evaluation_reservation


def now():
    return datetime.now(timezone.utc).isoformat()


def serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.storage.operation():
            return method(self, *args, **kwargs)
    return wrapped


class ExperimentSession:
    def __init__(self, directory, project_root):
        self.directory = Path(directory).resolve()
        self.manifest = json.loads(
            (self.directory / "manifest.json").read_text(encoding="utf-8")
        )
        self.experiment_id = self.manifest["experiment_id"]
        self.config = self.manifest["config"]
        if self.manifest.get("schema_version") != 9:
            raise ValueError("此清单属于旧格式，请创建新实验")
        identity = {
            name: self.manifest[name]
            for name in ["schema_version", "config", "files", "environment"]
        }
        encoded = json.dumps(
            identity, sort_keys=True, ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        if digest(encoded) != self.manifest["fingerprint"]:
            raise ValueError("实验清单与指纹不一致，请勿直接修改已有实验配置")
        if self.manifest["environment"] != environment_info():
            raise ValueError("运行环境已改变，请使用原环境或创建新实验")

        # 恢复实验时先检查文件。框架改变后需新建实验。
        project_root = Path(project_root).resolve()
        required = {"framework/session.py", "framework/generation.py"}
        if not required.issubset(self.manifest["files"]):
            raise ValueError("此清单属于旧格式，请创建新实验")
        for name, expected in self.manifest["files"].items():
            snapshot = self.directory / "snapshot" / name
            if digest(snapshot.read_bytes()) != expected:
                raise ValueError(f"实验快照被修改：{name}")
            if name.startswith("framework/"):
                current = project_root / "Evolve" / Path(name).name
                if digest(current.read_bytes()) != expected:
                    raise ValueError(f"框架版本已改变，请新建实验：{name}")

        task_config = self.config["task"]
        self.task = load_task(
            self.directory / "snapshot" / "task",
            task_config["task_id"],
            task_config["timeout_seconds"],
        )
        self.storage = Storage(self.directory, self.experiment_id, self.manifest["fingerprint"])
        with self.storage.operation():
            self.storage.publish_pending()
            self.storage.verify_files()

    @serialized
    def save_candidate(self, candidate, generation_id=None):
        if candidate.task_id != self.task.task_id:
            raise ValueError("候选与实验任务不一致")
        existing = self.storage.get("candidate", candidate.candidate_id)
        if existing:
            if existing["candidate"] != asdict(candidate) or existing["generation_id"] != generation_id:
                raise ValueError("候选编号已对应另一份记录")
            return
        folder = f"candidates/{candidate.candidate_id}"
        record = {
            "experiment_id": self.experiment_id,
            "generation_id": generation_id,
            "created_at_utc": now(),
            "source_sha256": digest(candidate.code.encode("utf-8")),
            "candidate": asdict(candidate),
        }
        self.storage.put("candidate", candidate.candidate_id, record, {
            f"{folder}/candidate.py": candidate.code.encode("utf-8"),
            f"{folder}/candidate.json": encode_json(record),
        })

    @serialized
    def candidates(self):
        records = {}
        for identifier, data in self.storage.all("candidate"):
            path = self.directory / "candidates" / identifier / "candidate.json"
            candidate = Candidate(**data["candidate"])
            if (
                data["experiment_id"] != self.experiment_id
                or candidate.task_id != self.task.task_id
                or path.parent.name != candidate.candidate_id
                or digest(candidate.code.encode("utf-8")) != data["source_sha256"]
                or path.with_name("candidate.py").read_text(encoding="utf-8")
                != candidate.code
            ):
                raise ValueError(f"候选记录不一致：{path}")
            records[candidate.candidate_id] = candidate
        return records

    @serialized
    def begin_evaluation(self, candidate, role, *, purpose="search", group_id=None, repeat_index=None, evaluation_id=None, reservation_generation_id=None):
        if purpose not in ("search", "confirmation"):
            raise ValueError("评测用途必须为 search 或 confirmation")
        if role == "confirmation" and purpose != "confirmation":
            raise ValueError("复核评测必须显式指定 confirmation 用途")
        if self.candidates().get(candidate.candidate_id) != candidate:
            raise ValueError("评测前必须保存匹配的候选记录")
        # 任何进入此实验评测的代码，都必须保留初始任务的骨架。
        validate_skeleton(
            self.task.initial_program.read_text(encoding="utf-8"),
            candidate.code,
        )
        evaluation_id = evaluation_id or new_id("eval")
        existing = self.storage.get("evaluation", evaluation_id)
        if existing:
            identity = (existing["candidate_id"], existing["role"], existing["purpose"], existing["evaluation_group_id"], existing["repeat_index"])
            if identity != (candidate.candidate_id, role, purpose, group_id, repeat_index):
                raise ValueError("评测编号已对应另一份计划")
            if existing["state"] == "completed":
                return existing
            raise ValueError("已有评测尚未完成；请先检查恢复状态，新执行必须使用新评测编号")
        released = 0.0
        if reservation_generation_id is not None:
            generation = self.storage.get("generation", reservation_generation_id)
            if (generation is None or role != "child" or purpose != "search"
                    or generation["plan"]["child_candidate_id"] != candidate.candidate_id
                    or generation["state"].get("evaluation_attempt_id", generation["plan"]["child_evaluation_attempt_id"]) != evaluation_id):
                raise ValueError("子代预算预留身份不一致")
            released = generation["state"].get("reserved_child_evaluation_seconds", 0.0)
        check_budget(self, evaluation_seconds=evaluation_reservation(self), release_evaluation_seconds=released)
        folder = self.directory / "evaluations" / evaluation_id
        folder.mkdir(exist_ok=True)
        record = {
            "experiment_id": self.experiment_id,
            "evaluation_attempt_id": evaluation_id,
            "candidate_id": candidate.candidate_id,
            "role": role,
            "purpose": purpose,
            "stage": "standard",
            "evaluation_group_id": group_id,
            "repeat_index": repeat_index,
            "started_at_utc": now(),
            "state": "running",
        }
        self.save_evaluation(record)
        return record

    @serialized
    def finish_evaluation(self, record, result, elapsed_seconds):
        identifier = record["evaluation_attempt_id"]
        current = self.storage.get("evaluation", identifier)
        if current != record or record["state"] != "running":
            raise ValueError("评测记录已改变，不能提交过期结果")
        if result.candidate_id != record["candidate_id"] or result.stage != record["stage"]:
            raise ValueError("评测结果的候选编号或阶段与本次计划不一致")
        record.update(state="completed", finished_at_utc=now(), elapsed_seconds=elapsed_seconds, result=asdict(result))
        self.save_evaluation(record)
        return identifier, result

    @serialized
    def fail_evaluation(self, record, exc, elapsed_seconds):
        record.update(state="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                      finished_at_utc=now(), elapsed_seconds=elapsed_seconds, error_type=type(exc).__name__)
        self.save_evaluation(record)

    @serialized
    def evaluate(self, candidate, role, *, purpose="search", group_id=None, repeat_index=None, evaluation_id=None, reservation_generation_id=None):
        record = self.begin_evaluation(candidate, role, purpose=purpose, group_id=group_id,
                                       repeat_index=repeat_index, evaluation_id=evaluation_id, reservation_generation_id=reservation_generation_id)
        if record["state"] == "completed":
            return record["evaluation_attempt_id"], Evaluation(**record["result"])
        started = perf_counter()
        try:
            result = run_candidate(self.task, candidate,
                artifact_dir=self.directory / "evaluations" / record["evaluation_attempt_id"] / "artifacts",
                limits=ExecutionLimits(**self.config["execution"]))
            if result.candidate_id != candidate.candidate_id or result.stage != "standard":
                raise ValueError("评测结果的候选编号或阶段与本次计划不一致")
        except BaseException as exc:
            self.fail_evaluation(record, exc, perf_counter() - started)
            raise
        return self.finish_evaluation(record, result, perf_counter() - started)

    def save_evaluation(self, record):
        identifier = record["evaluation_attempt_id"]
        folder = self.directory / "evaluations" / identifier
        files = {}
        artifacts = folder / "artifacts"
        if artifacts.is_dir():
            for item in sorted(artifacts.iterdir()):
                if item.is_file():
                    files[item.relative_to(self.directory).as_posix()] = item.read_bytes()
        if files:
            record["artifacts"] = {name.removeprefix(f"evaluations/{identifier}/"): digest(data) for name, data in files.items()}
        files[f"evaluations/{identifier}/evaluation.json"] = encode_json(record)
        self.storage.put("evaluation", identifier, record, files)
        if record["state"] == "completed" and record["purpose"] == "search":
            self.sync_archive()

    def save_generation(self, record, extra_files=None):
        identifier = record["plan"]["generation_attempt_id"]
        prefix = f"generations/{identifier}"
        files = {f"{prefix}/plan.json": encode_json(record["plan"]),
                 f"{prefix}/state.json": encode_json(record["state"])}
        if "response" in record:
            files[f"{prefix}/response.json"] = encode_json(record["response"])
        for name, value in (extra_files or {}).items():
            files[f"{prefix}/{name}"] = encode_json(value)
        self.storage.put("generation", identifier, record, files)

    @serialized
    def evaluate_repeated(self, candidate, repeats=5, *, progress=None, group_id=None):
        """固定标准预算的独立复核组；不更新父代选择，不自动重试中断的组。"""
        if type(repeats) is not int or repeats <= 0:
            raise ValueError("repeats 必须为正整数")
        if self.candidates().get(candidate.candidate_id) != candidate:
            raise ValueError("重复评测前必须保存匹配的候选记录")
        validate_skeleton(self.task.initial_program.read_text(encoding="utf-8"), candidate.code)
        group_id = group_id or new_id("group")
        existing = self.storage.get("group", group_id)
        if existing is not None:
            if existing["plan"]["candidate_id"] != candidate.candidate_id or existing["plan"]["planned_repeats"] != repeats:
                raise ValueError("复核组身份不一致")
            if existing["state"] == "completed":
                return group_id, existing["summary"]
            raise ValueError("已有复核组未完成；不自动重复执行")
        folder = self.directory / "evaluation_groups" / group_id
        # group_id 可在生成计划中预分配；只容忍提交前留下的空目录。
        folder.mkdir(exist_ok=True)
        if any(folder.iterdir()):
            raise ValueError("复核目录有未提交文件；请先检查孤立产物，不静默覆盖")
        plan = {
            "experiment_id": self.experiment_id, "evaluation_group_id": group_id,
            "candidate_id": candidate.candidate_id, "purpose": "confirmation",
            "stage": "standard", "planned_repeats": repeats,
            "aggregation_policy": REPEAT_POLICY,
            "source_sha256": digest(candidate.code.encode("utf-8")),
            "candidate_timeout_seconds": self.task.timeout_seconds,
            "execution": self.config["execution"], "created_at_utc": now(),
        }
        attempts = []
        best_solution = None
        started = perf_counter()

        def save_summary(state, error_type=None):
            summary = summarize_repeats(candidate.candidate_id, attempts, repeats)
            if state != "completed":
                summary["all_valid_median"] = None
            summary.update({
                "experiment_id": self.experiment_id, "evaluation_group_id": group_id,
                "purpose": "confirmation", "state": state,
                "updated_at_utc": now(), "elapsed_seconds": perf_counter() - started,
                "best_solution": best_solution,
            })
            if error_type:
                summary["error_type"] = error_type
            files = {f"evaluation_groups/{group_id}/plan.json": encode_json(plan),
                     f"evaluation_groups/{group_id}/summary.json": encode_json(summary)}
            if best_solution:
                path = folder / best_solution["file"]
                files[path.relative_to(self.directory).as_posix()] = path.read_bytes()
            self.storage.put("group", group_id, {"plan": plan, "summary": summary, "state": state}, files)
            return summary

        save_summary("running")
        try:
            for index in range(1, repeats + 1):
                identifier, result = self.evaluate(
                    candidate, "confirmation", purpose="confirmation",
                    group_id=group_id, repeat_index=index,
                )
                previous_best = max((item.score for _, item in attempts if item.status == "ok"), default=None)
                attempts.append((identifier, result))
                if result.status == "ok" and (previous_best is None or result.score > previous_best):
                    source = self.directory / "evaluations" / identifier / "artifacts" / "solution.json"
                    data = source.read_bytes()
                    # 单独保存最优合法布局，同时保留它实际对应的评测编号。
                    # 每次改进写新文件，避免中断时旧 summary 的哈希指向被覆盖的数据。
                    target = folder / f"best_solution_{identifier}.json"
                    temporary = folder / ".best_solution.tmp"
                    temporary.write_bytes(data)
                    temporary.replace(target)
                    best_solution = {
                        "file": target.name, "sha256": digest(data),
                        "evaluation_attempt_id": identifier,
                    }
                save_summary("running")
                if progress is not None:
                    progress(index, identifier, result)
        except BaseException as exc:
            # 中断也保存已完成部分；不把剩余次数当失败或成功，不隐式重跑。
            state = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
            save_summary(state, type(exc).__name__)
            raise
        return group_id, save_summary("completed")

    @serialized
    def best(self):
        """全局最佳独立于父代抽样，按最近一次已完成搜索评测排序。"""
        candidates = self.candidates()
        latest = {}
        for record in self._latest_search_records().values():
            identifier = record["evaluation_attempt_id"]
            path = self.directory / "evaluations" / identifier / "evaluation.json"
            if record["experiment_id"] != self.experiment_id:
                raise ValueError(f"评测属于其他实验：{path}")
            if record["state"] != "completed" or record["purpose"] != "search":
                continue
            candidate_id = record["candidate_id"]
            result = Evaluation(**record["result"])
            if candidate_id not in candidates or result.candidate_id != candidate_id:
                raise ValueError(f"评测与候选不一致：{path}")
            if record["stage"] != result.stage:
                raise ValueError(f"评测阶段与计划不一致：{path}")
            if result.stage != "standard":
                continue
            latest[candidate_id] = result
        valid = [
            (candidates[candidate_id], item)
            for candidate_id, item in latest.items()
            if item.status == "ok"
        ]
        return min(valid, key=lambda item: (-item[1].score, item[0].candidate_id), default=None)

    @serialized
    def parent(self):
        return self.select_parent()[0]

    @serialized
    def select_parent(self):
        """选择不推进游标；提交生成计划后 generation_count 才推进一格。"""
        if not self.candidates():
            self._seed()
        snapshot = self.archive_snapshot()
        sampled = sample_parent(snapshot, seed=self.config["search"]["seed"],
                                selection_index=self.generation_count(), config=self.config["archive"],
                                statistics=rebuild_search_statistics(self.storage.events(), self.config["archive"]["islands"]))
        if self.config["archive"]["strategy"] == "greedy":
            selected = self.best()
            parent = selected[0] if selected else self._seed()
            sampled.update(selection_strategy="latest-standard-greedy-v1",
                           sampled_cell=None, historical_evaluation_id=None,
                           random_seed_sha256=None, fallback=None if selected else "seed")
        else:
            parent = self.candidates()[sampled["parent_id"]] if sampled["parent_id"] else self._seed()
            sampled["fallback"] = None if sampled["parent_id"] else "seed"
        sampled["parent_id"] = parent.candidate_id
        return parent, sampled

    def _seed(self):
        seeds = [c for c in self.candidates().values() if c.parent_id is None]
        if seeds:
            return seeds[0]
        candidate = Candidate(
            task_id=self.task.task_id,
            code=self.task.initial_program.read_text(encoding="utf-8"),
        )
        self.save_candidate(candidate)
        return candidate

    def generation_count(self):
        return len(self.storage.all("generation"))

    @serialized
    def archive_snapshot(self):
        return rebuild_archive(self.config["archive"], self.storage.events())

    @serialized
    def sync_archive(self):
        """可丢弃的物化视图：事实先提交，中断后按同一事件序列重建。"""
        snapshot = self.archive_snapshot()
        self.storage.put("archive", "current", {"state": "rebuilt", "snapshot": snapshot}, {
            "archives/current.json": encode_json(snapshot),
            "archives/migrations.json": encode_json(snapshot["migrations"]),
        })
        return snapshot

    def _latest_search_records(self):
        latest, seen = {}, set()
        for event in self.storage.events():
            record, identifier = event["payload"], event["record_id"]
            if (event["kind"] != "evaluation" or record["state"] != "completed"
                    or record["purpose"] != "search" or record["stage"] != "standard"
                    or identifier in seen):
                continue
            seen.add(identifier)
            latest[record["candidate_id"]] = record
        return latest

    def selection_snapshot(self):
        """按提交顺序冻结每个候选的最近已完成搜索成绩，排除复核。"""
        latest = self._latest_search_records()
        return [{
            "candidate_id": identifier, "source_sha256": digest(candidate.code.encode("utf-8")),
            "latest_search_evaluation_id": latest[identifier]["evaluation_attempt_id"] if identifier in latest else None,
            "latest_search_result": latest[identifier]["result"] if identifier in latest else None,
        } for identifier, candidate in self.candidates().items()]
