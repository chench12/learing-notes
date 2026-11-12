"""一次生成和已持久化响应的恢复；恢复入口不接收模型客户端。"""

import ast
from dataclasses import asdict
import re
from uuid import uuid4

from Evolve.contracts import Candidate, Evaluation
from Evolve.blocks import SkeletonError, normalize_source, validate_skeleton
from Evolve.experiment import digest, new_id
from Evolve.context import build_context
from Evolve.routing import select_model
from Evolve.calls import request_model
from Evolve.budget import BudgetExceeded, check_budget, evaluation_reservation
from Evolve.meta_prompts import available_templates
from Evolve.patches import PatchApplyError, PatchParseError, apply_patches, parse_patches
from Evolve.session import now


TERMINAL_FAILURES = {"api_error", "incomplete", "parse_error", "patch_error", "syntax_error", "skeleton_error", "budget_stopped", "not_sent"}


def generation_state(session, record, status, **details):
    record["state"] = {
        **record["state"], "status": status, "updated_at_utc": now(), **details,
    }
    session.save_generation(record)


def prepare_generation(session, parent, sampled, parent_evaluation_id, parent_result, candidate_pool, *, reserve_child=False):
    """单写入者冻结计划；异步入口在请求前保留子代的完整评测额度。"""
    with session.storage.operation():
        if session.generation_count() >= session.config["search"]["max_generations"]:
            raise ValueError("实验已达到 max_generations 限制")
        child_reserve = evaluation_reservation(session) if reserve_child else 0.0
        check_budget(session, evaluation_seconds=child_reserve)
        mode = session.config["search"]["generation_mode"]
        model_config, route = select_model(session.config, session.generation_count())
        messages, context = build_context(session, parent, parent_result, sampled, templates=available_templates(session))
        reserve = check_budget(session, messages=messages, max_tokens=model_config["max_tokens"])
        best = session.best()
        identifier = new_id("gen")
        plan = {
            "experiment_id": session.experiment_id, "generation_attempt_id": identifier,
            "parent_id": parent.candidate_id,
            "parent_source_sha256": digest(parent.code.encode("utf-8")),
            "parent_evaluation_attempt_id": parent_evaluation_id,
            "child_candidate_id": uuid4().hex,
            "child_evaluation_attempt_id": new_id("eval"),
            "selection_strategy": sampled["selection_strategy"],
            "selection": sampled,
            "island_id": sampled["island_id"],
            "candidate_pool": candidate_pool,
            "inspiration_ids": [item["candidate_id"] for item in context["included"]],
            "prompt_template_id": context["template_id"], "context": context,
            "best_score_before": best[1].score if best else None,
            "call_id": new_id("call"), "reservation": reserve, "route": route,
            "search_seed": session.config["search"]["seed"],
            "model": model_config, "generation_mode": mode,
            "messages": messages,
            "created_at_utc": now(),
        }
        record = {"plan": plan, "state": {"generation_attempt_id": identifier, "status": "planned",
                    "reserved_child_evaluation_seconds": child_reserve}}
        session.save_generation(record)
        return record


def generate_once(session, model):
    if session.config["pipeline"]["mode"] != "serial":
        raise ValueError("异步实验请使用 run_search_async 入口")
    with session.storage.operation():
        if session.generation_count() >= session.config["search"]["max_generations"]:
            raise ValueError("实验已达到 max_generations 限制，请创建新实验")
        used = check_budget(session)
        if used["calls"] >= session.config["budget"]["max_calls"]:
            raise BudgetExceeded("已达到模型调用预算")
        session.sync_archive()
        parent, sampled = session.select_parent()
        candidate_pool = session.selection_snapshot()
        print("选择策略：", sampled["selection_strategy"], "岛屿：", sampled["island_id"], "格子：", sampled["sampled_cell"])
        parent_evaluation_id, parent_result = session.evaluate(parent, "parent")
        print("父代编号：", parent.candidate_id)
        print("父代本次状态：", parent_result.status)
        print("父代本次分数：", parent_result.score)
        if parent_result.status != "ok":
            raise RuntimeError(parent_result.feedback)

        record = prepare_generation(session, parent, sampled, parent_evaluation_id, parent_result, candidate_pool)
        plan = record["plan"]
        identifier = plan["generation_attempt_id"]
        generation_state(session, record, "requesting")
        print("生成记录：", session.directory / "generations" / identifier)
        print("正在请求模型……")
        try:
            _, response = request_model(session, model, plan["model"], plan["messages"],
                                        purpose="program", owner_id=identifier, call_id=plan["call_id"])
        except BaseException as exc:
            if isinstance(exc, BudgetExceeded):
                generation_state(session, record, "budget_stopped")
                raise
            # 请求超时/断线/中断后，无法证明服务端没有完成或计费。
            code = getattr(exc, "status_code", None)
            rejected = isinstance(code, int) and 400 <= code < 500 and code not in (408, 429)
            generation_state(session, record, "api_error" if rejected else "uncertain", error_type=type(exc).__name__)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeError(f"模型调用未返回可保存响应，类型：{type(exc).__name__}；不会自动重发") from None
        record["response"] = response
        generation_state(session, record, "response_received")
        print("Token 用量：", response.get("usage"))
        return process_saved_response(session, identifier)


