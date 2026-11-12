"""离线展示合法进化、修改固定代码、删除标记三种情况。"""

from pathlib import Path

from Evolve.blocks import SkeletonError, parse_blocks, validate_skeleton


def main():
    root = Path(__file__).resolve().parents[1]
    reference = (root / "tasks/circle_packing/initial.py").read_text(encoding="utf-8")
    print("可进化块数量：", len(parse_blocks(reference).editable))

    examples = {
        "块内修改": reference.replace("radius = 1 / 12", "radius = 1 / 13"),
        "修改固定入口": reference.replace("return optimize(initialize())", "return []"),
        "删除标记": reference.replace("# EVOLVE-BLOCK-END", "", 1),
    }
    for name, code in examples.items():
        try:
            validate_skeleton(reference, code)
        except SkeletonError as exc:
            print(f"{name}：拒绝，{exc}")
        else:
            print(f"{name}：通过骨架校验（不代表成绩更好）")


if __name__ == "__main__":
    main()
