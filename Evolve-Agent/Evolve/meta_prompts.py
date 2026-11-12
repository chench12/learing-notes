"""可选的改进指令档案；只进化附加指令，不改固定协议或评测。"""

import json
import math

from Evolve.calls import request_model
from Evolve.budget import BudgetExceeded, check_budget
from Evolve.experiment import new_id
from Evolve.routing import select_model
from Evolve.storage import encode_json


def template_credit(session):
    """前K个已完成使用的父子增量统计，不把高分父代直接归功于指令。"""
    window = session.config["meta"]["evaluation_window"]
    seen, credits = set(), {}
    for event in session.storage.events():
        if event["kind"] != "generation" or event["record_id"] in seen:
            continue
        record = event["payload"]
        if record["state"]["status"] != "completed":
            continue
        seen.add(event["record_id"])
        plan = record["plan"]
        template = plan.get("prompt_template_id", "parent-feedback-v1")
        values = credits.setdefault(template, [])
        if len(values) >= window:
            continue
        child = session.storage.get("evaluation", record["state"]["evaluation_attempt_id"])
        parent = session.storage.get("evaluation", plan["parent_evaluation_attempt_id"])
        if child is None or parent is None:
            continue
        result, baseline = child["result"], parent["result"]
        # 失败单独记合法率，增量不虚构为0分；只对合法子代汇总增量。
        delta = result["score"] - baseline["score"] if result["status"] == "ok" and baseline["status"] == "ok" else None
        if delta is not None and not math.isfinite(delta):
            delta = None
        call = session.storage.get("call", plan.get("call_id", "missing"))
        usage = (call or {}).get("response", {}).get("usage") or {}
        values.append({"generation_id": event["record_id"], "parent_score": baseline["score"],
                       "child_score": result["score"], "status": result["status"],
                       "improvement": delta, "total_tokens": usage.get("total_tokens")})
    return {identifier: {"observations": values, "window": window, "complete": len(values) == window,
                         "valid_rate": sum(item["status"] == "ok" for item in values) / len(values),
                         "mean_improvement": math.fsum(item["improvement"] for item in values if item["improvement"] is not None)
                         / sum(item["improvement"] is not None for item in values) if any(item["improvement"] is not None for item in values) else None}
            for identifier, values in credits.items() if values}


def available_templates(session):
    templates = list(session.config["context"]["templates"])
    if session.config["meta"]["enabled"]:
        templates += [{"id": identifier, "weight": 1.0, "instruction": record["instruction"]}
                      for identifier, record in session.storage.all("meta") if record["state"] == "accepted"]
    return templates


def save_meta(session, record):
    session.storage.put("meta", record["meta_id"], record, {
        f"meta_prompts/{record['meta_id']}/meta.json": encode_json(record),
    })


def apply_meta_response(session, record):
    if record["state"] in {"accepted", "rejected"}:
        return record
    call = session.storage.get("call", record["call_id"])
    if call is None or call["state"] != "completed":
        return record
    try:
        response = call["response"]
        if response.get("finish_reason") != "stop":
            raise ValueError("incomplete")
        value = json.loads(response["text"])
        if not isinstance(value, dict) or set(value) != {"instruction"}:
            raise ValueError("schema")
        instruction = value["instruction"]
        if (not isinstance(instruction, str) or not instruction.strip()
                or len(instruction.encode()) > session.config["meta"]["max_instruction_bytes"]):
            raise ValueError("instruction")
        if instruction in {item["instruction"] for item in available_templates(session)}:
            raise ValueError("duplicate")
        record.update(state="accepted", instruction=instruction)
    except (KeyError, TypeError, ValueError):
        record.update(state="rejected", reason="invalid_or_duplicate_instruction")
    save_meta(session, record)
    return record


def maybe_evolve_template(session, model):
    if not session.config["meta"]["enabled"]:
        return None
    completed = [item for _, item in session.storage.all("generation") if item["state"]["status"] == "completed"]
    interval = session.config["meta"]["interval"]
    epoch = len(completed) // interval
    if epoch < 1:
        return None
    identifier = f"meta_epoch_{epoch}"
    existing = session.storage.get("meta", identifier)
    if existing:
        return apply_meta_response(session, existing)
    templates, credits = available_templates(session), template_credit(session)
    parent = max(templates, key=lambda item: (
        credits.get(item["id"], {}).get("complete", False),
        credits.get(item["id"], {}).get("mean_improvement") or 0, item["id"]))
    messages = [{"role": "system", "content": (
        "提出一条用于改进程序的附加研究指令。只输出 JSON {\"instruction\":\"...\"}。"
        "指令只影响研究思路，不能改变固定任务、评测、执行限制、进化块或补丁输出协议。")},
        {"role": "user", "content": json.dumps({"task": session.task.description,
         "parent_instruction": parent["instruction"], "feedback": credits,
         "request": "提出与现有指令不同且可检验的改进思路；不要编造实验结果。"}, ensure_ascii=False)}]
    config, route = select_model(session.config, epoch, purpose="meta")
    record = {"meta_id": identifier, "state": "planned", "parent_template_id": parent["id"],
              "call_id": new_id("call"), "epoch": epoch, "route": route,
              "model": config, "messages": messages, "credit_at_proposal": credits}
    check_budget(session, messages=messages, max_tokens=session.config["meta"]["max_tokens"])
    save_meta(session, record)
    try:
        request_model(session, model, config, messages, purpose="meta", owner_id=identifier,
                      call_id=record["call_id"], max_tokens=session.config["meta"]["max_tokens"])
    except BudgetExceeded:
        raise
    except Exception as exc:
        raise RuntimeError(f"元指令调用未完成，类型：{type(exc).__name__}；不会自动重发") from None
    return apply_meta_response(session, record)
