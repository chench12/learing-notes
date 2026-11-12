"""实验展示与开发目录路由；不改变被冻结的搜索框架和历史记录。"""

import json
from pathlib import Path

from Evolve.experiment import create_experiment, digest
from Evolve.storage import Storage, atomic_bytes, encode_json


OVERVIEW_FORMAT = "evolve-experiment-overview-v1"


def create_development_experiment(config_path, project_root):
    """将检查/手写演示的新实验完整放到 dev_runs，保留原实验身份。"""
    project = Path(project_root).resolve()
    directory, manifest = create_experiment(config_path, project)
    destination = project / "dev_runs" / directory.name
    destination.parent.mkdir(exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    directory.rename(destination)
    return destination, manifest


def write_overview(directory):
    """只读取已提交记录，生成可重新导出的摘要；不请求模型或执行候选。"""
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 9:
        raise ValueError("摘要入口目前支持 schema 9；旧版实验保留原样")
    identity = {key: manifest[key] for key in ("schema_version", "config", "files", "environment")}
    identity_bytes = json.dumps(identity, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if digest(identity_bytes) != manifest["fingerprint"]:
        raise ValueError("实验清单与指纹不一致")
    storage = Storage(directory, manifest["experiment_id"], manifest["fingerprint"])
    with storage.operation():
        storage.verify_files()
        previous = directory / "summary.json"
        owned = previous.exists()
        if owned:
            if json.loads(previous.read_text(encoding="utf-8")).get("format") != OVERVIEW_FORMAT:
                raise ValueError("summary.json 已被其它内容占用")
        elif any((directory / name).exists() for name in ("best.py", "best_solution.json", "README.md")):
            raise ValueError("摘要导出目标已存在，拒绝覆盖已有文件")

        kinds = ("candidate", "generation", "evaluation", "group", "call", "control")
        records = {kind: dict(storage.all(kind)) for kind in kinds}
        events = storage.events()
        latest, seen = {}, set()
        for event in events:
            item, identifier = event["payload"], event["record_id"]
            if (event["kind"] == "evaluation" and item["state"] == "completed"
                    and item["purpose"] == "search" and item["stage"] == "standard"
                    and identifier not in seen):
                seen.add(identifier)
                latest[item["candidate_id"]] = item
        valid = [item for item in latest.values() if item["result"]["status"] == "ok"]
        best = min(valid, key=lambda item: (-item["result"]["score"], item["candidate_id"]), default=None)
        calls = records["call"]
        known_tokens, reserved_tokens, unknown_calls = 0, 0, []
        for identifier, call in calls.items():
            total = ((call.get("response") or {}).get("usage") or {}).get("total_tokens")
            if type(total) is int and total >= 0:
                known_tokens += total
            else:
                unknown_calls.append(identifier)
                reserved_tokens += call["plan"]["reservation"]["total_tokens"]
        last_control = next((event["payload"] for event in reversed(events) if event["kind"] == "control"), None)
        groups = []
        for identifier, group in records["group"].items():
            result = group["summary"]
            groups.append({"group_id": identifier, "candidate_id": group["plan"]["candidate_id"],
                           "state": group["state"], "completed_repeats": result["completed_repeats"],
                           "planned_repeats": result["planned_repeats"], "valid_rate": result["valid_rate"],
                           "all_valid_median": result["all_valid_median"],
                           "conditional_score_stats": result["conditional_score_stats"],
                           "path": f"evaluation_groups/{identifier}/summary.json"})
        summary = {
            "format": OVERVIEW_FORMAT, "experiment_id": manifest["experiment_id"],
            "source_event_sequence": events[-1]["sequence"] if events else 0,
            "created_at_utc": manifest["created_at_utc"], "task_id": manifest["config"]["task"]["task_id"],
            "model": manifest["config"]["model"]["model"],
            "backend": manifest["config"]["execution"]["backend"],
            "counts": {kind: len(items) for kind, items in records.items()},
            "usage": {"known_tokens": known_tokens, "reserved_tokens_for_unknown_calls": reserved_tokens,
                      "unknown_usage_call_ids": unknown_calls,
                      "estimated_cost": (last_control or {}).get("usage", {}).get("estimated_cost")},
            "last_control": ({key: last_control.get(key) for key in
                              ("control_id", "state", "stop_reason", "new_generations")} if last_control else None),
            "best": None, "confirmations": groups,
        }
        if best is not None:
            candidate_id, evaluation_id = best["candidate_id"], best["evaluation_attempt_id"]
            candidate_record = records["candidate"][candidate_id]
            code = candidate_record["candidate"]["code"].encode("utf-8")
            if digest(code) != candidate_record["source_sha256"]:
                raise ValueError("最佳候选源码与哈希不一致")
            solution = directory / "evaluations" / evaluation_id / "artifacts" / "solution.json"
            data = solution.read_bytes()
            if digest(data) != best["artifacts"]["artifacts/solution.json"]:
                raise ValueError("最佳候选布局与原评测哈希不一致")
            summary["best"] = {
                "candidate_id": candidate_id, "evaluation_id": evaluation_id, "score": best["result"]["score"],
                "selection_basis": "latest-completed-standard-search-evaluation-per-candidate",
                "source_sha256": digest(code), "solution_sha256": digest(data),
                "source": f"candidates/{candidate_id}/candidate.py",
                "evaluation": f"evaluations/{evaluation_id}/evaluation.json",
                "solution": solution.relative_to(directory).as_posix(),
            }
            atomic_bytes(directory / "best.py", code)
            atomic_bytes(directory / "best_solution.json", data)
        elif owned:
            # 旧摘要的最佳候选可能在后续复测失败，不保留过期的最佳副本。
            for name in ("best.py", "best_solution.json"):
                (directory / name).unlink(missing_ok=True)
        atomic_bytes(previous, encode_json(summary))
        atomic_bytes(directory / "README.md", _overview_markdown(summary, directory.parent.name).encode("utf-8"))
        return summary


def _overview_markdown(summary, parent_name):
    counts, usage = summary["counts"], summary["usage"]
    best, control = summary["best"], summary["last_control"]
    lines = [f"# 实验 {summary['experiment_id']}", "",
             "本页及 summary.json、best.py、best_solution.json 是可重新生成的展示文件。", "",
             f"任务：{summary['task_id']}；模型：{summary['model']}；后端：{summary['backend']}。", "",
             f"生成尝试 {counts['generation']} 次；候选 {counts['candidate']} 个；模型调用 {counts['call']} 次；评测 {counts['evaluation']} 次。", "",
             f"已知 Token：{usage['known_tokens']}；用量未知的调用：{len(usage['unknown_usage_call_ids'])} 次。费用以服务平台账单为准。", ""]
    if usage["unknown_usage_call_ids"]:
        lines += [f"未知调用仍占用 {usage['reserved_tokens_for_unknown_calls']} Token 预算预留；真实用量和计费未知，不自动重发。", ""]
    if control:
        lines += [f"最近控制状态：{control['state']}；停止原因：{control['stop_reason']}。", ""]
    if best:
        lines += [f"当前搜索最佳：**{best['score']}**。按各候选最近一次完整搜索评测排序，不混入复核分数。", "",
                  "- [最佳程序](best.py)", "- [对应的实际输出布局](best_solution.json)",
                  f"- [原始评测]({best['evaluation']})", ""]
    else:
        lines += ["当前没有合法的搜索评测结果，未导出最佳程序或布局。", ""]
    for group in summary["confirmations"]:
        lines += [f"复核 {group['completed_repeats']}/{group['planned_repeats']} 次，状态 {group['state']}，合法率 {group['valid_rate']}，全部合法时中位分数 {group['all_valid_median']}。[复核记录]({group['path']})", ""]
    lines += ["详细记录入口：", "", "- [生成计划与模型响应](generations/)",
              "- [全部候选](candidates/)", "- [执行、评测与日志](evaluations/)",
              "- [岛屿档案](archives/)", "- [调用账本](calls/)",
              "- [配置与环境清单](manifest.json)", "",
              "这些摘要副本不参与父代采样或恢复；完整记录仍保存在 SQLite、.objects 和原始目录。", "",
              "刷新本页（不调用模型或执行候选）：", "", "```powershell",
              f"python -X utf8 -m Examples.summarize_experiment --experiment {parent_name}/{summary['experiment_id']}",
              "```", ""]
    return "\n".join(lines)


def refresh_overview(directory):
    """CLI 结束时刷新展示；展示失败不能覆盖搜索本身的异常。"""
    try:
        summary = write_overview(directory)
    except Exception as exc:
        print(f"摘要未更新（{type(exc).__name__}）；原始运行记录保留，可用 summarize_experiment 检查。")
    else:
        print("实验摘要：", Path(directory) / "README.md")
        return summary
