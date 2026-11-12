"""可选模块的显式默认值；创建实验时展开并冻结，不改变旧清单。"""

from copy import deepcopy
import math


DEFAULTS = {
    "context": {
        "inspirations": 2, "max_bytes": 60000, "reference_max_bytes": 8000,
        "diagnostic_max_bytes": 1500,
        "templates": [
            {"id": "improve", "weight": 2.0, "instruction": "分析父代的数值瓶颈，提出并实现可验证的改进。"},
            {"id": "alternative", "weight": 1.0, "instruction": "探索与父代不同的构造或搜索策略，保留有效思路并避免只改变常数。"},
        ],
    },
    "budget": {"max_calls": 100, "max_tokens": 1000000, "max_evaluation_seconds": 3600.0,
               "max_wall_seconds": 7200.0, "max_estimated_cost": None,
               "input_cost_per_million": None, "output_cost_per_million": None,
               "price_source": None},
    "routing": {"models": []},
    "confirmation": {"enabled": False, "repeats": 3, "min_improvement": 0.0},
    "meta": {"enabled": False, "interval": 10, "max_tokens": 512,
             "max_instruction_bytes": 2000, "evaluation_window": 3},
    "pipeline": {"mode": "serial", "max_inflight": 4, "model_concurrency": 2,
                 "evaluation_concurrency": 1},
}


def expanded_config(config):
    result = deepcopy(config)
    for name, defaults in DEFAULTS.items():
        if name not in result:
            result[name] = deepcopy(defaults)
    return result


def validate_extensions(config):
    for name, defaults in DEFAULTS.items():
        if not isinstance(config[name], dict) or set(config[name]) != set(defaults):
            raise ValueError(f"{name} 配置字段与约定不一致")

    def positive(value, name, zero=False, integer=False):
        if (type(value) not in ((int,) if integer else (int, float))
                or not math.isfinite(value) or (value < 0 if zero else value <= 0)):
            raise ValueError(f"{name} 数值不合法")

    context = config["context"]
    for key in ("inspirations", "reference_max_bytes", "diagnostic_max_bytes"):
        positive(context[key], key, zero=True, integer=True)
    positive(context["max_bytes"], "context.max_bytes", integer=True)
    if not isinstance(context["templates"], list) or not context["templates"]:
        raise ValueError("需要至少一个提示词模板")
    ids = set()
    for template in context["templates"]:
        if not isinstance(template, dict) or set(template) != {"id", "weight", "instruction"}:
            raise ValueError("模板字段不一致")
        if not isinstance(template["id"], str) or not template["id"] or template["id"] in ids:
            raise ValueError("模板 ID 必须非空且不重复")
        ids.add(template["id"])
        if not isinstance(template["instruction"], str) or not template["instruction"].strip():
            raise ValueError("模板改进指令不能为空")
        positive(template["weight"], "template.weight")
    budget = config["budget"]
    for key in ("max_calls", "max_tokens"):
        positive(budget[key], key, integer=True)
    for key in ("max_evaluation_seconds", "max_wall_seconds"):
        positive(budget[key], key)
    prices = [budget["input_cost_per_million"], budget["output_cost_per_million"]]
    if any(value is not None for value in prices):
        if not all(value is not None for value in prices):
            raise ValueError("必须同时配置输入/输出单价")
        for value in prices:
            positive(value, "price", zero=True)
        if not isinstance(budget["price_source"], str) or not budget["price_source"].strip():
            raise ValueError("价格必须说明来源及日期，不能冒充已验证账单")
    if budget["max_estimated_cost"] is not None:
        positive(budget["max_estimated_cost"], "max_estimated_cost")
        if prices[0] is None:
            raise ValueError("费用预算需要配置参考单价")
    models = config["routing"]["models"]
    if not isinstance(models, list):
        raise ValueError("routing.models 必须为列表")
    for model in models:
        if not isinstance(model, dict) or set(model) != {"id", "weight", "base_url", "model", "key_env", "max_tokens"}:
            raise ValueError("路由模型字段不一致")
        for key in ("id", "base_url", "model", "key_env"):
            if not isinstance(model[key], str) or not model[key].strip():
                raise ValueError("路由模型标识不能为空")
        positive(model["weight"], "model.weight")
        positive(model["max_tokens"], "model.max_tokens", integer=True)
    if len({model["id"] for model in models}) != len(models):
        raise ValueError("路由模型 ID 重复")
    for name in ("confirmation", "meta"):
        if type(config[name]["enabled"]) is not bool:
            raise ValueError(f"{name}.enabled 必须是 bool")
    positive(config["confirmation"]["repeats"], "repeats", integer=True)
    positive(config["confirmation"]["min_improvement"], "min_improvement", zero=True)
    for key in ("interval", "max_tokens", "max_instruction_bytes", "evaluation_window"):
        positive(config["meta"][key], key, integer=True)
    pipeline = config["pipeline"]
    if pipeline["mode"] not in {"serial", "async"}:
        raise ValueError("pipeline.mode 必须为 serial 或 async")
    for key in ("max_inflight", "model_concurrency", "evaluation_concurrency"):
        positive(pipeline[key], f"pipeline.{key}", integer=True)
    if max(pipeline["model_concurrency"], pipeline["evaluation_concurrency"]) > pipeline["max_inflight"]:
        raise ValueError("工作节点并发不能超过 max_inflight")
