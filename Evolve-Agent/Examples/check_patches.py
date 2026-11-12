"""应用一个手写补丁，再演示失败补丁；不调用模型或执行候选。"""

from pathlib import Path

from Evolve.blocks import validate_skeleton
from Evolve.patches import PatchApplyError, apply_patches, parse_patches


def main():
    root = Path(__file__).resolve().parents[1]
    parent = (root / "tasks/circle_packing/initial.py").read_text(encoding="utf-8")
    response = """这里只演示补丁协议，不以提高得分为目标。
<<<<<<< SEARCH
        radius = 1 / 12
=======
        radius = 1 / 13
>>>>>>> REPLACE
"""
    patches = parse_patches(response)
    child = apply_patches(parent, patches)
    validate_skeleton(parent, child)
    print("补丁数：", len(patches))
    print("子代包含新半径：", "radius = 1 / 13" in child)
    print("父代保持原样：", "radius = 1 / 12" in parent)
    print("固定骨架校验：通过")

    bad_response = response.replace("radius = 1 / 12", "radius = 1 / 999")
    try:
        apply_patches(parent, parse_patches(bad_response))
    except PatchApplyError as exc:
        print("无法匹配的补丁：拒绝，", exc)


if __name__ == "__main__":
    main()
