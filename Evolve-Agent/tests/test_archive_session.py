"""档案与真实持久化/生成恢复边界；全部离线。"""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from Evolve.contracts import Candidate, Evaluation
from Evolve.experiment import create_experiment
from Evolve.generation import generate_once, generation_state, process_saved_response
from Evolve.recovery import recover_experiment
from Evolve.session import ExperimentSession
from Examples.check_archive import FixtureModel


PROJECT = Path(__file__).resolve().parents[1]
PATCH_TEXT = "<<<<<<< SEARCH\n        radius = 1 / 12\n=======\n        radius = 1 / 13\n>>>>>>> REPLACE\n"


class ArchiveSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-archive-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("Evolve", "Examples", "tasks"):
            shutil.copytree(PROJECT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        self.config = json.loads((PROJECT / "configs/circle_packing.json").read_text(encoding="utf-8"))
        self.config["archive"]["migration_interval"] = 1
        self.create()
        output = redirect_stdout(StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        guard = patch("socket.socket.connect", side_effect=AssertionError("unexpected API call"))
        guard.start()
        self.addCleanup(guard.stop)

    def create(self):
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        self.directory, _ = create_experiment("config.json", self.root)
        self.session = ExperimentSession(self.directory, self.root)

    def fake_result(self, task, candidate, *, artifact_dir, **kwargs):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return Evaluation(candidate_id=candidate.candidate_id, status="ok", score=2.0,
                          features={"radius_cv": 0.0, "boundary_fraction": 0.0})

    def runner_returning(self, result):
        def run(task, candidate, *, artifact_dir, **kwargs):
            artifact_dir.mkdir(parents=True, exist_ok=True)
            return result
        return run

    def generate(self):
        return generate_once(self.session, FixtureModel(PATCH_TEXT))

    def test_plan_freezes_selection_before_parent_reevaluation_and_records_child_island(self):
        child, _ = self.generate()
        identifier, record = self.session.storage.all("generation")[0]
        plan = record["plan"]
        self.assertEqual(plan["selection_strategy"], "island-grid-latest-v1")
        self.assertEqual(plan["selection"]["fallback"], "seed")
        self.assertEqual(plan["selection"]["parent_id"], child.parent_id)
        self.assertIsNone(plan["candidate_pool"][0]["latest_search_evaluation_id"])
        self.assertEqual(plan["selection"]["archive_snapshot"]["excluded"][0]["reason"], "not_evaluated")
        self.assertEqual(plan["island_id"], 0)
        self.assertEqual(self.session.archive_snapshot()["memberships"][child.candidate_id], [0])
        self.assertEqual(self.session.storage.get("candidate", child.candidate_id)["generation_id"], identifier)

    def test_committed_evaluation_rebuilds_after_archive_export_interruption(self):
        seed = self.session.parent()
        with patch("Evolve.session.run_candidate", side_effect=self.fake_result), patch.object(
            self.session, "sync_archive", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.session.evaluate(seed, "parent")
        self.assertEqual(self.session.storage.all("evaluation")[0][1]["state"], "completed")
        self.session = ExperimentSession(self.directory, self.root)
        expected = self.session.archive_snapshot()
        self.assertEqual(expected["islands"][0]["cells"][0]["candidate_id"], seed.candidate_id)
        with patch("Evolve.session.run_candidate", side_effect=AssertionError("must not execute")):
            recover_experiment(self.session)
        self.assertEqual(self.session.storage.get("archive", "current")["snapshot"], expected)
        counts = self.session.storage.stats()
        recover_experiment(self.session)
        self.assertEqual(self.session.storage.stats(), counts)

    def test_completion_interrupt_recovery_materializes_exactly_one_migration(self):
        original = generation_state

        def stop_before_completion(session, record, status, **details):
            if status == "completed":
                raise KeyboardInterrupt
            return original(session, record, status, **details)

        with patch("Evolve.generation.generation_state", side_effect=stop_before_completion):
            with self.assertRaises(KeyboardInterrupt):
                self.generate()
        self.assertEqual(self.session.archive_snapshot()["migrations"], [])
        self.session = ExperimentSession(self.directory, self.root)
        identifier = self.session.storage.all("generation")[0][0]
        with patch("Evolve.session.run_candidate", side_effect=AssertionError("must not execute")):
            recover_experiment(self.session)
            self.assertEqual(len(self.session.archive_snapshot()["migrations"]), 1)
            counts = self.session.storage.stats()
            process_saved_response(self.session, identifier)
            recover_experiment(self.session)
        self.assertEqual(self.session.storage.stats(), counts)

    def test_completed_generation_before_cache_commit_is_replayed_without_new_facts(self):
        original = self.session.sync_archive

        def interrupt_after_completion():
            rows = self.session.storage.all("generation")
            if any(record["state"]["status"] == "completed" for _, record in rows):
                raise KeyboardInterrupt
            return original()

        with patch.object(self.session, "sync_archive", side_effect=interrupt_after_completion):
            with self.assertRaises(KeyboardInterrupt):
                self.generate()
        self.session = ExperimentSession(self.directory, self.root)
        expected = self.session.archive_snapshot()
        self.assertEqual(len(expected["migrations"]), 1)
        identifier = self.session.storage.all("generation")[0][0]
        with patch("Evolve.session.run_candidate", side_effect=AssertionError("must not execute")):
            process_saved_response(self.session, identifier)
        self.assertEqual(self.session.storage.get("archive", "current")["snapshot"], expected)

    def test_response_recovery_uses_frozen_parent_and_island(self):
        with patch("Evolve.generation.process_saved_response", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.generate()
        identifier, record = self.session.storage.all("generation")[0]
        # 后来加入一个新的高分根程序，不应改变已经提交的生成计划。
        original_parent = self.session.candidates()[record["plan"]["parent_id"]]
        other = Candidate(task_id=original_parent.task_id, code=original_parent.code)
        self.session.save_candidate(other)
        with patch("Evolve.session.run_candidate", side_effect=self.runner_returning(Evaluation(
            candidate_id=other.candidate_id, status="ok", score=3.0,
            features={"radius_cv": 0.0, "boundary_fraction": 0.5},
        ))):
            self.session.evaluate(other, "parent")
        with patch.object(self.session, "select_parent", side_effect=AssertionError("must not resample")):
            child, _ = process_saved_response(self.session, identifier)
        self.assertEqual(child.parent_id, original_parent.candidate_id)
        self.assertEqual(self.session.archive_snapshot()["memberships"][child.candidate_id], [0])

    def test_read_queries_do_not_add_events_or_advance_sampling(self):
        self.generate()
        counts = self.session.storage.stats()
        parent1, selection1 = self.session.select_parent()
        parent2, selection2 = self.session.select_parent()
        self.assertEqual((parent1, selection1), (parent2, selection2))
        self.session.archive_snapshot()
        self.session.best()
        self.assertEqual(self.session.storage.stats(), counts)

    def test_confirmation_is_excluded_and_global_best_is_independent_of_sampled_island(self):
        child, _ = self.generate()
        seed = self.session.candidates()[child.parent_id]
        self.assertEqual(self.session.best()[0], seed)
        # 下一次选择岛1，低分子代只在岛0，父代应为岛1中的种子。
        parent, selection = self.session.select_parent()
        self.assertEqual(parent, seed)
        self.assertEqual(selection["island_id"], 1)
        self.assertNotIn(child.candidate_id, {item["candidate_id"] for item in selection["archive_snapshot"]["islands"][1]["cells"]})
        with patch("Evolve.session.run_candidate", side_effect=self.runner_returning(Evaluation(
            candidate_id=child.candidate_id, status="invalid"
        ))):
            self.session.evaluate(child, "confirmation", purpose="confirmation")
        self.assertIn(child.candidate_id, {item["candidate_id"] for item in self.session.archive_snapshot()["islands"][0]["cells"]})

    def test_explicit_greedy_control_uses_global_best(self):
        self.config["archive"]["strategy"] = "greedy"
        self.create()
        child, _ = self.generate()
        seed = self.session.candidates()[child.parent_id]
        parent, selection = self.session.select_parent()
        self.assertEqual(parent, seed)
        self.assertEqual(selection["selection_strategy"], "latest-standard-greedy-v1")
        self.assertEqual(self.session.archive_snapshot()["migrations"], [])


if __name__ == "__main__":
    unittest.main()
