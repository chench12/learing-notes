"""三策略的选择语义与搜索反馈重建；不需要模型或候选执行。"""

from copy import deepcopy
from dataclasses import asdict
import json
import math
from pathlib import Path
import unittest

from Evolve.archive import validate_archive_config
from Evolve.contracts import Evaluation
from Evolve.selection import rebuild_search_statistics, sample_parent, selection_weights, temperature
from Examples.compare_selection import mechanism_demo


PROJECT = Path(__file__).resolve().parents[1]


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((PROJECT / "configs/circle_packing.json").read_text(encoding="utf-8"))["archive"]
        self.cells = [{"candidate_id": name, "score": score, "cell": [index],
                       "evaluation_attempt_id": f"eval_{name}"}
                      for index, (name, score) in enumerate([("a", 2.6), ("b", 2.5), ("c", 2.3)])]
        self.statistics = {"expansions": 0, "candidates": {}}

    def choose(self, method):
        self.config["parent_sampling"] = method
        return selection_weights(self.cells, self.config, self.statistics)

    def test_uniform_does_not_change_with_rewards_or_visits(self):
        before = self.choose("uniform_cell")[0]
        self.statistics.update(expansions=1000, candidates={"a": {"visits": 1000, "best_child_score": 100}})
        self.assertEqual(self.choose("uniform_cell")[0], before)
        self.assertEqual(before, [1 / 3] * 3)

    def test_softmax_matches_formula_and_temperature_cycles(self):
        weights, detail = self.choose("softmax_cell")
        expected = [math.exp(score / 0.25) for score in [2.6, 2.5, 2.3]]
        for actual, value in zip(weights, expected):
            self.assertAlmostEqual(actual, value / sum(expected))
        initial = detail["temperature"]
        self.statistics["expansions"] = self.config["temperature_period"] - 1
        cold, detail = self.choose("softmax_cell")
        self.assertLess(detail["temperature"], initial)
        self.assertGreater(cold[0], weights[0])
        self.assertEqual(temperature(self.config, self.config["temperature_period"]), initial)

    def test_softmax_stability_with_extreme_finite_scores(self):
        self.cells[0]["score"] = 1e308
        self.cells[1]["score"] = -1e308
        weights, _ = self.choose("softmax_cell")
        self.assertEqual(weights, [1, 0, 0])

    def test_puct_rewards_productive_parent_and_penalizes_many_visits(self):
        self.statistics.update(expansions=65, candidates={
            "a": {"visits": 60, "best_child_score": 2.60},
            "b": {"visits": 5, "best_child_score": 2.64}})
        weights, detail = self.choose("puct_cell")
        self.assertEqual(weights, [0, 1, 0])
        b = detail["candidates"][1]
        self.assertEqual(b["q"], 2.64)
        self.assertAlmostEqual(b["prior"], 2 / 6)
        self.assertEqual(b["visits"], 5)
        # 乘回公共单位，应等于附录 PUCT 公式（当前分数范围为 0.3）。
        expected = 2.64 + self.config["puct_c"] * 0.3 * (2 / 6) * math.sqrt(66) / 6
        self.assertAlmostEqual(b["utility_scaled"] * detail["utility_unit"], expected)

    def test_puct_can_select_neglected_lower_reward_parent(self):
        self.statistics.update(expansions=100, candidates={
            "a": {"visits": 50, "best_child_score": 2.60},
            "b": {"visits": 50, "best_child_score": 2.64}})
        self.assertEqual(self.choose("puct_cell")[0], [0, 0, 1])

    def test_puct_failed_children_fallback_to_own_score_and_flat_rewards_keep_exploring(self):
        for item in self.cells:
            item["score"] = 2
        self.statistics.update(expansions=100, candidates={
            "a": {"visits": 100, "best_child_score": None}})
        weights, detail = self.choose("puct_cell")
        self.assertEqual(weights, [0, 1, 0])
        self.assertTrue(detail["flat_reward_fallback"])
        self.assertEqual(detail["candidates"][0]["q"], 2)

    def test_puct_extreme_reward_range_stays_finite(self):
        self.cells[0]["score"] = 1e308
        self.cells[1]["score"] = -1e308
        weights, detail = self.choose("puct_cell")
        self.assertAlmostEqual(sum(weights), 1)
        self.assertTrue(all(math.isfinite(row["utility_scaled"]) for row in detail["candidates"]))

    def test_methods_reproduce_and_only_current_island_is_eligible(self):
        snapshot = {"islands": [{"cells": self.cells}, {"cells": [self.cells[2]]}]}
        statistics = {"islands": [self.statistics, self.statistics]}
        for method in ["uniform_cell", "softmax_cell", "puct_cell"]:
            self.config["parent_sampling"] = method
            first = sample_parent(snapshot, seed=42, selection_index=0, config=self.config, statistics=statistics)
            again = sample_parent(deepcopy(snapshot), seed=42, selection_index=0, config=self.config, statistics=deepcopy(statistics))
            self.assertEqual(first, again)
            next_island = sample_parent(snapshot, seed=42, selection_index=1, config=self.config, statistics=statistics)
            self.assertEqual(next_island["parent_id"], "c")
            self.assertIn("diagnostics", first)

    def test_feedback_strategies_refuse_missing_statistics(self):
        self.config["parent_sampling"] = "puct_cell"
        with self.assertRaisesRegex(ValueError, "统计"):
            sample_parent({"islands": [{"cells": self.cells}]}, seed=42, selection_index=0, config=self.config)

    def test_invalid_sampling_parameters_rejected(self):
        for key, value in [("parent_sampling", "unknown"), ("temperature_period", 0),
                           ("temperature_initial", 0), ("puct_c", float("nan")), ("puct_c", True)]:
            config = deepcopy(self.config)
            config[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_archive_config(config)

    def test_illustrative_demo_has_expected_distinct_behavior(self):
        result = mechanism_demo(self.config, draws=40)
        rows = {(row["scenario"], row["method"]): row for row in result["rows"]}
        self.assertEqual(rows["productive_branch", "puct_cell"]["probabilities"]["B"], 1)
        self.assertEqual(rows["neglected_branch", "puct_cell"]["probabilities"]["C"], 1)
        self.assertAlmostEqual(rows["fresh", "uniform_cell"]["probabilities"]["A"], 1 / 3)


class SearchStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.add("candidate", "seed", {"candidate": {"parent_id": None}})

    def add(self, kind, identifier, payload):
        self.events.append({"kind": kind, "record_id": identifier,
                            "payload": deepcopy(payload), "sequence": len(self.events) + 1})

    def generation(self, name, parent, child, status, island=0, evaluation=None):
        self.add("generation", name, {
            "plan": {"parent_id": parent, "child_candidate_id": child, "island_id": island},
            "state": {"status": status, "evaluation_attempt_id": evaluation}})

    def evaluation(self, name, child, score=2.7, status="ok", purpose="search"):
        self.add("evaluation", name, {"evaluation_attempt_id": name, "candidate_id": child,
                 "purpose": purpose, "stage": "standard", "state": "completed",
                 "result": asdict(Evaluation(candidate_id=child, status=status, score=score if status == "ok" else None))})

    def stats(self):
        return rebuild_search_statistics(self.events, 2)["islands"]

    def test_request_counts_once_including_unknown_and_parse_failures(self):
        self.generation("g0", "seed", "a", "planned")
        self.assertEqual(self.stats()[0]["expansions"], 0)
        for status in ["requesting", "uncertain", "uncertain"]:
            self.generation("g0", "seed", "a", status)
        for status in ["requesting", "response_received", "parse_error"]:
            self.generation("g1", "seed", "b", status)
        stat = self.stats()[0]
        self.assertEqual(stat["expansions"], 2)
        self.assertEqual(stat["candidates"]["seed"]["visits"], 2)
        self.assertIsNone(stat["candidates"]["seed"]["best_child_score"])

    def test_max_direct_child_only_and_visits_backpropagate_to_ancestors(self):
        self.generation("g0", "seed", "a", "requesting")
        self.add("candidate", "a", {"candidate": {"parent_id": "seed"}})
        self.evaluation("eval_a", "a", 2.5)
        self.generation("g0", "seed", "a", "completed", evaluation="eval_a")
        self.generation("g1", "a", "b", "requesting")
        self.add("candidate", "b", {"candidate": {"parent_id": "a"}})
        self.evaluation("eval_b", "b", 2.8)
        self.generation("g1", "a", "b", "completed", evaluation="eval_b")
        stats = self.stats()[0]
        self.assertEqual(stats["candidates"]["seed"]["visits"], 2)
        self.assertEqual(stats["candidates"]["seed"]["best_child_score"], 2.5)
        self.assertEqual(stats["candidates"]["a"]["best_child_score"], 2.8)
        self.assertEqual(stats["candidates"]["a"]["visits"], 1)

    def test_confirmation_and_later_reevaluation_do_not_rewrite_historical_credit(self):
        self.generation("g0", "seed", "a", "requesting")
        self.evaluation("eval_a", "a", 2.5)
        self.generation("g0", "seed", "a", "completed", evaluation="eval_a")
        self.evaluation("confirm", "a", 9, purpose="confirmation")
        self.evaluation("later", "a", 1)
        self.generation("g0", "seed", "a", "completed", evaluation="later")
        self.assertEqual(self.stats()[0]["candidates"]["seed"]["best_child_score"], 2.5)
        self.assertEqual(self.stats()[0]["expansions"], 1)

    def test_invalid_child_has_no_reward_credit_and_islands_have_separate_counts(self):
        self.generation("g0", "seed", "a", "requesting", island=1)
        self.evaluation("bad", "a", status="invalid")
        self.generation("g0", "seed", "a", "completed", island=1, evaluation="bad")
        self.assertEqual(self.stats()[0]["expansions"], 0)
        self.assertEqual(self.stats()[1]["expansions"], 1)
        self.assertIsNone(self.stats()[1]["candidates"]["seed"]["best_child_score"])

    def test_missing_or_wrong_child_result_is_rejected(self):
        self.generation("g0", "seed", "a", "completed", evaluation="missing")
        with self.assertRaisesRegex(ValueError, "缺少"):
            self.stats()
        self.events.pop()
        self.evaluation("wrong", "b", 2.8)
        self.generation("g0", "seed", "a", "completed", evaluation="wrong")
        with self.assertRaisesRegex(ValueError, "身份"):
            self.stats()


if __name__ == "__main__":
    unittest.main()
