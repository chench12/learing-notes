"""可重放的父代采样；不调用模型、不执行候选。"""

import hashlib
import math
import random

from Evolve.archive import POLICY


SAMPLERS = {
    "uniform_cell": POLICY,
    "softmax_cell": "island-grid-softmax-v1",
    "puct_cell": "island-grid-puct-v1",
}


def rebuild_search_statistics(events, island_count):
    """首次发起请求计一次扩展；Q 只由完成链的直接子代搜索结果更新。

    访问数回传同一选岛中的父代及祖先；迁移不复制访问统计。
    未知 API 结果、解析失败等已经发起的尝试仍占用一次访问。
    """
    parents, evaluations = {}, {}
    plans = {}
    started, finished = set(), set()
    islands = [{"island_id": i, "expansions": 0, "candidates": {}}
               for i in range(island_count)]

    def entry(island, identifier):
        return island["candidates"].setdefault(identifier, {
            "visits": 0, "direct_expansions": 0, "best_child_score": None,
            "best_child_evaluation_id": None,
        })

    def count_request(identifier, plan):
        if identifier in started:
            return
        island_id, parent = plan["island_id"], plan["parent_id"]
        if type(island_id) is not int or not 0 <= island_id < island_count:
            raise ValueError("选择统计的岛屿编号越界")
        started.add(identifier)
        island = islands[island_id]
        island["expansions"] += 1
        entry(island, parent)["direct_expansions"] += 1
        ancestor, seen = parent, set()
        while ancestor is not None:
            if ancestor in seen:
                raise ValueError("候选谱系存在环")
            seen.add(ancestor)
            entry(island, ancestor)["visits"] += 1
            ancestor = parents.get(ancestor)

    for event in events:
        kind, record, identifier = event["kind"], event["payload"], event["record_id"]
        if kind == "candidate":
            parents[identifier] = record["candidate"]["parent_id"]
        elif kind == "call" and record["plan"]["purpose"] == "program":
            owner = record["plan"]["owner_id"]
            if owner in plans:
                count_request(owner, plans[owner])
        elif (kind == "evaluation" and record["state"] == "completed"
              and record["purpose"] == "search" and record["stage"] == "standard"):
            evaluations.setdefault(identifier, record)
        elif kind == "generation":
            plan, state = record["plan"], record["state"]
            plans[identifier] = plan
            if "island_id" not in plan or "parent_id" not in plan:
                continue
            island_id, parent = plan["island_id"], plan["parent_id"]
            if type(island_id) is not int or not 0 <= island_id < island_count:
                raise ValueError("选择统计的岛屿编号越界")
            island = islands[island_id]
            if "call_id" not in plan and state["status"] not in {"planned", "not_sent", "budget_stopped"}:
                count_request(identifier, plan)  # 旧记录/离线机制夹具。
            if state["status"] == "completed" and identifier not in finished:
                finished.add(identifier)
                result = evaluations.get(state.get("evaluation_attempt_id"))
                if result is None:
                    raise ValueError("完成生成缺少已提交的子代搜索评测")
                child = plan["child_candidate_id"]
                if result["candidate_id"] != child or result["result"]["candidate_id"] != child:
                    raise ValueError("选择统计的子代评测身份不一致")
                score = result["result"].get("score")
                item = entry(island, parent)
                if result["result"]["status"] == "ok" and (
                    item["best_child_score"] is None or score > item["best_child_score"]
                ):
                    item.update(best_child_score=score,
                                best_child_evaluation_id=result["evaluation_attempt_id"])
    return {"policy": "request-count-direct-child-max-v1", "islands": islands}


def temperature(config, expansions):
    period = config["temperature_period"]
    # 与 FunSearch 相似的周期下降；计数改为本岛已发起的生成次数。
    return max(config["temperature_initial"] * (1 - (expansions % period) / period),
               math.ulp(0.0))


