import unittest

from Evolve.patches import (
    Patch, PatchApplyError, PatchParseError, apply_patches, parse_patches,
)


SOURCE = '''# 固定说明
# EVOLVE-BLOCK-START
x = 1
y = 2
# EVOLVE-BLOCK-END

# EVOLVE-BLOCK-START
def helper():
    return x + y
# EVOLVE-BLOCK-END

def solve():
    return helper()
'''


def block(search, replacement):
    return f"<<<<<<< SEARCH\n{search}=======\n{replacement}>>>>>>> REPLACE\n"


class PatchesTests(unittest.TestCase):
    def test_parses_explanation_multiple_blocks_and_preserves_indentation(self):
        text = "思路说明。\n" + block("x = 1\n", "x = 3\n")
        text += "第二项修改。\n" + block("    return x + y\n", "    return x * y\n")
        patches = parse_patches(text)
        self.assertEqual(len(patches), 2)
        self.assertEqual(patches[1].replacement, "    return x * y\n")
        child = apply_patches(SOURCE, patches)
        self.assertIn("x = 3", child)
        self.assertIn("return x * y", child)

    def test_sequential_searches_use_updated_source(self):
        child = apply_patches(SOURCE, [Patch("x = 1\n", "x = 3\n"), Patch("x = 3\n", "x = 4\n")])
        self.assertIn("x = 4\n", child)

    def test_deletion_is_allowed_but_empty_search_is_not(self):
        patches = parse_patches(block("y = 2\n", ""))
        self.assertNotIn("y = 2", apply_patches(SOURCE, patches))
        with self.assertRaises(PatchParseError):
            parse_patches(block("", "x = 3\n"))
        with self.assertRaises(PatchApplyError):
            apply_patches(SOURCE, [Patch("", "x = 3\n")])

    def test_missing_or_ambiguous_match_is_rejected(self):
        for source, search in [
            (SOURCE, "x = 10\n"),
            (SOURCE.replace("y = 2", "x = 1"), "x = 1\n"),
            (SOURCE, " x = 1\n"),
        ]:
            with self.subTest(search=search), self.assertRaises(PatchApplyError):
                apply_patches(source, [Patch(search, "x = 4\n")])

    def test_no_partial_result_or_parent_mutation_when_later_patch_fails(self):
        original = SOURCE
        with self.assertRaises(PatchApplyError):
            apply_patches(SOURCE, [Patch("x = 1\n", "x = 3\n"), Patch("missing\n", "other\n")])
        self.assertEqual(SOURCE, original)

    def test_fixed_code_and_marker_edits_are_rejected(self):
        for search in ["# 固定说明\n", "# EVOLVE-BLOCK-START\nx = 1\n", "def solve():\n    return helper()\n"]:
            with self.subTest(search=search), self.assertRaisesRegex(PatchApplyError, "越过"):
                apply_patches(SOURCE, [Patch(search, "# changed\n")])

    def test_malformed_and_truncated_blocks_are_rejected(self):
        for text in [
            "没有补丁", "<<<<<<< SEARCH\nx = 1\n", "=======\n",
            ">>>>>>> REPLACE\n", "<<<<<<< SEARCH\nx = 1\n>>>>>>> REPLACE\n",
            "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n",
            "<<<<<<< SEARCH\nx = 1\n<<<<<<< SEARCH\n",
            " <<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n",
        ]:
            with self.subTest(text=text), self.assertRaises(PatchParseError):
                parse_patches(text)

    def test_empty_list_and_no_change_are_rejected(self):
        with self.assertRaises(PatchApplyError):
            apply_patches(SOURCE, [])
        with self.assertRaises(PatchApplyError):
            apply_patches(SOURCE, [Patch("x = 1\n", "x = 1\n")])

    def test_crlf_and_missing_final_delimiter_newline(self):
        text = block("x = 1\n", "x = 3\n").rstrip("\n").replace("\n", "\r\n")
        self.assertIn("x = 3\n", apply_patches(SOURCE.replace("\n", "\r\n"), parse_patches(text)))


if __name__ == "__main__":
    unittest.main()
