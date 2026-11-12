"""恢复事实与状态；不调用模型，不执行候选，不重发未知请求。"""

from dataclasses import asdict
import json

from Evolve.contracts import Evaluation
from Evolve.evaluation import summarize_repeats
from Evolve.experiment import digest
from Evolve.session import now
from Evolve.storage import encode_json
from Evolve.calls import save_call


def recovery_report(session):
    generations = []
    for identifier, record in session.storage.all("generation"):
        state = record["state"]["status"]
        action = "terminal"
        if "response" in record and state not in {"completed", "incomplete", "parse_error", "patch_error", "syntax_error", "skeleton_error", "api_error", "budget_stopped", "not_sent"}:
            action = "resume_saved_response"
        elif state == "requesting":
            action = "mark_uncertain_no_resend"
        elif state == "uncertain":
            action = "unknown_provider_outcome_no_resend"
        elif state == "planned":
            action = "not_sent_no_automatic_request"
        generations.append({"generation_id": identifier, "state": state, "action": action})
    return {
        "experiment_id": session.experiment_id, "counts": session.storage.stats(),
        "generations": generations,
        "running_evaluations": [identifier for identifier, item in session.storage.all("evaluation") if item["state"] == "running"],
        "running_groups": [identifier for identifier, item in session.storage.all("group") if item["state"] == "running"],
        "unknown_calls": [identifier for identifier, item in session.storage.all("call") if item["state"] in {"requesting", "uncertain"}],
        "unfinished_controls": [identifier for identifier, item in session.storage.all("control") if item["state"] == "running"],
        "orphan_directories": [
            path.relative_to(session.directory).as_posix()
            for kind, folder in [("candidate", "candidates"), ("generation", "generations"), ("evaluation", "evaluations"), ("group", "evaluation_groups")]
            for path in sorted((session.directory / folder).iterdir())
            if path.is_dir() and session.storage.get(kind, path.name) is None
        ],
    }


