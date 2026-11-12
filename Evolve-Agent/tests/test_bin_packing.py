"""第二任务的独立整数验证、公开基线与任务协议。"""

from copy import deepcopy
import unittest

from tasks.bin_packing.initial import solve
from tasks.bin_packing.evaluator import evaluate


class BinPackingTests(unittest.TestCase):
    def test_first_fit_valid_and_lower_bound_not_reported_as_optimum(self):
        result = evaluate(solve(), "first-fit")
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.score, 0.9104393473993456)
        self.assertEqual(result.metrics["total_bins"], 729)
        self.assertEqual(result.metrics["capacity_lower_bound"], 664)
        self.assertEqual(set(result.features), {"mean_waste", "waste_cv"})
        self.assertIn("不等于", result.feedback)

    def test_missing_extra_or_invalid_assignments_are_rejected(self):
        solution = solve()
        bad = deepcopy(solution)
        bad[0][0] = True
        wrong_length = deepcopy(solution)
        wrong_length[0].pop()
        non_online = deepcopy(solution)
        non_online[0][0] = 1
        overload = deepcopy(solution)
        overload[0] = [0] * 120
        for item in [[], solution[:-1], bad, wrong_length, non_online, overload]:
            result = evaluate(item, "bad")
            self.assertEqual(result.status, "invalid")
            self.assertIsNone(result.score)
            self.assertEqual(result.features, {})


if __name__ == "__main__":
    unittest.main()
