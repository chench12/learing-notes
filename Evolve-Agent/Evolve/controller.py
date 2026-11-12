"""串行、可停止/恢复的有限预算控制器；不自动重发未知调用。"""

from time import perf_counter

from Evolve.budget import BudgetExceeded, check_budget, evaluation_reservation, usage_snapshot
from Evolve.experiment import new_id
from Evolve.generation import TERMINAL_FAILURES, generate_once, process_saved_response
from Evolve.meta_prompts import maybe_evolve_template, template_credit
from Evolve.recovery import recover_experiment
from Evolve.routing import ModelPool
from Evolve.session import now
from Evolve.storage import encode_json


def save_control(session, record):
    session.storage.put("control", record["control_id"], record, {
        f"controls/{record['control_id']}/control.json": encode_json(record),
    })


def confirm_generation(session, record):
    config = session.config["confirmation"]
    if not config["enabled"] or record["state"]["status"] != "completed":
        return None
    state, plan = record["state"], record["plan"]
    if state.get("confirmation_state") in {"completed", "skipped"}:
        return None
    evaluation = session.storage.get("evaluation", state["evaluation_attempt_id"])
    result, baseline = evaluation["result"], plan.get("best_score_before")
    qualifies = result["status"] == "ok" and (baseline is None or result["score"] > baseline + config["min_improvement"])
    if not qualifies:
        state["confirmation_state"] = "skipped"
        session.save_generation(record)
        return None
    group_id = state.get("confirmation_group_id")
    group = session.storage.get("group", group_id) if group_id else None
    if group is not None:
        if group["state"] != "completed":
            raise RuntimeError("复核组未完成，保留已有结果；不会隐式补跑或新建重复组")
        state["confirmation_state"] = "completed"
        session.save_generation(record)
        return group_id
    check_budget(session, evaluation_seconds=config["repeats"] * evaluation_reservation(session))
    group_id = group_id or new_id("group")
    state.update(confirmation_state="planned", confirmation_group_id=group_id)
    session.save_generation(record)
    candidate = session.candidates()[state["candidate_id"]]
    identifier, summary = session.evaluate_repeated(candidate, repeats=config["repeats"], group_id=group_id)
    state["confirmation_state"] = "completed"
    session.save_generation(record)
    return identifier


def run_search(session, model=None, *, steps=None, reviewed_fixtures=False, stop_requested=None):
    """steps 是本次启动的上限；实验总预算仍由冻结配置决定。

    reviewed_fixtures 只用于可信手写离线程序；真实自动生成要求 Docker。
    """
    if steps is not None and (type(steps) is not int or steps <= 0):
        raise ValueError("steps 必须为正整数")
    if session.config["pipeline"]["mode"] != "serial":
        raise ValueError("异步实验请使用 run_search_async 入口")
    if reviewed_fixtures and (model is None or isinstance(model, ModelPool)):
        raise ValueError("手写离线模式需要显式的本地响应夹具")
    if not reviewed_fixtures:
        if session.config["execution"]["backend"] != "docker":
            raise RuntimeError("真实自动循环要求 Docker；local-reviewed 继续使用人工审查的单次入口")
        from Evolve.docker_execution import inspect_image, inspect_runtime
        inspect_runtime()
        inspect_image(session.config["execution"]["docker_image"])
    model = model or ModelPool()
    with session.storage.operation():
        recover_experiment(session)
        unresolved = [identifier for identifier, call in session.storage.all("call") if call["state"] in {"requesting", "uncertain"}]
        if unresolved:
            raise RuntimeError("存在结果未知的模型调用，停止自动续跑；恢复不会重发")
        record = {"control_id": new_id("control"), "state": "running", "started_at_utc": now(),
                  "steps_limit": steps, "new_generations": 0,
                  "model_mode": "handwritten-fixtures" if reviewed_fixtures else "configured-remote"}
        save_control(session, record)
        started = perf_counter()
        reason = "step_limit"
        try:
            # 先完成已保存响应及尚未开始的确认，再允许新请求。
            for identifier, generation in session.storage.all("generation"):
                status = generation["state"]["status"]
                if status not in TERMINAL_FAILURES | {"completed"}:
                    if "response" not in generation:
                        raise RuntimeError("遗留生成无可恢复响应，停止自动循环")
                    process_saved_response(session, identifier)
                confirm_generation(session, session.storage.get("generation", identifier))
            while steps is None or record["new_generations"] < steps:
                if stop_requested is not None and stop_requested():
                    reason = "stop_requested"
                    break
                if session.generation_count() >= session.config["search"]["max_generations"]:
                    reason = "generation_budget"
                    break
                check_budget(session)
                maybe_evolve_template(session, model)
                count_before = session.generation_count()
                try:
                    generate_once(session, model)
                except (ValueError, RuntimeError) as exc:
                    new_records = [item for _, item in session.storage.all("generation")]
                    if session.generation_count() == count_before or not new_records:
                        raise
                    # 仅明确的格式/语法/骨架失败允许下轮；未知请求/执行故障停止。
                    latest = max(new_records, key=lambda item: item["plan"]["created_at_utc"])
                    if latest["state"]["status"] not in TERMINAL_FAILURES:
                        raise
                    if latest["state"]["status"] == "api_error":
                        raise RuntimeError("提供方明确拒绝请求，停止循环，请检查模型/认证配置") from None
                    if isinstance(exc, BudgetExceeded):
                        raise
                record["new_generations"] += session.generation_count() - count_before
                for _, generation in session.storage.all("generation"):
                    confirm_generation(session, generation)
                save_control(session, record)
        except BudgetExceeded as exc:
            reason = "budget_exhausted"
            record["detail"] = str(exc)
        except BaseException as exc:
            record.update(state="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                          error_type=type(exc).__name__, elapsed_seconds=perf_counter() - started,
                          finished_at_utc=now(), usage=usage_snapshot(session))
            save_control(session, record)
            raise
        session.sync_archive()
        record.update(state="stopped", stop_reason=reason, elapsed_seconds=perf_counter() - started,
                      finished_at_utc=now(), usage=usage_snapshot(session), template_credit=template_credit(session))
        save_control(session, record)
        return record
