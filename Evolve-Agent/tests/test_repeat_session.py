"""重复复核的记录、最佳布局关联和搜索选择隔离；实际执行手写种子。"""

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from Evolve.contracts import Candidate, Evaluation
from Evolve.experiment import create_experiment, digest
from Evolve.session import ExperimentSession


PROJECT = Path(__file__).resolve().parents[1]


class RepeatSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-repeat-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("Evolve", "Examples", "tasks"):
            shutil.copytree(PROJECT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        config = (PROJECT / "configs/circle_packing.json").read_bytes()
        (self.root / "config.json").write_bytes(config)
        self.directory, _ = create_experiment("config.json", self.root)
        self.session = ExperimentSession(self.directory, self.root)
        self.candidate = self.session.parent()

    def test_real_group_keeps_all_attempts_and_associates_best_layout(self):
        group_id, summary = self.session.evaluate_repeated(self.candidate, repeats=3)
        self.assertEqual(summary["state"], "completed")
        self.assertEqual(summary["valid_rate"], 1)
        self.assertAlmostEqual(summary["all_valid_median"], 26 / 12)
        self.assertEqual(summary["conditional_score_stats"]["sample_stdev"], 0)
        folder = self.directory / "evaluation_groups" / group_id
        source_hash = digest(self.candidate.code.encode("utf-8"))
        self.assertEqual(json.loads((folder / "plan.json").read_text(encoding="utf-8"))["source_sha256"], source_hash)
        identifiers = summary["evaluation_attempt_ids"]
        self.assertEqual(len(set(identifiers)), 3)
        for index, identifier in enumerate(identifiers, 1):
            record = json.loads((self.directory / "evaluations" / identifier / "evaluation.json").read_text(encoding="utf-8"))
            self.assertEqual(record["purpose"], "confirmation")
            self.assertEqual(record["evaluation_group_id"], group_id)
            self.assertEqual(record["repeat_index"], index)
            self.assertEqual(record["result"]["stage"], "standard")
            self.assertIn("radius_cv", record["result"]["features"])
        solution = summary["best_solution"]
        self.assertEqual(digest((folder / solution["file"]).read_bytes()), solution["sha256"])
        original = self.directory / "evaluations" / solution["evaluation_attempt_id"] / "artifacts/solution.json"
        self.assertEqual((folder / solution["file"]).read_bytes(), original.read_bytes())
        self.assertEqual(summary["best_evaluation_attempt_id"], solution["evaluation_attempt_id"])
        # 复核本身不建立搜索档案资格，也不消耗模型生成次数。
        self.assertIsNone(self.session.best())
        self.assertEqual(self.session.generation_count(), 0)

    def test_failed_confirmation_does_not_invalidate_search_parent(self):
        _, searched = self.session.evaluate(self.candidate, "seed")

        def fake_timeout(task, candidate, *, artifact_dir, limits):
            artifact_dir.mkdir()
            (artifact_dir / "execution.json").write_text('{"offline_fixture": true}')
            return Evaluation(candidate.candidate_id, "timeout", feedback="offline fixture")

        with patch("Evolve.session.run_candidate", side_effect=fake_timeout):
            group_id, summary = self.session.evaluate_repeated(self.candidate, repeats=2)
        self.assertEqual(summary["state"], "completed")
        self.assertEqual(summary["valid_rate"], 0)
        self.assertIsNone(summary["all_valid_median"])
        self.assertIsNone(summary["best_solution"])
        self.assertEqual(list((self.directory / "evaluation_groups" / group_id).glob("best_solution_*.json")), [])
        best, result = self.session.best()
        self.assertEqual(best, self.candidate)
        self.assertEqual(result, searched)

    def test_exception_keeps_partial_group_and_does_not_invent_final_score(self):
        original_evaluate = self.session.evaluate
        calls = 0

        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("offline interruption")
            return original_evaluate(*args, **kwargs)

        with patch.object(self.session, "evaluate", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.session.evaluate_repeated(self.candidate, repeats=3)
        path = next(self.directory.glob("evaluation_groups/*/summary.json"))
        summary = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(summary["state"], "failed")
        self.assertEqual(summary["completed_repeats"], 1)
        self.assertIsNone(summary["valid_rate"])
        self.assertIsNone(summary["all_valid_median"])
        self.assertTrue((path.parent / summary["best_solution"]["file"]).exists())
        restored = ExperimentSession(self.directory, self.root)
        self.assertEqual(restored.generation_count(), 0)
        self.assertEqual(len(list(self.directory.glob("evaluations/*/evaluation.json"))), 1)

    def test_keyboard_interrupt_is_recorded_and_propagated(self):
        with patch.object(self.session, "evaluate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.session.evaluate_repeated(self.candidate, repeats=3)
        path = next(self.directory.glob("evaluation_groups/*/summary.json"))
        summary = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(summary["state"], "interrupted")
        self.assertEqual(summary["completed_repeats"], 0)
        self.assertIsNone(summary["best_solution"])
        self.assertIsNone(summary["valid_rate"])

    def test_invalid_count_or_unsaved_candidate_creates_no_group(self):
        for count in (0, -1, True):
            with self.assertRaises(ValueError):
                self.session.evaluate_repeated(self.candidate, repeats=count)
        unknown = Candidate(self.candidate.task_id, self.candidate.code)
        with self.assertRaisesRegex(ValueError, "保存匹配"):
            self.session.evaluate_repeated(unknown, repeats=1)
        self.assertEqual(list(self.directory.glob("evaluation_groups/*")), [])

    def test_returned_stage_must_match_standard_plan(self):
        def fake_stage(task, candidate, *, artifact_dir, limits):
            return Evaluation(candidate.candidate_id, "ok", score=999, stage="low_budget")

        with patch("Evolve.session.run_candidate", side_effect=fake_stage):
            with self.assertRaisesRegex(ValueError, "阶段"):
                self.session.evaluate(self.candidate, "seed")
        path = next(self.directory.glob("evaluations/*/evaluation.json"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "failed")
        self.assertIsNone(self.session.best())


if __name__ == "__main__":
    unittest.main()
