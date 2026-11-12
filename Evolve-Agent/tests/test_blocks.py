import unittest

from Evolve.blocks import SkeletonError, parse_blocks, validate_skeleton


SOURCE = '''"""固定模块说明。"""
# EVOLVE-BLOCK-START
import math
# EVOLVE-BLOCK-END

# EVOLVE-BLOCK-START
def initialize():
    return []

def optimize(circles):
    return circles
# EVOLVE-BLOCK-END

def solve():
    return optimize(initialize())
'''


class BlocksTests(unittest.TestCase):
    def test_allows_changes_across_multiple_blocks_and_functions(self):
        child = SOURCE.replace("import math", "import math\nimport random")
        child = child.replace("return []", "return helper()")
        child = child.replace(
            "def optimize(circles):", "def helper():\n    return []\n\ndef optimize(circles):",
        )
        validate_skeleton(SOURCE, child)
        self.assertEqual(len(parse_blocks(child).editable), 2)

    def test_rejects_changes_to_fixed_entrypoint_comments_and_whitespace(self):
        for before, after in [
            ("return optimize(initialize())", "return []"),
            ("固定模块说明", "不同说明"),
            ("\ndef solve():", "\n\ndef solve():"),
        ]:
            with self.subTest(before=before), self.assertRaises(SkeletonError):
                validate_skeleton(SOURCE, SOURCE.replace(before, after))

    def test_rejects_missing_unmatched_nested_or_indented_markers(self):
        examples = [
            "def solve(): return []\n",
            "# EVOLVE-BLOCK-END\n",
            "# EVOLVE-BLOCK-START\nx = 1\n",
            "# EVOLVE-BLOCK-START\n# EVOLVE-BLOCK-START\n# EVOLVE-BLOCK-END\n",
            " # EVOLVE-BLOCK-START\n# EVOLVE-BLOCK-END\n",
            "x = 1  # EVOLVE-BLOCK-START\n# EVOLVE-BLOCK-END\n",
        ]
        for source in examples:
            with self.subTest(source=source), self.assertRaises(SkeletonError):
                parse_blocks(source)

    def test_marker_text_inside_strings_is_not_a_real_marker(self):
        text = 'text = """\n# EVOLVE-BLOCK-START\n# EVOLVE-BLOCK-END\n"""\n'
        with self.assertRaises(SkeletonError):
            parse_blocks(text)
        self.assertEqual(len(parse_blocks(text + SOURCE).editable), 2)

    def test_cannot_hide_a_real_end_marker_in_a_string(self):
        hidden = SOURCE.replace("import math", 'text = """', 1)
        hidden = hidden.replace("def initialize():", '"""\ndef initialize():', 1)
        with self.assertRaises(SkeletonError):
            validate_skeleton(SOURCE, hidden)

    def test_line_endings_and_missing_final_newline_are_normalized(self):
        validate_skeleton(SOURCE, SOURCE.replace("\n", "\r\n"))
        validate_skeleton(SOURCE, SOURCE.rstrip("\n"))

    def test_empty_editable_block_is_valid(self):
        self.assertEqual(
            parse_blocks("# EVOLVE-BLOCK-START\n# EVOLVE-BLOCK-END\n").editable,
            ("",),
        )

    def test_added_block_or_modified_marker_line_is_rejected(self):
        for child in [
            SOURCE + "# EVOLVE-BLOCK-START\nx = 1\n# EVOLVE-BLOCK-END\n",
            SOURCE.replace("# EVOLVE-BLOCK-START\n", "# EVOLVE-BLOCK-START  \n", 1),
        ]:
            with self.subTest(child=child), self.assertRaises(SkeletonError):
                validate_skeleton(SOURCE, child)


if __name__ == "__main__":
    unittest.main()