def process_saved_response(session, generation_id, *, reevaluate_interrupted=False, prepare_only=False):
    """离线解析已提交响应；只在确有需要时运行候选，绝不请求 LLM。"""
    with session.storage.operation():
        record = session.storage.get("generation", generation_id)
        if record is None:
            raise ValueError("实验中没有此生成尝试")
        plan, state = record["plan"], record["state"]
        if state["status"] == "completed":
            session.sync_archive()
            candidate = session.candidates()[state["candidate_id"]]
            evaluation = session.storage.get("evaluation", state["evaluation_attempt_id"])
            return candidate, state["evaluation_attempt_id"] if prepare_only else Evaluation(**evaluation["result"])
        if state["status"] in TERMINAL_FAILURES:
            raise ValueError(f"生成尝试已终止：{state['status']}；保留原始记录，不自动修复或重发")
        if "response" not in record:
            raise ValueError("没有已持久化响应；请求可能已计费，恢复不会重发模型请求")
        parent = session.candidates()[plan["parent_id"]]
        if digest(parent.code.encode("utf-8")) != plan["parent_source_sha256"]:
            raise ValueError("父代源码与原生成计划不一致")
        response = record["response"]
        if response.get("finish_reason") != "stop":
            generation_state(session, record, "incomplete", finish_reason=response.get("finish_reason"))
            raise ValueError("生成未正常结束，原始响应已保存")
        code = response.get("text")
        if not isinstance(code, str) or not code.strip():
            generation_state(session, record, "parse_error")
            raise ValueError("模型没有返回代码文本，原始响应已保存")
        if plan["generation_mode"] == "patch":
            try:
                patches = parse_patches(code)
            except PatchParseError as exc:
                generation_state(session, record, "parse_error", feedback=str(exc))
                raise ValueError(f"补丁解析失败：{exc}；原始响应已保存") from None
            session.save_generation(record, {"patches.json": [asdict(patch) for patch in patches]})
            try:
                code = apply_patches(parent.code, patches)
            except PatchApplyError as exc:
                generation_state(session, record, "patch_error", feedback=str(exc))
                raise ValueError(f"补丁应用失败：{exc}；父代保持不变") from None
        else:
            match = re.fullmatch(r"```(?:python)?\s*\n(.*?)\n```", code.strip(), flags=re.DOTALL)
            code = normalize_source(match.group(1) if match else code)
        candidate = Candidate(
            task_id=session.task.task_id, code=code, parent_id=parent.candidate_id,
            candidate_id=plan["child_candidate_id"],
        )
        session.save_candidate(candidate, generation_id)
        try:
            ast.parse(code)
        except SyntaxError as exc:
            generation_state(session, record, "syntax_error", candidate_id=candidate.candidate_id, line=exc.lineno)
            raise ValueError("候选语法错误，代码及生成记录已保存") from None
        try:
            validate_skeleton(session.task.initial_program.read_text(encoding="utf-8"), code)
        except SkeletonError as exc:
            generation_state(session, record, "skeleton_error", candidate_id=candidate.candidate_id, feedback=str(exc))
            raise ValueError(f"候选骨架校验失败：{exc}；生成记录已保存") from None

        evaluation_id = state.get("evaluation_attempt_id", plan["child_evaluation_attempt_id"])
        existing = session.storage.get("evaluation", evaluation_id)
        if existing and existing["state"] != "completed":
            if not reevaluate_interrupted or existing["state"] == "running":
                raise ValueError("子代评测未完成；先运行状态恢复，再显式使用 --reevaluate-interrupted 新建评测")
            previous = list(state.get("previous_evaluation_attempt_ids", [])) + [evaluation_id]
            evaluation_id = new_id("eval")
            generation_state(session, record, "candidate_ready", candidate_id=candidate.candidate_id,
                             evaluation_attempt_id=evaluation_id, previous_evaluation_attempt_ids=previous)
        elif existing is None:
            generation_state(session, record, "candidate_ready", candidate_id=candidate.candidate_id,
                             evaluation_attempt_id=evaluation_id)
        if prepare_only:
            return candidate, evaluation_id
        generation_state(session, record, "evaluating", candidate_id=candidate.candidate_id, evaluation_attempt_id=evaluation_id)
        evaluation_id, result = session.evaluate(candidate, "child", evaluation_id=evaluation_id,
                                                 reservation_generation_id=generation_id)
        return finish_generation(session, generation_id, candidate, evaluation_id, result)


def finish_generation(session, generation_id, candidate, evaluation_id, result):
    with session.storage.operation():
        record = session.storage.get("generation", generation_id)
        evaluation = session.storage.get("evaluation", evaluation_id)
        if (record["plan"]["child_candidate_id"] != candidate.candidate_id
                or record["state"].get("evaluation_attempt_id") != evaluation_id
                or evaluation is None or evaluation["state"] != "completed"
                or Evaluation(**evaluation["result"]) != result):
            raise ValueError("生成完成记录的评测身份不一致")
        generation_state(session, record, "completed", candidate_id=candidate.candidate_id, evaluation_attempt_id=evaluation_id)
        session.sync_archive()
        print("候选编号：", candidate.candidate_id)
        print("候选状态：", result.status)
        print("候选分数：", result.score)
        print("评测反馈：", result.feedback)
        print("评测记录：", session.directory / "evaluations" / evaluation_id / "evaluation.json")
        return candidate, result
