"""档案策略的行为测试：保存多样性、复测撤销、事件边界及环形迁移。"""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import unittest

from Evolve.archive import feature_cell, rebuild_archive, validate_archive_config
from Evolve.contracts import Evaluation
from Evolve.selection import sample_parent


PROJECT = Path(__file__).resolve().parents[1]


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((PROJECT / "configs/circle_packing.json").read_text(encoding="utf-8"))["archive"]
        self.config["islands"] = 3
        self.events = []

    def event(self, kind, identifier, payload):
        self.events.append({"sequence": len(self.events) + 1, "kind": kind,
                            "record_id": identifier, "payload": deepcopy(payload)})

    def candidate(self, identifier, island=None):
        generation = f"gen_{identifier}" if island is not None else None
        if generation:
            self.event("generation", generation, {"plan": {"child_candidate_id": identifier, "island_id": island},
                                                  "state": {"status": "planned"}})
        self.event("candidate", identifier, {"candidate": {"parent_id": "seed" if generation else None},
                                             "generation_id": generation})

    def evaluate(self, identifier, score=2.0, cv=0.0, boundary=0.5, status="ok", purpose="search", evaluation_id=None):
        result = Evaluation(candidate_id=identifier, status=status,
                            score=score if status == "ok" else None,
                            features={"radius_cv": cv, "boundary_fraction": boundary} if status == "ok" else {})
        record = {"candidate_id": identifier, "state": "completed", "purpose": purpose,
                  "stage": "standard", "result": asdict(result)}
        self.event("evaluation", evaluation_id or f"eval_{len(self.events)}", record)

    def finish(self, identifier, island):
        self.event("generation", f"gen_{identifier}", {"plan": {"child_candidate_id": identifier, "island_id": island},
                                                       "state": {"status": "completed"}})

    def snapshot(self):
        return rebuild_archive(self.config, self.events)

    def ids(self, island=0):
        return {member["candidate_id"] for member in self.snapshot()["islands"][island]["cells"]}

    def test_cell_edges_and_rejection(self):
        axes = [{"name": "x", "edges": [0, 0.25, 0.5, 1]}]
        self.assertEqual(feature_cell({"x": 0}, axes), (0,))
        self.assertEqual(feature_cell({"x": 0.25}, axes), (1,))
        self.assertEqual(feature_cell({"x": 1}, axes), (2,))
        for features in ({}, {"x": -0.1}, {"x": 1.1}, {"x": float("nan")}, {"x": True}):
            self.assertIsNone(feature_cell(features, axes))

    def test_config_rejects_ambiguous_rules(self):
        validate_archive_config(self.config)
        for key, value in [("islands", True), ("migration_interval", 0), ("cell_capacity", 2),
                           ("restart", "random"), ("strategy", "unknown"), ("axes", []),
                           ("axes", [{"name": "x", "edges": [0, 0]}])]:
            config = deepcopy(self.config)
            config[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_archive_config(config)

    def test_lower_score_in_new_cell_survives_and_islands_are_independent(self):
        self.candidate("seed")
        self.evaluate("seed", 2.2)
        self.candidate("low", 0)
        self.evaluate("low", 1.9, boundary=0)
        self.assertEqual(self.ids(0), {"seed", "low"})
        self.assertEqual(self.ids(1), {"seed"})

    def test_same_cell_competition_and_ties_are_deterministic(self):
        for identifier, score in [("z", 3), ("b", 2), ("a", 3)]:
            self.candidate(identifier, 0)
            self.evaluate(identifier, score)
        self.assertEqual(self.ids(), {"a"})
        self.assertEqual(len(self.snapshot()["memberships"]), 3)

    def test_latest_completed_evaluation_can_move_or_invalidate_candidate(self):
        self.candidate("a", 0)
        self.evaluate("a", 2.2)
        self.candidate("b", 0)
        self.evaluate("b", 2)
        self.assertEqual(self.ids(), {"a"})
        self.evaluate("a", 1.8, boundary=0)
        self.assertEqual(self.ids(), {"a", "b"})
        self.assertEqual(sorted(member["score"] for member in self.snapshot()["islands"][0]["cells"]), [1.8, 2])
        self.evaluate("a", status="invalid")
        self.assertEqual(self.ids(), {"b"})

    def test_confirmation_and_running_attempt_do_not_replace_search_result(self):
        self.candidate("a", 0)
        self.evaluate("a", 2)
        self.evaluate("a", 4, boundary=0, purpose="confirmation")
        self.event("evaluation", "pending", {"candidate_id": "a", "state": "running", "purpose": "search", "stage": "standard"})
        member = self.snapshot()["islands"][0]["cells"][0]
        self.assertEqual((member["score"], member["cell"]), (2, [0, 2]))

    def test_commit_order_is_used_instead_of_uuid_or_wall_clock(self):
        self.candidate("a", 0)
        self.evaluate("a", 3, evaluation_id="z")
        self.evaluate("a", 1, evaluation_id="a")
        self.assertEqual(self.snapshot()["islands"][0]["cells"][0]["score"], 1)

    def test_out_of_range_features_excluded_with_reason(self):
        self.candidate("a", 0)
        self.evaluate("a", cv=6)
        self.assertEqual(self.ids(), set())
        self.assertEqual(self.snapshot()["excluded"][0]["reason"], "missing_or_out_of_range_features")

    def test_simultaneous_migration_is_single_hop_and_replay_idempotent(self):
        self.config["migration_interval"] = 2
        self.candidate("a", 0)
        self.evaluate("a", 3)
        self.finish("a", 0)
        self.candidate("b", 2)
        self.evaluate("b", 1)
        self.assertEqual(self.ids(1), set())
        self.finish("b", 2)
        self.assertEqual(self.ids(0), {"a"})
        self.assertEqual(self.ids(1), {"a"})
        self.assertEqual(self.ids(2), {"b"})
        migration = self.snapshot()["migrations"][0]
        self.assertEqual([item["accepted"] for item in migration["transfers"]], [True, False])
        self.assertEqual(migration["transfers"][0]["evaluation_attempt_id"], "eval_2")
        before = self.snapshot()
        # 重复提交同一 completed 状态不能再次触发迁移。
        self.finish("b", 2)
        after = self.snapshot()
        self.assertEqual(after["migrations"], before["migrations"])
        self.assertEqual(after["islands"], before["islands"])

    def test_later_reevaluation_cannot_change_historical_migration_decision(self):
        self.config["migration_interval"] = 1
        self.candidate("a", 0)
        self.evaluate("a", 3)
        self.finish("a", 0)
        history = self.snapshot()["migrations"]
        self.evaluate("a", status="timeout")
        self.assertEqual(self.snapshot()["migrations"], history)
        self.assertEqual(self.ids(0) | self.ids(1), set())

    def test_greedy_control_disables_migration(self):
        self.config.update(strategy="greedy", migration_interval=1)
        self.candidate("a", 0)
        self.evaluate("a")
        self.finish("a", 0)
        self.assertEqual(self.snapshot()["migrations"], [])

    def test_uniform_cell_sampling_reproduces_and_can_select_lower_score(self):
        self.candidate("seed")
        self.evaluate("seed", 2.2)
        self.candidate("low", 0)
        self.evaluate("low", 1.5, boundary=0)
        snapshot = self.snapshot()
        draws = [sample_parent(snapshot, seed=42, selection_index=i) for i in range(60)]
        self.assertIn("low", {draw["parent_id"] for draw in draws})
        self.assertEqual([draw["island_id"] for draw in draws[:6]], [0, 1, 2, 0, 1, 2])
        self.assertEqual(draws, [sample_parent(deepcopy(snapshot), seed=42, selection_index=i) for i in range(60)])
        self.assertNotEqual([draw["parent_id"] for draw in draws],
                            [sample_parent(snapshot, seed=43, selection_index=i)["parent_id"] for i in range(60)])


if __name__ == "__main__":
    unittest.main()
