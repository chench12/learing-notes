"""评测契约、几何描述特征和重复统计；不调用模型。"""

from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import runpy
import unittest

from Evolve.contracts import Evaluation
from Evolve.evaluation import summarize_repeats
from tasks.circle_packing.evaluator import evaluate, TOLERANCE


PROJECT = Path(__file__).resolve().parents[1]


class EvaluationContractTests(unittest.TestCase):
    def test_structured_result_roundtrips_without_collapsing_features_into_score(self):
        result = Evaluation("candidate", "ok", score=2.5, objectives={"quality": 2.5},
                            constraints={"feasibility": -1e-9}, features={"spread": 0.2})
        self.assertEqual(Evaluation(**json.loads(json.dumps(asdict(result)))), result)
        self.assertEqual(result.score, 2.5)

    def test_nonfinite_bool_or_failed_features_are_rejected(self):
        cases = [
            {"score": True}, {"score": float("nan")},
            {"metrics": {"x": float("inf")}}, {"features": {"x": True}},
            {"objectives": {"": 1}}, {"constraints": {"overlap": 0.1}},
            {"stage": ""}, {"candidate_id": ""},
            {"status": "invalid", "score": None, "features": {"x": 1}},
            {"status": "error", "score": None, "objectives": {"x": 1}},
        ]
        for fields in cases:
            values = {"candidate_id": "candidate", "status": "ok", "score": 1}
            values.update(fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                Evaluation(**values)


class CircleEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.circles = runpy.run_path(str(PROJECT / "tasks/circle_packing/initial.py"))["solve"]()

    def test_seed_objective_constraints_and_features_have_distinct_roles(self):
        result = evaluate(self.circles, "seed")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.stage, "standard")
        self.assertAlmostEqual(result.objectives["sum_radii"], 26 / 12)
        self.assertEqual(result.objectives["sum_radii"], result.score)
        self.assertTrue(all(value <= 0 for value in result.constraints.values()))
        self.assertAlmostEqual(result.features["radius_cv"], 0)
        self.assertAlmostEqual(result.features["boundary_fraction"], 13 / 26)

    def test_uniformly_smaller_circles_are_not_near_boundary(self):
        circles = [[x, y, radius / 2] for x, y, radius in self.circles]
        result = evaluate(circles, "small")
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.features["radius_cv"], 0)
        self.assertEqual(result.features["boundary_fraction"], 0)
        self.assertAlmostEqual(result.score, 26 / 24)

    def test_unequal_radii_have_positive_cv(self):
        circles = [[x, y, radius / (2 if index % 2 else 1)]
                   for index, (x, y, radius) in enumerate(self.circles)]
        result = evaluate(circles, "unequal")
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.features["radius_cv"], 1 / 3)

    def test_boundary_tolerance_is_separate_from_descriptor_contact_threshold(self):
        circles = [list(circle) for circle in self.circles]
        circles[0][0] -= 0.5 * TOLERANCE
        accepted = evaluate(circles, "within-tolerance")
        self.assertEqual(accepted.status, "ok")
        self.assertLessEqual(accepted.constraints["boundary"], 0)
        circles[0][0] -= 2 * TOLERANCE
        rejected = evaluate(circles, "outside-tolerance")
        self.assertEqual(rejected.status, "invalid")
        self.assertGreater(rejected.constraints["boundary"], 0)
        self.assertEqual(rejected.features, {})
        self.assertEqual(rejected.objectives, {})

    def test_overlap_reports_positive_residual_without_quality_or_archive_features(self):
        circles = [list(circle) for circle in self.circles]
        circles[1][:2] = circles[0][:2]
        result = evaluate(circles, "overlap")
        self.assertEqual(result.status, "invalid")
        self.assertIsNone(result.score)
        self.assertGreater(result.constraints["non_overlap"], 0)
        self.assertEqual(result.features, {})

    def test_extreme_or_bad_values_return_invalid_serializable_results(self):
        for value in [1e308, float("nan"), float("inf"), True, 10 ** 1000]:
            circles = [list(circle) for circle in self.circles]
            circles[0][0] = value
            result = evaluate(circles, "bad")
            self.assertEqual(result.status, "invalid")
            json.dumps(asdict(result), allow_nan=False)

    def test_tiny_radii_do_not_underflow_cv_computation(self):
        circles = [[x, y, 5e-324] for x, y, _ in self.circles]
        result = evaluate(circles, "tiny")
        self.assertEqual(result.status, "ok")
        self.assertTrue(math.isfinite(result.features["radius_cv"]))
        self.assertAlmostEqual(result.features["radius_cv"], 0)


