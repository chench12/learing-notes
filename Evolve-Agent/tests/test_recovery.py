"""离线响应及真实小型评测的中断恢复；断言不重发、不重复入库。"""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from Evolve.evaluation import REPEAT_POLICY, summarize_repeats
from Evolve.experiment import create_experiment, new_id
from Evolve.generation import generate_once, generation_state, process_saved_response
from Evolve.recovery import recover_experiment, recovery_report
from Evolve.runner import run_candidate
from Evolve.session import ExperimentSession, now
from Evolve.storage import encode_json


PROJECT = Path(__file__).resolve().parents[1]
PATCH_TEXT = "<<<<<<< SEARCH\n        radius = 1 / 12\n=======\n        radius = 1 / 13\n>>>>>>> REPLACE\n"


class OfflineModel:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def generate(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return {"text": PATCH_TEXT, "finish_reason": "stop", "usage": {"total_tokens": 0}}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("Evolve", "Examples", "tasks"):
            shutil.copytree(PROJECT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "config.json").write_bytes((PROJECT / "configs/circle_packing.json").read_bytes())
        self.directory, _ = create_experiment("config.json", self.root)
        self.session = ExperimentSession(self.directory, self.root)
        self.model = OfflineModel()
        output = redirect_stdout(StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        guard = patch("socket.socket.connect", side_effect=AssertionError("unexpected API call"))
        guard.start()
        self.addCleanup(guard.stop)

    def pause_after_response(self):
        with patch("Evolve.generation.process_saved_response", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                generate_once(self.session, self.model)
        return self.session.storage.all("generation")[0][0]

    def reopen(self):
        self.session = ExperimentSession(self.directory, self.root)

    def test_saved_response_resumes_without_model_or_duplicate_candidate_or_evaluation(self):
        identifier = self.pause_after_response()
        self.reopen()
        self.assertEqual(recovery_report(self.session)["generations"][0]["action"], "resume_saved_response")
        plan = self.session.storage.get("generation", identifier)["plan"]
        self.assertEqual(plan["inspiration_ids"], [])
        self.assertEqual(len(plan["candidate_pool"]), 1)
        candidate, result = process_saved_response(self.session, identifier)
        self.assertEqual(candidate.candidate_id, plan["child_candidate_id"])
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.score, 2.0)
        counts = self.session.storage.stats()
        with patch("Evolve.session.run_candidate", side_effect=AssertionError("must not rerun")):
            again = process_saved_response(self.session, identifier)
        self.assertEqual(again, (candidate, result))
        self.assertEqual(self.session.storage.stats(), counts)
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.session.generation_count(), 1)
        self.assertEqual(counts["candidate"], 2)
        self.assertEqual(counts["evaluation"], 2)

    def test_child_saved_before_interruption_is_reused(self):
        original = self.session.save_candidate

        def interrupted(candidate, generation_id=None):
            original(candidate, generation_id)
            if generation_id:
                raise KeyboardInterrupt

        with patch.object(self.session, "save_candidate", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                generate_once(self.session, self.model)
        self.assertEqual(len(self.session.candidates()), 2)
        identifier = self.session.storage.all("generation")[0][0]
        self.reopen()
        process_saved_response(self.session, identifier)
        self.assertEqual(len(self.session.candidates()), 2)
        self.assertEqual(self.model.calls, 1)

    def test_completed_evaluation_reconciles_generation_without_reexecution(self):
        def interrupted(session, record, status, **details):
            if status == "completed":
                raise KeyboardInterrupt
            return generation_state(session, record, status, **details)

        with patch("Evolve.generation.generation_state", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                generate_once(self.session, self.model)
        self.reopen()
        with patch("Evolve.session.run_candidate", side_effect=AssertionError("must not rerun")):
            report = recover_experiment(self.session)
            self.assertEqual(report["generations"][0]["state"], "completed")
            process_saved_response(self.session, report["generations"][0]["generation_id"])
        counts = self.session.storage.stats()
        recover_experiment(self.session)
        self.assertEqual(self.session.storage.stats(), counts)
        self.assertEqual(self.model.calls, 1)

    def test_unknown_provider_outcome_never_resends_or_stores_error_body(self):
        model = OfflineModel(TimeoutError("private-error-body"))
        with self.assertRaises(RuntimeError):
            generate_once(self.session, model)
        identifier, record = self.session.storage.all("generation")[0]
        self.assertEqual(record["state"]["status"], "uncertain")
        self.assertNotIn("private-error-body", json.dumps(record))
        recover_experiment(self.session)
        with self.assertRaisesRegex(ValueError, "不会重发"):
            process_saved_response(self.session, identifier)
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.session.generation_count(), 1)

    def test_explicit_http_rejection_is_api_error(self):
        class Rejected(Exception):
            status_code = 401
        with self.assertRaises(RuntimeError):
            generate_once(self.session, OfflineModel(Rejected("do not store")))
        self.assertEqual(self.session.storage.all("generation")[0][1]["state"]["status"], "api_error")

    def test_stale_request_is_uncertain_while_planned_is_not_sent(self):
        for status in ("planned", "requesting"):
            identifier = new_id("gen")
            record = {"plan": {"generation_attempt_id": identifier, "child_candidate_id": uuid4().hex},
                      "state": {"status": status, "generation_attempt_id": identifier}}
            self.session.save_generation(record)
        report = recover_experiment(self.session)
        self.assertEqual({item["state"] for item in report["generations"]}, {"planned", "uncertain"})
        counts = self.session.storage.stats()
        recover_experiment(self.session)
        self.assertEqual(self.session.storage.stats(), counts)

    def running_evaluation(self, *, group_id=None):
        candidate = self.session.parent()
        identifier = new_id("eval")
        record = {
            "experiment_id": self.session.experiment_id, "evaluation_attempt_id": identifier,
            "candidate_id": candidate.candidate_id, "role": "confirmation" if group_id else "seed",
            "purpose": "confirmation" if group_id else "search", "stage": "standard",
            "evaluation_group_id": group_id, "repeat_index": 1 if group_id else None,
            "state": "running", "started_at_utc": now(),
        }
        self.session.save_evaluation(record)
        return identifier, candidate

    def test_runner_result_saved_before_db_completion_is_recovered(self):
        identifier, candidate = self.running_evaluation()
        folder = self.directory / "evaluations" / identifier
        result = run_candidate(self.session.task, candidate, artifact_dir=folder / "artifacts")
        self.assertEqual(result.status, "ok")
        self.reopen()
        recover_experiment(self.session)
        record = self.session.storage.get("evaluation", identifier)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["recovery_reason"], "runner_result_recovered")
        self.assertEqual(record["result"]["score"], result.score)
        self.session.storage.verify_files()

    def test_no_result_becomes_interrupted_and_requires_explicit_new_evaluation(self):
        identifier = self.pause_after_response()
        record = self.session.storage.get("generation", identifier)
        plan = record["plan"]
        from Evolve.contracts import Candidate
        from Evolve.patches import apply_patches, parse_patches
        parent = self.session.candidates()[plan["parent_id"]]
        child = Candidate(parent.task_id, apply_patches(parent.code, parse_patches(PATCH_TEXT)),
                          parent_id=parent.candidate_id, candidate_id=plan["child_candidate_id"])
        self.session.save_candidate(child, identifier)
        evaluation_id = plan["child_evaluation_attempt_id"]
        evaluation = {
            "experiment_id": self.session.experiment_id, "evaluation_attempt_id": evaluation_id,
            "candidate_id": child.candidate_id, "role": "child", "purpose": "search", "stage": "standard",
            "evaluation_group_id": None, "repeat_index": None, "state": "running", "started_at_utc": now(),
        }
        self.session.save_evaluation(evaluation)
        generation_state(self.session, record, "evaluating", candidate_id=child.candidate_id, evaluation_attempt_id=evaluation_id)
        recover_experiment(self.session)
        self.assertEqual(self.session.storage.get("evaluation", evaluation_id)["state"], "interrupted")
        with self.assertRaisesRegex(ValueError, "显式"):
            process_saved_response(self.session, identifier)
        _, result = process_saved_response(self.session, identifier, reevaluate_interrupted=True)
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.session.storage.get("evaluation", evaluation_id)["state"], "interrupted")
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(len(self.session.storage.all("evaluation")), 3)

    def test_runner_journal_from_different_budget_is_not_adopted(self):
        identifier, candidate = self.running_evaluation()
        folder = self.directory / "evaluations" / identifier
        run_candidate(self.session.task, candidate, artifact_dir=folder / "artifacts")
        path = folder / "artifacts/execution.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["candidate_timeout_seconds"] = 999
        path.write_text(json.dumps(data), encoding="utf-8")
        recover_experiment(self.session)
        record = self.session.storage.get("evaluation", identifier)
        self.assertEqual(record["state"], "interrupted")
        self.assertNotIn("result", record)

    def test_running_group_is_rebuilt_from_completed_attempts_without_rerun(self):
        candidate = self.session.parent()
        group_id = new_id("group")
        plan = {"candidate_id": candidate.candidate_id, "planned_repeats": 3, "aggregation_policy": REPEAT_POLICY}
        summary = summarize_repeats(candidate.candidate_id, [], 3)
        record = {"plan": plan, "summary": summary, "state": "running"}
        self.session.storage.put("group", group_id, record, {
            f"evaluation_groups/{group_id}/plan.json": encode_json(plan),
            f"evaluation_groups/{group_id}/summary.json": encode_json(summary),
        })
        self.session.evaluate(candidate, "confirmation", purpose="confirmation", group_id=group_id, repeat_index=1)
        with patch("Evolve.session.run_candidate", side_effect=AssertionError("must not rerun")):
            recover_experiment(self.session)
        record = self.session.storage.get("group", group_id)
        self.assertEqual(record["state"], "interrupted")
        self.assertEqual(record["summary"]["completed_repeats"], 1)
        self.assertIsNone(record["summary"]["valid_rate"])
        self.assertIsNotNone(record["summary"]["best_solution"])

    def test_orphan_directory_is_reported_without_becoming_candidate(self):
        (self.directory / "candidates/orphan").mkdir()
        report = recovery_report(self.session)
        self.assertIn("candidates/orphan", report["orphan_directories"])
        self.assertEqual(self.session.candidates(), {})


if __name__ == "__main__":
    unittest.main()