def recover_experiment(session):
    with session.storage.operation():
        # 统一调用账本中已提交响应可以补齐生成记录，无需再次请求。
        for identifier, record in session.storage.all("generation"):
            call = session.storage.get("call", record["plan"].get("call_id", "missing"))
            if call and call["state"] == "completed" and "response" not in record:
                if call["plan"]["owner_id"] != identifier or call["plan"]["purpose"] != "program":
                    raise ValueError("恢复调用与生成身份不一致")
                record["response"] = call["response"]
                record["state"].update(status="response_received", recovery_reason="call_response_recovered", updated_at_utc=now())
                session.save_generation(record)
        for _, call in session.storage.all("call"):
            if call["state"] == "requesting":
                call.update(state="uncertain", recovery_reason="provider_outcome_unknown")
                save_call(session, call)
        from Evolve.meta_prompts import apply_meta_response
        for _, meta in session.storage.all("meta"):
            apply_meta_response(session, meta)
        # 中断启动按开始至恢复的时间保守记账，停机时间可能被多算，不会漏算。
        from datetime import datetime, timezone
        for identifier, control in session.storage.all("control"):
            if control["state"] == "running":
                control.update(state="interrupted", finished_at_utc=now(),
                    elapsed_seconds=max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(control["started_at_utc"])).total_seconds()))
                session.storage.put("control", identifier, control, {f"controls/{identifier}/control.json": encode_json(control)})
        # 只在取得单实验锁后判定 running 为遗留状态，避免干扰活跃操作。
        for identifier, record in session.storage.all("evaluation"):
            if record["state"] != "running":
                continue
            execution = session.directory / "evaluations" / identifier / "artifacts/execution.json"
            result = None
            if execution.is_file():
                try:
                    data = json.loads(execution.read_text(encoding="utf-8"))
                    candidate = session.candidates()[record["candidate_id"]]
                    if (data["candidate_id"] != candidate.candidate_id
                            or data["source_sha256"] != digest(candidate.code.encode("utf-8"))
                            or data["evaluator_sha256"] != digest(session.task.evaluator.read_bytes())
                            or data["candidate_timeout_seconds"] != session.task.timeout_seconds
                            or data["limits"] != session.config["execution"]):
                        raise ValueError("执行产物身份不一致")
                    if data.get("result") is not None:
                        result = Evaluation(**data["result"])
                        if result.candidate_id != candidate.candidate_id or result.stage != record["stage"]:
                            raise ValueError("执行产物的评测结果不一致")
                        if result.status in ("ok", "invalid"):
                            for name in ("candidate", "evaluator"):
                                phase = data["phases"][name]
                                if (phase["status"] != "exited" or phase["returncode"] != 0
                                        or phase.get("cleanup_succeeded") is False):
                                    raise ValueError("执行阶段尚未正常完成")
                            for name, filename in [("solution", "solution.json"), ("evaluator_result", "evaluator_result.json")]:
                                saved = data[name]
                                if (saved.get("file") != filename or not saved.get("complete")
                                        or digest((execution.parent / filename).read_bytes()) != saved["sha256"]):
                                    raise ValueError("执行产物不完整或哈希不一致")
                            verified = Evaluation(**json.loads((execution.parent / "evaluator_result.json").read_text(encoding="utf-8")))
                            if verified != result:
                                raise ValueError("评测器输出与执行记录不一致")
                except (KeyError, ValueError, TypeError, OSError):
                    # 不根据不完整或身份不符的临时文件发明成绩。
                    result = None
            record.update(state="completed" if result else "interrupted", finished_at_utc=now(),
                          recovery_reason="runner_result_recovered" if result else "operation_ended_without_committed_result")
            if result:
                record["result"] = asdict(result)
            session.save_evaluation(record)

        for identifier, record in session.storage.all("generation"):
            state = record["state"]["status"]
            if state == "requesting" and "response" not in record:
                record["state"].update(status="uncertain", updated_at_utc=now(), recovery_reason="provider_outcome_unknown")
                session.save_generation(record)
            elif state in {"evaluating", "evaluation_interrupted"}:
                evaluation_id = record["state"].get("evaluation_attempt_id")
                evaluation = session.storage.get("evaluation", evaluation_id) if evaluation_id else None
                if evaluation:
                    target_state = "completed" if evaluation["state"] == "completed" else "evaluation_interrupted"
                    if state == target_state:
                        continue
                    record["state"].update(
                        status=target_state,
                        updated_at_utc=now(), recovery_reason="evaluation_state_reconciled",
                    )
                    session.save_generation(record)

        for identifier, record in session.storage.all("group"):
            if record["state"] != "running":
                continue
            plan = record["plan"]
            rows = [item for _, item in session.storage.all("evaluation") if item.get("evaluation_group_id") == identifier and item["state"] == "completed"]
            rows.sort(key=lambda item: item["repeat_index"])
            attempts = [(item["evaluation_attempt_id"], Evaluation(**item["result"])) for item in rows]
            summary = summarize_repeats(plan["candidate_id"], attempts, plan["planned_repeats"])
            state = "completed" if summary["complete"] else "interrupted"
            best = summary["best_evaluation_attempt_id"]
            prefix = f"evaluation_groups/{identifier}"
            files = {}
            solution = None
            if best:
                source = session.directory / "evaluations" / best / "artifacts/solution.json"
                data = source.read_bytes()
                filename = f"best_solution_{best}.json"
                files[f"{prefix}/{filename}"] = data
                solution = {"file": filename, "sha256": digest(data), "evaluation_attempt_id": best}
            summary.update(experiment_id=session.experiment_id, evaluation_group_id=identifier,
                           purpose="confirmation", state=state, updated_at_utc=now(), best_solution=solution,
                           recovery_reason="rebuilt_from_committed_evaluations")
            if state != "completed":
                summary["all_valid_median"] = None
            record.update(state=state, summary=summary)
            files[f"{prefix}/summary.json"] = encode_json(summary)
            session.storage.put("group", identifier, record, files)
        session.sync_archive()
        return recovery_report(session)