class RepeatStatisticsTests(unittest.TestCase):
    def ok(self, score, **fields):
        return Evaluation("candidate", "ok", score=score, **fields)

    def test_complete_group_reports_mean_median_variation_and_best_attempt(self):
        summary = summarize_repeats("candidate", [("a", self.ok(1)), ("b", self.ok(3)), ("c", self.ok(2))], 3)
        self.assertEqual(summary["valid_rate"], 1)
        self.assertEqual(summary["all_valid_median"], 2)
        self.assertEqual(summary["conditional_score_stats"]["mean"], 2)
        self.assertEqual(summary["conditional_score_stats"]["sample_stdev"], 1)
        self.assertEqual(summary["best_evaluation_attempt_id"], "b")

    def test_failure_is_counted_without_zero_imputation_or_hidden_dropping(self):
        attempts = [("a", self.ok(4)), ("b", Evaluation("candidate", "invalid")),
                    ("c", Evaluation("candidate", "timeout"))]
        summary = summarize_repeats("candidate", attempts, 3)
        self.assertAlmostEqual(summary["valid_rate"], 1 / 3)
        self.assertEqual(summary["conditional_score_stats"]["mean"], 4)
        self.assertIsNone(summary["conditional_score_stats"]["sample_stdev"])
        self.assertIsNone(summary["all_valid_median"])
        self.assertEqual(summary["status_counts"], {"ok": 1, "invalid": 1, "timeout": 1})

    def test_incomplete_group_does_not_report_final_valid_rate(self):
        summary = summarize_repeats("candidate", [("a", self.ok(4))], 3)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["observed_valid_rate"], 1)
        self.assertIsNone(summary["valid_rate"])
        self.assertIsNone(summary["all_valid_median"])

    def test_zero_score_is_valid_and_all_failed_or_empty_groups_have_no_score(self):
        zero = summarize_repeats("candidate", [("a", self.ok(0))], 1)
        self.assertEqual(zero["all_valid_median"], 0)
        failed = summarize_repeats("candidate", [("a", Evaluation("candidate", "error"))], 1)
        self.assertEqual(failed["valid_rate"], 0)
        self.assertIsNone(failed["conditional_score_stats"]["max"])
        self.assertIsNone(failed["best_evaluation_attempt_id"])
        empty = summarize_repeats("candidate", [], 3)
        self.assertIsNone(empty["observed_valid_rate"])

    def test_mixed_candidate_stage_duplicate_attempt_or_bad_count_is_rejected(self):
        cases = [
            ([("a", self.ok(1)), ("a", self.ok(2))], 2),
            ([("a", replace(self.ok(1), candidate_id="another"))], 1),
            ([("a", self.ok(1, stage="low_budget"))], 1),
            ([("a", self.ok(1))], 0), ([], True),
        ]
        for attempts, planned in cases:
            with self.subTest(planned=planned), self.assertRaises(ValueError):
                summarize_repeats("candidate", attempts, planned)

    def test_extreme_finite_scores_do_not_create_infinite_json_statistics(self):
        same = summarize_repeats("candidate", [("a", self.ok(1e308)), ("b", self.ok(1e308))], 2)
        self.assertEqual(same["all_valid_median"], 1e308)
        varied = summarize_repeats("candidate", [("a", self.ok(-1.7e308)), ("b", self.ok(1.7e308))], 2)
        self.assertIsNone(varied["conditional_score_stats"]["sample_stdev"])
        self.assertIn("sample_stdev", varied["unrepresentable_statistics"])
        json.dumps(varied, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
