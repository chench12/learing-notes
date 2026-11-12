"""从事务事实重建岛屿特征档案；规则为本项目设计，并非论文公开参数。"""

from bisect import bisect_right
import math

from Evolve.contracts import Evaluation


POLICY = "island-grid-latest-v1"


def validate_archive_config(config):
    expected = {"strategy", "islands", "axes", "cell_capacity", "island_selection", "parent_sampling",
                "seed_assignment", "tie_break", "migration_interval", "migration_count",
                "migration_topology", "restart", "temperature_initial", "temperature_period", "puct_c"}
    if not isinstance(config, dict) or set(config) != expected:
        raise ValueError("archive 配置字段与约定不一致")
    if config["strategy"] not in ("island_grid", "greedy"):
        raise ValueError("archive.strategy 必须为 island_grid 或 greedy")
    for key in ("islands", "migration_interval", "migration_count", "temperature_period"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"archive.{key} 必须为正整数")
    if config["parent_sampling"] not in ("uniform_cell", "softmax_cell", "puct_cell"):
        raise ValueError("未知父代采样规则")
    for key in ("temperature_initial", "puct_c"):
        if type(config[key]) not in (int, float) or not math.isfinite(config[key]) or config[key] <= 0:
            raise ValueError(f"archive.{key} 必须为有限正数")
    fixed = {"cell_capacity": 1, "island_selection": "round_robin", "seed_assignment": "all",
             "tie_break": "candidate_id_ascending", "migration_topology": "ring",
             "restart": "none"}
    for key, value in fixed.items():
        if type(config[key]) is not type(value) or config[key] != value:
            raise ValueError(f"archive.{key} 当前仅支持 {value}")
    axes = config["axes"]
    if not isinstance(axes, list) or not axes:
        raise ValueError("archive.axes 必须为非空列表")
    names = set()
    for axis in axes:
        if not isinstance(axis, dict) or set(axis) != {"name", "edges"}:
            raise ValueError("特征轴必须包含 name 和 edges")
        name, edges = axis["name"], axis["edges"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("特征名必须非空且不重复")
        names.add(name)
        if (not isinstance(edges, list) or len(edges) < 2
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in edges)
                or any(a >= b for a, b in zip(edges, edges[1:]))):
            raise ValueError("特征边界必须为严格递增的有限数")


def feature_cell(features, axes):
    """左闭右开，最后一个格子包含最右端；缺失/越界拒绝，不静默截断。"""
    cell = []
    for axis in axes:
        value = features.get(axis["name"])
        edges = axis["edges"]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value < edges[0] or value > edges[-1]):
            return None
        cell.append(min(bisect_right(edges, value) - 1, len(edges) - 2))
    return tuple(cell)


def quality_key(member):
    return (-member["score"], member["candidate_id"])


