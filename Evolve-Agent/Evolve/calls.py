"""所有模型调用统一记账；先提交预留计划，再请求一次，不自动重试。"""

from datetime import datetime, timezone
from time import perf_counter

from Evolve.budget import check_budget
from Evolve.experiment import new_id
from Evolve.routing import invoke
from Evolve.storage import encode_json


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save_call(session, record):
    session.storage.put("call", record["call_id"], record, {
        f"calls/{record['call_id']}/call.json": encode_json(record),
    })


def prepare_call(session, model_config, messages, *, purpose, owner_id, call_id=None, max_tokens=None):
    with session.storage.operation():
        identifier = call_id or new_id("call")
        output_limit = max_tokens or model_config["max_tokens"]
        existing = session.storage.get("call", identifier)
        if existing is not None:
            if (existing["plan"]["owner_id"] != owner_id or existing["plan"]["purpose"] != purpose
                    or existing["plan"]["model"] != model_config or existing["plan"]["messages"] != messages
                    or existing["plan"]["max_tokens"] != output_limit):
                raise ValueError("调用编号身份不一致")
            if existing["state"] == "completed":
                return existing
            raise ValueError("已有调用结果未知或失败；不自动重发")
        reserve = check_budget(session, messages=messages, max_tokens=output_limit)
        record = {"call_id": identifier, "experiment_id": session.experiment_id, "state": "requesting",
                  "started_at_utc": stamp(), "plan": {"owner_id": owner_id, "purpose": purpose,
                  "model": model_config, "messages": messages, "max_tokens": output_limit, "reservation": reserve}}
        save_call(session, record)
        return record


def finish_call(session, record, response, elapsed_seconds):
    if not isinstance(response, dict):
        raise TypeError("模型接口必须返回字典")
    record.update(state="completed", response=response, elapsed_seconds=elapsed_seconds, finished_at_utc=stamp())
    save_call(session, record)
    return response


def fail_call(session, record, exc, elapsed_seconds):
    code = getattr(exc, "status_code", None)
    rejected = type(code) is int and 400 <= code < 500 and code not in (408, 429)
    record.update(state="api_error" if rejected else "uncertain", error_type=type(exc).__name__,
                  elapsed_seconds=elapsed_seconds, finished_at_utc=stamp())
    save_call(session, record)


def request_model(session, model, model_config, messages, *, purpose, owner_id, call_id=None, max_tokens=None):
    with session.storage.operation():
        record = prepare_call(session, model_config, messages, purpose=purpose, owner_id=owner_id,
                              call_id=call_id, max_tokens=max_tokens)
        if record["state"] == "completed":
            return record["call_id"], record["response"]
        started = perf_counter()
        try:
            response = invoke(model, model_config, messages=messages, max_tokens=record["plan"]["max_tokens"])
            if not isinstance(response, dict):
                raise TypeError("模型接口必须返回字典")
        except BaseException as exc:
            fail_call(session, record, exc, perf_counter() - started)
            raise
        finish_call(session, record, response, perf_counter() - started)
        return record["call_id"], response
