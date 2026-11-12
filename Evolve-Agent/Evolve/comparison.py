"""匹配配置的选择策略对照准备与只读分析；不调用模型、不执行候选。"""

from collections import Counter
from copy import deepcopy
import csv
import io
import json
import math
from statistics import mean

from Evolve.experiment import _validate_config
from Evolve.selection import SAMPLERS
from Evolve.storage import encode_json
from Evolve.budget import usage_snapshot
from Evolve.generation import TERMINAL_FAILURES


METHODS = tuple(SAMPLERS)


def prepare_configs(base_config, output, *, seeds, generations):
    if not seeds or len(set(seeds)) != len(seeds) or any(type(seed) is not int for seed in seeds):
        raise ValueError("搜索种子必须为不重复的整数")
    if type(generations) is not int or generations <= 0:
        raise ValueError("generations 必须为正整数")
    _validate_config(base_config)
    if base_config["archive"]["strategy"] != "island_grid":
        raise ValueError("三策略对照要求共用 island_grid 档案")
    configs = []
    for seed in seeds:
        for method in METHODS:
            config = deepcopy(base_config)
            config["search"].update(seed=seed, max_generations=generations)
            config["archive"]["parent_sampling"] = method
            configs.append((f"{method}_seed{seed}.json", config))
    output.mkdir(parents=True, exist_ok=False)
    for name, config in configs:
        (output / name).write_bytes(encode_json(config))
    plan = {
        "kind": "matched-parent-selection-configs-v1", "seeds": seeds,
        "generations_per_run": generations, "runs": [name for name, _ in configs],
        "total_planned_generations": generations * len(configs),
        "started_experiments": 0, "model_calls": 0,
        "comparison": "相同档案/迁移/提示词/预算；仅父代采样规则不同。冻结模型，不是完整 FunSearch/TTT-Discover 复现。",
        "budget_note": "等生成计划上限，不等于等 token/费用；统一调用/token/时间及可选参考费用预算由冻结配置限制。",
    }
    (output / "comparison_plan.json").write_bytes(encode_json(plan))
    return plan


def comparison_identity(manifest):
    config = deepcopy(manifest["config"])
    config["search"].pop("seed")
    config["archive"].pop("parent_sampling")
    return {"config": config, "files": manifest["files"],
            "environment": manifest["environment"], "schema_version": manifest["schema_version"]}


def summarize_session(session):
    """所有计数按唯一生成/评测 ID；缓存事件不计为搜索进展。"""
    with session.storage.operation():
        return _summarize_session(session)


def _summarize_session(session):
    latest, seen_evaluations, started, responses, parents = {}, set(), set(), set(), Counter()
    known_tokens, trace = 0, []
    for event in session.storage.events():
        kind, record, identifier = event["kind"], event["payload"], event["record_id"]
        if kind == "call" and record["plan"]["purpose"] == "program":
            owner = record["plan"]["owner_id"]
            if owner not in started:
                generation = session.storage.get("generation", owner)
                if generation is not None:
                    started.add(owner)
                    parents[generation["plan"]["parent_id"]] += 1
        if kind == "call" and record["state"] == "completed" and identifier not in responses:
            responses.add(identifier)
            total = (record.get("response", {}).get("usage") or {}).get("total_tokens")
            if type(total) is int and total >= 0:
                known_tokens += total
        elif kind == "generation":
            if "call_id" not in record["plan"] and record["state"]["status"] not in {"planned", "not_sent", "budget_stopped"} and identifier not in started:
                started.add(identifier)
                parents[record["plan"]["parent_id"]] += 1
            if "response" in record and "call_id" not in record["plan"] and identifier not in responses:
                responses.add(identifier)
                total = (record["response"].get("usage") or {}).get("total_tokens")
                if type(total) is int and total >= 0:
                    known_tokens += total
        elif (kind == "evaluation" and record["state"] == "completed"
              and record["purpose"] == "search" and record["stage"] == "standard"
              and identifier not in seen_evaluations):
            seen_evaluations.add(identifier)
            latest[record["candidate_id"]] = record["result"]
            valid = [result["score"] for result in latest.values() if result["status"] == "ok"]
            trace.append({"event_sequence": event["sequence"], "evaluation_id": identifier,
                          "candidate_id": record["candidate_id"], "role": record["role"],
                          "status": record["result"]["status"], "score": record["result"]["score"],
                          "best_latest_score": max(valid, default=None),
                          "requested_generations": len(started), "known_tokens": known_tokens})
    generations = [record for _, record in session.storage.all("generation")]
    evaluations = [record for _, record in session.storage.all("evaluation")]
    children = [record for record in evaluations if record["state"] == "completed"
                and record["purpose"] == "search" and record["stage"] == "standard" and record["role"] == "child"]
    token_complete = all(
        "response" in record
        and type((record["response"].get("usage") or {}).get("total_tokens")) is int
        and (record["response"].get("usage") or {})["total_tokens"] >= 0
        for record in generations if record["plan"]["generation_attempt_id"] in started
    )
    calls = [record for _, record in session.storage.all("call")]
    if calls:
        token_complete = not usage_snapshot(session)["unknown_usage_call_ids"]
    snapshot, best = session.archive_snapshot(), session.best()
    active = [member for island in snapshot["islands"] for member in island["cells"]]
    possible = session.config["archive"]["islands"] * math.prod(len(axis["edges"]) - 1 for axis in snapshot["axes"])
    entropy = -sum((count / len(started)) * math.log(count / len(started)) for count in parents.values()) if started else 0.0
    states = dict(Counter(record["state"]["status"] for record in generations))
    budget = session.config["search"]["max_generations"]
    terminal = TERMINAL_FAILURES | {"completed"}
    return {
        "experiment_id": session.experiment_id, "directory": str(session.directory),
        "method": session.config["archive"]["parent_sampling"], "seed": session.config["search"]["seed"],
        "planned_generation_budget": budget, "generation_plans": len(generations),
        "requested_generations": len(started), "saved_responses": len(responses),
        "all_model_calls": len(calls), "call_purposes": dict(Counter(call["plan"]["purpose"] for call in calls)),
        "known_tokens": known_tokens, "token_accounting_complete": token_complete,
        "finished_chains": states.get("completed", 0), "generation_states": states,
        "budget_finished": len(generations) == budget and all(record["state"]["status"] in terminal for record in generations),
        "best_latest_score": best[1].score if best else None,
        "best_latest_candidate_id": best[0].candidate_id if best else None,
        "max_observed_search_score": max((item["score"] for item in trace if item["status"] == "ok"), default=None),
        "completed_child_evaluations": len(children),
        "child_valid_rate": sum(record["result"]["status"] == "ok" for record in children) / len(children) if children else None,
        "search_evaluation_seconds": math.fsum(record.get("elapsed_seconds", 0) for record in evaluations if record["purpose"] == "search"),
        "confirmation_evaluation_seconds": math.fsum(record.get("elapsed_seconds", 0) for record in evaluations if record["purpose"] == "confirmation"),
        "occupied_island_cells": len(active), "possible_island_cells": possible,
        "coverage_fraction": len(active) / possible,
        "unique_active_candidates": len({member["candidate_id"] for member in active}),
        "unique_selected_parents": len(parents), "parent_selection_entropy_nats": entropy,
        "response_models": sorted({str(record["response"].get("model")) for record in generations if "response" in record}),
        "trace": trace,
    }


