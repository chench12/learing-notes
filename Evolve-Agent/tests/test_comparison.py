"""三策略匹配配置及持久化结果报告的离线验收。"""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from Evolve.comparison import METHODS, compare_sessions, prepare_configs, summarize_session, write_comparison
from Evolve.experiment import create_experiment
from Evolve.generation import generate_once, process_saved_response
from Evolve.recovery import recover_experiment
from Evolve.selection import rebuild_search_statistics
from Evolve.session import ExperimentSession
from Examples.check_archive import FixtureModel


PROJECT = Path(__file__).resolve().parents[1]
PATCH_TEXT = "<<<<<<< SEARCH\n        radius = 1 / 12\n=======\n        radius = 1 / 13\n>>>>>>> REPLACE\n"


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-compare-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("Evolve", "Examples", "tasks"):
            shutil.copytree(PROJECT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        self.config = json.loads((PROJECT / "configs/circle_packing.json").read_text(encoding="utf-8"))
        output = redirect_stdout(StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        guard = patch("socket.socket.connect", side_effect=AssertionError("unexpected model call"))
        guard.start()
        self.addCleanup(guard.stop)

    def session(self, method, seed=42, generations=1):
        config = deepcopy(self.config)
        config["archive"]["parent_sampling"] = method
        config["search"].update(seed=seed, max_generations=generations)
        path = self.root / f"{method}_{seed}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        directory, _ = create_experiment(path, self.root)
        return ExperimentSession(directory, self.root)

    def test_prepare_only_changes_sampling_seed_budget_and_never_starts_experiments(self):
        output = self.root / "prepared"
        plan = prepare_configs(self.config, output, seeds=[42, 43], generations=100)
        self.assertEqual(len(plan["runs"]), 6)
        self.assertEqual(plan["total_planned_generations"], 600)
        self.assertFalse((self.root / "experiments").exists())
        for name in plan["runs"]:
            config = json.loads((output / name).read_text(encoding="utf-8"))
            config["archive"]["parent_sampling"] = self.config["archive"]["parent_sampling"]
            config["search"] = self.config["search"]
            self.assertEqual(config, self.config)
        with self.assertRaises(FileExistsError):
            prepare_configs(self.config, output, seeds=[42], generations=100)

    def test_all_methods_save_diagnostics_and_recover_without_new_requests_or_visits(self):
        sessions = []
        for method in METHODS:
            session = self.session(method)
            with patch("Evolve.generation.process_saved_response", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    generate_once(session, FixtureModel(PATCH_TEXT))
            identifier, record = session.storage.all("generation")[0]
            self.assertEqual(record["plan"]["selection"]["diagnostics"]["rule"], method)
            frozen = deepcopy(record["plan"]["selection"])
            session = ExperimentSession(session.directory, self.root)
            with patch.object(session, "select_parent", side_effect=AssertionError("must not resample")):
                process_saved_response(session, identifier)
            self.assertEqual(session.storage.get("generation", identifier)["plan"]["selection"], frozen)
            stats = rebuild_search_statistics(session.storage.events(), session.config["archive"]["islands"])
            island = stats["islands"][0]
            self.assertEqual(island["expansions"], 1)
            parent = island["candidates"][record["plan"]["parent_id"]]
            self.assertEqual(parent["visits"], 1)
            self.assertEqual(parent["best_child_score"], 2.0)
            counts = session.storage.stats()
            recover_experiment(session)
            process_saved_response(session, identifier)
            self.assertEqual(session.storage.stats(), counts)
            self.assertEqual(rebuild_search_statistics(session.storage.events(), 4), stats)
            sessions.append(session)
        report = compare_sessions(sessions)
        self.assertTrue(report["all_budgets_finished"])
        self.assertEqual(report["paired_seeds"], [42])
        for row in report["runs"]:
            self.assertEqual(row["requested_generations"], 1)
            self.assertEqual(row["known_tokens"], 0)
            self.assertTrue(row["token_accounting_complete"])
            self.assertAlmostEqual(row["best_latest_score"], 26 / 12)
            self.assertEqual(row["child_valid_rate"], 1)
        counts = [session.storage.stats() for session in sessions]
        write_comparison(report, self.root / "results")
        self.assertTrue((self.root / "results/trace.csv").is_file())
        self.assertEqual([session.storage.stats() for session in sessions], counts)

    def test_puct_next_selection_contains_rebuilt_parent_credit(self):
        # 仅一岛保证下一次选择可读到刚才父代的反馈。
        config = deepcopy(self.config)
        config["archive"].update(islands=1, parent_sampling="puct_cell")
        config["search"]["max_generations"] = 2
        path = self.root / "one.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        directory, _ = create_experiment(path, self.root)
        session = ExperimentSession(directory, self.root)
        child, _ = generate_once(session, FixtureModel(PATCH_TEXT))
        _, selection = session.select_parent()
        detail = {item["candidate_id"]: item for item in selection["diagnostics"]["candidates"]}
        self.assertEqual(detail[child.parent_id]["q"], 2.0)
        self.assertEqual(detail[child.parent_id]["visits"], 1)
        self.assertEqual(detail[child.candidate_id]["visits"], 0)
        self.assertIsNotNone(detail[child.parent_id]["best_child_evaluation_id"])

    def test_unknown_request_counts_and_usage_stays_explicitly_incomplete(self):
        session = self.session("puct_cell")
        with patch.object(FixtureModel, "generate", side_effect=TimeoutError):
            with self.assertRaises(RuntimeError):
                generate_once(session, FixtureModel(PATCH_TEXT))
        summary = summarize_session(session)
        self.assertEqual(summary["requested_generations"], 1)
        self.assertEqual(summary["saved_responses"], 0)
        self.assertFalse(summary["token_accounting_complete"])
        self.assertFalse(summary["budget_finished"])
        self.assertEqual(summary["generation_states"], {"uncertain": 1})
        stat = rebuild_search_statistics(session.storage.events(), 4)["islands"][0]
        self.assertEqual(stat["expansions"], 1)

    def test_unmatched_missing_or_duplicate_runs_refused(self):
        sessions = [self.session(method) for method in METHODS]
        report = compare_sessions(sessions)
        self.assertFalse(report["all_budgets_finished"])
        with self.assertRaisesRegex(ValueError, "种子集合"):
            compare_sessions(sessions[:2])
        with self.assertRaisesRegex(ValueError, "多个实验"):
            compare_sessions([*sessions, sessions[0]])
        sessions[0].manifest["config"]["archive"]["migration_interval"] += 1
        with self.assertRaisesRegex(ValueError, "不匹配"):
            compare_sessions(sessions)


if __name__ == "__main__":
    unittest.main()