def selection_weights(cells, config, statistics):
    """返回选择概率/诊断。PUCT 为最大分选择；只在最大分同分时随机。"""
    if not cells:
        return [], {"rule": config["parent_sampling"], "candidates": []}
    method, size = config["parent_sampling"], len(cells)
    details = [{"candidate_id": item["candidate_id"], "score": item["score"]} for item in cells]
    diagnostics = {"rule": method, "expansions": statistics["expansions"], "candidates": details}
    if method == "uniform_cell":
        weights = [1 / size] * size
    elif method == "softmax_cell":
        tau = temperature(config, statistics["expansions"])
        maximum = max(item["score"] for item in cells)
        logits = [math.exp((item["score"] - maximum) / tau) for item in cells]
        total = math.fsum(logits)
        weights = [value / total for value in logits]
        diagnostics["temperature"] = tau
    elif method == "puct_cell":
        records = statistics["candidates"]
        ranked = sorted(cells, key=lambda item: (-item["score"], item["candidate_id"]))
        priors = {item["candidate_id"]: (size - rank) / (size * (size + 1) / 2)
                  for rank, item in enumerate(ranked)}
        qs = [records.get(item["candidate_id"], {}).get("best_child_score") for item in cells]
        qs = [item["score"] if q is None else q for item, q in zip(cells, qs)]
        # 用公共正单位缩放整个公式，防止有限的极端奖励相减溢出；不改变 argmax。
        unit = max(1.0, *(abs(item["score"]) for item in cells), *(abs(q) for q in qs))
        reward_range = max(item["score"] / unit for item in cells) - min(item["score"] / unit for item in cells)
        # 同分时论文的 range 为零；本项目明确用 1 奖励单位避免探索项消失。
        flat = reward_range == 0
        exploration_scale = 1 / unit if flat else reward_range
        values = []
        for detail, item, q in zip(details, cells, qs):
            record = records.get(item["candidate_id"], {})
            visits, prior = record.get("visits", 0), priors[item["candidate_id"]]
            bonus = (config["puct_c"] * exploration_scale * prior
                     * math.sqrt(1 + statistics["expansions"]) / (1 + visits))
            value = q / unit + bonus
            if not math.isfinite(value):
                raise ValueError("PUCT 数值溢出，请降低 puct_c")
            values.append(value)
            detail.update(q=q, prior=prior, visits=visits,
                          direct_expansions=record.get("direct_expansions", 0),
                          best_child_evaluation_id=record.get("best_child_evaluation_id"),
                          exploration_bonus_scaled=bonus, utility_scaled=value)
        best = max(values)
        tied = sum(value == best for value in values)
        weights = [1 / tied if value == best else 0.0 for value in values]
        diagnostics.update(puct_c=config["puct_c"], utility_unit=unit,
                           reward_range_scaled=reward_range, flat_reward_fallback=flat)
    else:
        raise ValueError("未知父代采样规则")
    for detail, weight in zip(details, weights):
        detail["probability"] = weight
    return weights, diagnostics


def sample_parent(snapshot, *, seed, selection_index, config=None, statistics=None):
    config = config or {"parent_sampling": "uniform_cell"}
    if statistics is None:
        if config["parent_sampling"] != "uniform_cell":
            raise ValueError("带反馈的父代选择需要显式传入事件重建的统计")
        statistics = {"islands": [{"expansions": 0, "candidates": {}} for _ in snapshot["islands"]]}
    island_id = selection_index % len(snapshot["islands"])
    cells = snapshot["islands"][island_id]["cells"]
    strategy = SAMPLERS[config["parent_sampling"]]
    entropy = hashlib.sha256(f"{strategy}:{seed}:{selection_index}".encode("utf-8")).hexdigest()
    weights, diagnostics = selection_weights(cells, config, statistics["islands"][island_id])
    rng = random.Random(entropy)
    if not cells:
        selected = None
    elif config["parent_sampling"] == "uniform_cell":
        selected = rng.choice(cells)  # 保留首版均匀策略的抽样序列。
    else:
        selected = rng.choices(cells, weights=weights, k=1)[0]
    return {
        "selection_strategy": strategy, "selection_index": selection_index,
        "island_id": island_id, "random_seed_sha256": entropy,
        "sampled_cell": selected["cell"] if selected else None,
        "parent_id": selected["candidate_id"] if selected else None,
        "historical_evaluation_id": selected["evaluation_attempt_id"] if selected else None,
        "diagnostics": diagnostics,
        "archive_snapshot": snapshot,
    }
