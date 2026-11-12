"""解析逐行 SEARCH/REPLACE 补丁，在内存中精确应用。"""

from dataclasses import dataclass

from Evolve.blocks import SkeletonError, normalize_source, parse_blocks


SEARCH = "<<<<<<< SEARCH"
SEPARATOR = "======="
REPLACE = ">>>>>>> REPLACE"
DELIMITERS = {SEARCH, SEPARATOR, REPLACE}


class PatchParseError(ValueError):
    pass


class PatchApplyError(ValueError):
    pass


@dataclass(frozen=True)
class Patch:
    search: str
    replacement: str


def parse_patches(text):
    """允许块外解释；块内缩进和空白原样保留。"""
    if not isinstance(text, str) or not text.strip():
        raise PatchParseError("响应没有补丁文本")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    patches = []
    state = "outside"
    search = []
    replacement = []
    for number, line in enumerate(lines, 1):
        marker = line.rstrip("\n")
        # 分隔符必须独占一行，不自动修正其缩进或尾部空格。
        if marker.strip() in DELIMITERS and marker not in DELIMITERS:
            raise PatchParseError(f"第 {number} 行补丁分隔符含有多余空白")
        if state == "outside":
            if marker == SEARCH:
                state = "search"
                search, replacement = [], []
            elif marker in DELIMITERS:
                raise PatchParseError(f"第 {number} 行补丁分隔符顺序错误")
        elif state == "search":
            if marker == SEPARATOR:
                if not "".join(search).strip():
                    raise PatchParseError("SEARCH 片段不能为空")
                state = "replacement"
            elif marker in DELIMITERS:
                raise PatchParseError(f"第 {number} 行 SEARCH 块未正确结束")
            else:
                search.append(line)
        else:
            if marker == REPLACE:
                patches.append(Patch("".join(search), "".join(replacement)))
                state = "outside"
            elif marker in DELIMITERS:
                raise PatchParseError(f"第 {number} 行 REPLACE 块未正确结束")
            else:
                replacement.append(line)
    if state != "outside":
        raise PatchParseError("补丁未闭合，可能被截断")
    if not patches:
        raise PatchParseError("没有找到 SEARCH/REPLACE 补丁")
    return patches


def _editable_ranges(source):
    blocks = parse_blocks(source)
    cursor = 0
    ranges = []
    for fixed, editable in zip(blocks.fixed, blocks.editable):
        cursor += len(fixed)
        ranges.append((cursor, cursor + len(editable)))
        cursor += len(editable)
    return ranges


def apply_patches(parent_code, patches):
    """全部成功才返回子代；不写文件，不修改父代。"""
    if not patches:
        raise PatchApplyError("补丁列表不能为空")
    working = normalize_source(parent_code)
    for index, patch in enumerate(patches, 1):
        if not patch.search.strip():
            raise PatchApplyError(f"补丁 {index} 的 SEARCH 片段为空")
        position = working.find(patch.search)
        if position < 0:
            raise PatchApplyError(f"补丁 {index} 未找到精确匹配；不进行模糊匹配")
        if working.find(patch.search, position + 1) >= 0:
            raise PatchApplyError(f"补丁 {index} 匹配多处，请提供更多上下文")
        if patch.search == patch.replacement:
            raise PatchApplyError(f"补丁 {index} 没有修改代码")
        try:
            ranges = _editable_ranges(working)
        except SkeletonError as exc:
            raise PatchApplyError(f"补丁 {index} 应用前的进化区域无法解析") from exc
        end = position + len(patch.search)
        if not any(start <= position and end <= stop for start, stop in ranges):
            raise PatchApplyError(f"补丁 {index} 越过可进化区域；SEARCH 不得包含标记或固定骨架")
        working = working[:position] + patch.replacement + working[end:]
    return normalize_source(working)
