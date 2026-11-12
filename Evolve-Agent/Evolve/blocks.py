"""解析可进化块，并检查候选是否保留固定骨架。"""

from dataclasses import dataclass
from io import StringIO
import tokenize


START = "# EVOLVE-BLOCK-START"
END = "# EVOLVE-BLOCK-END"


class SkeletonError(ValueError):
    pass


def normalize_source(source):
    """统一换行；文件末尾统一保留换行，其他空白不做改写。"""
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    return source if source.endswith("\n") else source + "\n"


@dataclass(frozen=True)
class EvolutionBlocks:
    # fixed 包含边界标记本身，比 editable 多一个元素。
    fixed: tuple[str, ...]
    editable: tuple[str, ...]


def parse_blocks(source):
    source = normalize_source(source)
    lines = source.splitlines(keepends=True)
    markers = []
    try:
        for token in tokenize.generate_tokens(StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            marker = token.string.strip()
            if marker not in (START, END):
                continue
            if token.start[1] != 0 or token.line.strip() != marker:
                raise SkeletonError("进化标记必须独占一行，且位于文件顶层、不缩进")
            markers.append((token.start[0] - 1, marker))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        raise SkeletonError("无法解析进化标记：Python 代码词法不完整") from exc

    fixed = []
    editable = []
    cursor = 0
    opening = None
    for line_number, marker in markers:
        if marker == START:
            if opening is not None:
                raise SkeletonError("不允许嵌套进化块")
            fixed.append("".join(lines[cursor:line_number + 1]))
            opening = line_number + 1
        else:
            if opening is None:
                raise SkeletonError("结束标记没有对应的开始标记")
            editable.append("".join(lines[opening:line_number]))
            cursor = line_number
            opening = None

    if opening is not None:
        raise SkeletonError("进化块缺少结束标记")
    if not editable:
        raise SkeletonError("程序必须包含至少一组进化标记")
    fixed.append("".join(lines[cursor:]))
    return EvolutionBlocks(tuple(fixed), tuple(editable))


def validate_skeleton(reference, candidate):
    """始终对照任务种子骨架，而不只与上一代比较。"""
    expected = parse_blocks(reference)
    actual = parse_blocks(candidate)
    if len(expected.editable) != len(actual.editable):
        raise SkeletonError("候选改变了进化块数量")
    if expected.fixed != actual.fixed:
        raise SkeletonError("候选修改了固定骨架或进化标记")