def rebuild_archive(config, events):
    """按提交序号读取事实，档案只是可重建视图；同格落选事实仍保留。

    岛屿成员资格持久存在，最新已完成搜索评测决定资格和格子。
    重新竞争全部成员，避免保留某候选历史上最幸运的一次成绩。
    """
    candidates, generations, latest, migrations = {}, {}, {}, []
    memberships = {}
    seen_evaluations = set()
    completed_generations = set()
    source_sequence = 0

    def assign_membership(identifier):
        record = candidates[identifier]
        candidate = record["candidate"]
        if candidate["parent_id"] is None:
            memberships[identifier].update(range(config["islands"]))
        else:
            plan = generations.get(record["generation_id"])
            if plan is not None and plan.get("island_id") is not None:
                island = plan["island_id"]
                if type(island) is not int or not 0 <= island < config["islands"]:
                    raise ValueError("档案岛屿编号越界")
                memberships[identifier].add(island)

    for event in events:
        kind, record = event["kind"], event["payload"]
        if kind not in {"candidate", "generation", "evaluation"}:
            continue
        source_sequence = event["sequence"]
        if kind == "candidate":
            candidates[event["record_id"]] = record
            memberships.setdefault(event["record_id"], set())
            assign_membership(event["record_id"])
        elif kind == "generation":
            generations[event["record_id"]] = record["plan"]
            # 正常路径先计划后子代；亦支持从已有候选事实重放后续计划。
            child = record["plan"].get("child_candidate_id")
            if child in candidates:
                assign_membership(child)
            if record["state"]["status"] == "completed" and event["record_id"] not in completed_generations:
                completed_generations.add(event["record_id"])
                if (config["strategy"] == "island_grid"
                        and len(completed_generations) % config["migration_interval"] == 0):
                    islands, _ = _compete(config, candidates, latest, memberships)
                    transfers = ring_transfers({"islands": islands}, config["migration_count"])
                    migrations.append({
                        "migration_id": f"migration_{event['record_id']}",
                        "trigger_generation_id": event["record_id"],
                        "trigger_event_sequence": event["sequence"],
                        "completed_generations": len(completed_generations),
                        "transfers": transfers,
                    })
                    for transfer in transfers:
                        if transfer["accepted"]:
                            memberships[transfer["candidate_id"]].add(transfer["target_island"])
        elif (record["state"] == "completed" and record["purpose"] == "search"
              and record["stage"] == "standard" and event["record_id"] not in seen_evaluations):
            seen_evaluations.add(event["record_id"])
            result = Evaluation(**record["result"])
            if result.candidate_id != record["candidate_id"] or result.stage != "standard":
                raise ValueError("档案评测身份不一致")
            latest[result.candidate_id] = (event["record_id"], result)
    islands, excluded = _compete(config, candidates, latest, memberships)
    return {
        "policy": POLICY, "strategy": config["strategy"],
        "source_event_sequence": source_sequence, "axes": config["axes"],
        "islands": islands,
        "memberships": {key: sorted(value) for key, value in sorted(memberships.items())},
        "excluded": excluded, "migrations": migrations,
    }


def _compete(config, candidates, latest, memberships):
    cells = [{} for _ in range(config["islands"])]
    excluded = []
    for identifier in sorted(candidates):
        item = latest.get(identifier)
        reason = None
        if item is None:
            reason = "not_evaluated"
        elif item[1].status != "ok":
            reason = item[1].status
        else:
            cell = feature_cell(item[1].features, config["axes"])
            if cell is None:
                reason = "missing_or_out_of_range_features"
        if reason:
            excluded.append({"candidate_id": identifier, "reason": reason})
            continue
        member = {"candidate_id": identifier, "evaluation_attempt_id": item[0],
                  "score": item[1].score, "features": item[1].features, "cell": list(cell)}
        for island in sorted(memberships[identifier]):
            if type(island) is not int or not 0 <= island < config["islands"]:
                raise ValueError("档案岛屿编号越界")
            old = cells[island].get(cell)
            if old is None or quality_key(member) < quality_key(old):
                cells[island][cell] = member
    return ([{"island_id": index, "cells": [members[key] for key in sorted(members)]}
             for index, members in enumerate(cells)], excluded)


def ring_transfers(snapshot, count):
    """冻结全部供体后同时迁移；目标同格比较，不在一轮内连续传递。"""
    islands = snapshot["islands"]
    transfers = []
    if len(islands) == 1:
        return transfers
    for source in islands:
        target = islands[(source["island_id"] + 1) % len(islands)]
        target_cells = {tuple(member["cell"]): member for member in target["cells"]}
        for member in sorted(source["cells"], key=quality_key)[:count]:
            old = target_cells.get(tuple(member["cell"]))
            transfers.append({
                "source_island": source["island_id"], "target_island": target["island_id"],
                **member, "previous_candidate_id": old["candidate_id"] if old else None,
                "accepted": old is None or quality_key(member) < quality_key(old),
            })
    return transfers
