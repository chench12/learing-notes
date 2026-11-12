"""全调用/评测预算；缺失用量保留预留，估计费用不冒充实际账单。"""

from datetime import datetime, timezone
import math


class BudgetExceeded(RuntimeError):
    pass


def evaluation_reservation(session):
    limits = session.config["execution"]
    startup = limits["startup_timeout_seconds"] if limits["backend"] == "docker" else 1.0
    # 两个独立阶段、启动及清理余量；实际OS调度不保证硬实时上限。
    return session.task.timeout_seconds + limits["evaluator_timeout_seconds"] + 2 * startup + 2


def token_reservation(messages, max_tokens):
    # UTF-8 byte + framing 估计，刻意偏保守。非提供方 tokenizer 的精确保证。
    prompt = sum(len(item["content"].encode("utf-8")) + 64 for item in messages) + 256
    return {"input_tokens": prompt, "output_tokens": max_tokens, "total_tokens": prompt + max_tokens,
            "policy": "utf8-bytes-plus-framing-v1"}


def estimated_cost(config, inputs, outputs):
    if config["input_cost_per_million"] is None:
        return None
    return (inputs * config["input_cost_per_million"] + outputs * config["output_cost_per_million"]) / 1000000


def usage_snapshot(session):
    calls = [item for _, item in session.storage.all("call")]
    tokens, cost, call_time, unknown = 0, 0.0, 0.0, []
    for item in calls:
        # planned 也是预算预留；禁止恢复自动重发。
        actual = (item.get("response") or {}).get("usage") or {}
        reservation = item["plan"]["reservation"]
        total = actual.get("total_tokens")
        known = type(total) is int and total >= 0
        tokens += total if known else reservation["total_tokens"]
        inputs, outputs = actual.get("prompt_tokens"), actual.get("completion_tokens")
        if type(inputs) is not int or type(outputs) is not int or min(inputs, outputs) < 0:
            inputs, outputs = reservation["input_tokens"], reservation["output_tokens"]
        value = estimated_cost(session.config["budget"], inputs, outputs)
        cost += value or 0
        call_time += item.get("elapsed_seconds", 0)
        if not known:
            unknown.append(item["call_id"])
    evaluation_seconds = 0.0
    evaluations = dict(session.storage.all("evaluation"))
    for item in evaluations.values():
        evaluation_seconds += item.get("elapsed_seconds", evaluation_reservation(session))
    pending_evaluation_seconds = 0.0
    for _, generation in session.storage.all("generation"):
        state, plan = generation["state"], generation["plan"]
        if state["status"] in {"completed", "api_error", "incomplete", "parse_error", "patch_error",
                               "syntax_error", "skeleton_error", "budget_stopped", "not_sent"}:
            continue
        identifier = state.get("evaluation_attempt_id", plan["child_evaluation_attempt_id"])
        if identifier not in evaluations:
            pending_evaluation_seconds += state.get("reserved_child_evaluation_seconds", 0.0)
    # 控制器跨重启累计；活跃/异常结束时保守计入从开始到当前时间。
    wall = 0.0
    for _, item in session.storage.all("control"):
        wall += item.get("elapsed_seconds", max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(item["started_at_utc"])).total_seconds()))
    return {"calls": len(calls), "charged_tokens": tokens, "unknown_usage_call_ids": unknown,
            "estimated_cost": cost if session.config["budget"]["input_cost_per_million"] is not None else None,
            "evaluation_seconds": evaluation_seconds, "pending_evaluation_seconds": pending_evaluation_seconds,
            "wall_seconds": max(wall, evaluation_seconds + call_time)}


def check_budget(session, *, messages=None, max_tokens=None, evaluation_seconds=0, release_evaluation_seconds=0):
    used, budget = usage_snapshot(session), session.config["budget"]
    if used["wall_seconds"] >= budget["max_wall_seconds"]:
        raise BudgetExceeded("已达到累计墙钟预算")
    pending = max(0, used["pending_evaluation_seconds"] - release_evaluation_seconds)
    if used["evaluation_seconds"] + pending + evaluation_seconds > budget["max_evaluation_seconds"]:
        raise BudgetExceeded("剩余评测预算不足以预留下一次完整评测")
    if (evaluation_seconds or pending) and used["wall_seconds"] + pending + evaluation_seconds > budget["max_wall_seconds"]:
        raise BudgetExceeded("剩余墙钟预算不足以预留下一次完整评测")
    if messages is not None:
        # ChatModel 的请求超时固定120秒；预留整个请求窗口，避免临近期限仍发请求。
        if used["wall_seconds"] + 120 > budget["max_wall_seconds"]:
            raise BudgetExceeded("剩余墙钟预算不足以预留120秒模型请求窗口")
        reserve = token_reservation(messages, max_tokens)
        if used["calls"] >= budget["max_calls"]:
            raise BudgetExceeded("已达到模型调用预算")
        if used["charged_tokens"] + reserve["total_tokens"] > budget["max_tokens"]:
            raise BudgetExceeded("剩余 token 预算不足以预留完整输入和最大输出")
        value = estimated_cost(budget, reserve["input_tokens"], reserve["output_tokens"])
        if budget["max_estimated_cost"] is not None and used["estimated_cost"] + value > budget["max_estimated_cost"]:
            raise BudgetExceeded("剩余参考费用预算不足；该估计不是平台实际账单")
        return reserve
    return used
