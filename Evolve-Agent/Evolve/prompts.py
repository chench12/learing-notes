import json
from dataclasses import asdict

from Evolve.contracts import Candidate, Evaluation, TaskSpec


def build_messages(
    task: TaskSpec,
    parent: Candidate,
    evaluation: Evaluation,
    *,
    mode="patch",
):
    if parent.task_id != task.task_id:
        raise ValueError("父代与任务不匹配")

    if evaluation.candidate_id != parent.candidate_id:
        raise ValueError("评测结果与父代不匹配")

    if mode == "patch":
        output_rules = (
            "输出一个或多个 SEARCH/REPLACE 补丁，允许在块外简短解释思路。\n"
            "每个补丁格式如下，三个分隔符必须独占一行且不缩进：\n"
            "<<<<<<< SEARCH\n"
            "父代中需要替换的原始完整代码行\n"
            "=======\n"
            "替换后的完整代码行\n"
            ">>>>>>> REPLACE\n"
            "SEARCH 必须逐字匹配，包括缩进和空白，并且在当前文件中仅出现一次。"
            "有重复时加入更多块内上下文。SEARCH 不能为空；删除代码时 REPLACE 可以为空。"
            "补丁按顺序应用，后续 SEARCH 匹配前面补丁应用后的代码。"
            "每个 SEARCH 片段必须完整位于一个可进化块内部，不得包含边界标记或固定代码。"
            "增加函数时替换现有块内片段；不要输出整个文件。"
        )
    elif mode == "rewrite":
        output_rules = "只返回包括固定骨架和进化标记在内的完整 Python 文件，不要解释或 Markdown 标记。"
    else:
        raise ValueError("未知生成模式")

    return [
        {
            "role": "system",
            "content": (
                "你负责改进解决指定任务的 Python 程序。"
                "只允许修改 # EVOLVE-BLOCK-START 与 # EVOLVE-BLOCK-END 之间的代码。"
                "允许同时修改多个进化块以及块内的多个函数。"
                "保留所有标记的数量、顺序及标记行原文。"
                "进化块之外的所有代码、注释和空白必须逐字保留，包括固定 solve() 入口。"
                "solve() 返回的解必须能够被 JSON 序列化。"
                "本次仅允许使用 Python 标准库。"
                "不访问网络、文件、环境变量，不创建子进程。"
                "评测代码和评测规则不可修改。"
                f"\n输出协议：\n{output_rules}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"任务说明：\n{task.description}\n\n"
                f"候选运行时间上限：{task.timeout_seconds} 秒\n\n"
                "父代评测结果：\n"
                f"{json.dumps(asdict(evaluation), ensure_ascii=False)}\n\n"
                f"父代程序：\n```python\n{parent.code}\n```\n\n"
                "请在可进化区域内实现改进，争取获得更高的合法得分。"
                "严格按输出协议提交修改。"
            ),
        },
    ]
