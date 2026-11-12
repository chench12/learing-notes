"""验证整理后仍可恢复实验，摘要不会混淆搜索/复核和未知用量。"""

from dataclasses import asdict
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from Evolve.calls import fail_call, prepare_call
from Evolve.contracts import Evaluation
from Evolve.experiment import create_experiment
from Evolve.session import ExperimentSession
from Examples.experiment_output import create_development_experiment, write_overview
from Examples import run_search as search_cli


PROJECT = Path(__file__).resolve().parents[1]


class ExperimentOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-overview-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        for name in ("Evolve", "Examples", "tasks"):
            shutil.copytree(PROJECT / name, self.project / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        config = json.loads((PROJECT / "configs/circle_packing.offline.json").read_text(encoding="utf-8"))
        (self.project / "config.json").write_text(json.dumps(config), encoding="utf-8")
        guard = patch("socket.socket.connect", side_effect=AssertionError("unexpected network"))
        guard.start()
        self.addCleanup(guard.stop)

    def session(self):
        directory, _ = create_experiment("config.json", self.project)
        return ExperimentSession(directory, self.project)

    def test_development_move_preserves_identity_and_reopens(self):
        directory, manifest = create_development_experiment("config.json", self.project)
        self.assertEqual(directory.parent, self.project / "dev_runs")
        self.assertFalse((self.project / "experiments" / directory.name).exists())
        session = ExperimentSession(directory, self.project)
        self.assertEqual(session.experiment_id, manifest["experiment_id"])
        self.assertEqual(session.manifest["fingerprint"], manifest["fingerprint"])
        write_overview(directory)
        self.assertIn(f"dev_runs/{directory.name}", (directory / "README.md").read_text(encoding="utf-8"))

    def test_matching_exports_idempotence_and_confirmation_separation(self):
        session = self.session()
        candidate = session.parent()
        evaluation_id, result = session.evaluate(candidate, "seed")
        # 复核中的较大数值不能代替搜索结果；这里不执行额外求解。
        record = session.begin_evaluation(candidate, "confirmation", purpose="confirmation")
        altered = asdict(result)
        altered["score"] = 99.0
        session.finish_evaluation(record, Evaluation(**altered), 0.0)
        events_before = session.storage.events()
        summary = write_overview(session.directory)
        self.assertEqual(summary["best"]["evaluation_id"], evaluation_id)
        self.assertEqual(summary["best"]["score"], result.score)
        source = session.directory / "candidates" / candidate.candidate_id / "candidate.py"
        solution = session.directory / "evaluations" / evaluation_id / "artifacts/solution.json"
        self.assertEqual((session.directory / "best.py").read_bytes(), source.read_bytes())
        self.assertEqual((session.directory / "best_solution.json").read_bytes(), solution.read_bytes())
        self.assertEqual(write_overview(session.directory), summary)
        self.assertEqual(session.storage.events(), events_before)
        reopened = ExperimentSession(session.directory, self.project)
        self.assertEqual(reopened.best()[1].score, result.score)

    def test_latest_failed_search_removes_stale_best(self):
        session = self.session()
        candidate = session.parent()
        session.evaluate(candidate, "seed")
        self.assertIsNotNone(write_overview(session.directory)["best"])
        record = session.begin_evaluation(candidate, "parent")
        failed = Evaluation(candidate.candidate_id, "invalid", None, {}, "test invalid result")
        session.finish_evaluation(record, failed, 0.0)
        self.assertIsNone(write_overview(session.directory)["best"])
        self.assertFalse((session.directory / "best.py").exists())
        self.assertFalse((session.directory / "best_solution.json").exists())

    def test_uncertain_call_retains_reservation_and_empty_experiment_has_no_best(self):
        session = self.session()
        call = prepare_call(session, session.config["model"], [{"role": "user", "content": "test"}],
                            purpose="program", owner_id="test-owner")
        fail_call(session, call, ConnectionError(), 1.0)
        summary = write_overview(session.directory)
        self.assertIsNone(summary["best"])
        self.assertEqual(summary["usage"]["known_tokens"], 0)
        self.assertEqual(summary["usage"]["unknown_usage_call_ids"], [call["call_id"]])
        self.assertGreater(summary["usage"]["reserved_tokens_for_unknown_calls"], 0)
        self.assertFalse((session.directory / "best_solution.json").exists())

    def test_offline_cli_writes_to_development_and_refreshes_summary(self):
        with (patch.object(search_cli, "__file__", str(self.project / "Examples/run_search.py")),
              patch("sys.argv", ["run_search", "--config", "config.json", "--offline-demo", "--steps", "1"]),
              redirect_stdout(StringIO())):
            search_cli.main()
        directories = list((self.project / "dev_runs").iterdir())
        self.assertEqual(len(directories), 1)
        summary = json.loads((directories[0] / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["counts"]["generation"], 1)
        self.assertEqual(summary["usage"]["known_tokens"], 0)
        self.assertIsNotNone(summary["best"])
        self.assertEqual(list((self.project / "experiments").iterdir()), [])

    def test_cli_preserves_original_error_and_still_exports(self):
        session = self.session()
        with (patch.object(search_cli, "__file__", str(self.project / "Examples/run_search.py")),
              patch("sys.argv", ["run_search", "--experiment", str(session.directory)]),
              patch.object(search_cli, "run_search", side_effect=RuntimeError("original failure")),
              redirect_stdout(StringIO()),
              self.assertRaisesRegex(RuntimeError, "original failure")):
            search_cli.main()
        self.assertTrue((session.directory / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()