def compare_sessions(sessions):
    if not sessions:
        raise ValueError("没有实验")
    identity = comparison_identity(sessions[0].manifest)
    if any(comparison_identity(session.manifest) != identity for session in sessions):
        raise ValueError("实验配置/任务/框架/环境不匹配；只允许 parent_sampling 与搜索 seed 不同")
    if any(session.config["archive"]["strategy"] != "island_grid" for session in sessions):
        raise ValueError("三策略比较要求共用 island_grid 档案")
    rows = [summarize_session(session) for session in sessions]
    pairs = [(row["method"], row["seed"]) for row in rows]
    if len(set(pairs)) != len(pairs):
        raise ValueError("同一策略/种子有多个实验，请显式选取一份，避免事后挑选最好结果")
    seeds = sorted({row["seed"] for row in rows})
    if set(pairs) != {(method, seed) for method in METHODS for seed in seeds}:
        raise ValueError("需要三种策略及完全相同的种子集合，不能用缺组数据排名")
    aggregate = []
    for method in METHODS:
        group = [row for row in rows if row["method"] == method]
        valid_scores = [row["best_latest_score"] for row in group if row["best_latest_score"] is not None]
        aggregate.append({
            "method": method, "runs": len(group), "runs_with_valid_score": len(valid_scores),
            "mean_best_latest_score": mean(valid_scores) if valid_scores else None,
            "min_best_latest_score": min(valid_scores) if valid_scores else None,
            "max_best_latest_score": max(valid_scores) if valid_scores else None,
            "mean_coverage_fraction": mean(row["coverage_fraction"] for row in group),
            "mean_requested_generations": mean(row["requested_generations"] for row in group),
            "mean_known_tokens": mean(row["known_tokens"] for row in group),
            "all_token_usage_known": all(row["token_accounting_complete"] for row in group),
        })
    return {
        "kind": "matched-parent-selection-results-v1", "paired_seeds": seeds,
        "all_budgets_finished": all(row["budget_finished"] for row in rows),
        "equal_requested_generations": len({row["requested_generations"] for row in rows}) == 1,
        "runs": rows, "aggregate": aggregate,
        "interpretation": "描述性结果；未自动执行独立复核或显著性检验。等调用预算不等于等 token/费用。",
    }


def write_comparison(report, output):
    output.mkdir(parents=True, exist_ok=False)
    (output / "results.json").write_bytes(encode_json(report))
    rows = [{key: value for key, value in row.items() if key != "trace"} for row in report["runs"]]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                     for key, value in row.items()} for row in rows)
    (output / "runs.csv").write_text(stream.getvalue(), encoding="utf-8-sig")
    trace = io.StringIO(newline="")
    fields = ["experiment_id", "method", "seed", "event_sequence", "evaluation_id", "candidate_id", "role", "status", "score", "best_latest_score", "requested_generations", "known_tokens"]
    writer = csv.DictWriter(trace, fieldnames=fields)
    writer.writeheader()
    for row in report["runs"]:
        for item in row["trace"]:
            writer.writerow({"experiment_id": row["experiment_id"], "method": row["method"], "seed": row["seed"], **item})
    (output / "trace.csv").write_text(trace.getvalue(), encoding="utf-8-sig")
