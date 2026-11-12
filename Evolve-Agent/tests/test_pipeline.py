"""上下文、全调用预算、路由、元指令、复核与多轮恢复的离线验收。"""

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

from Evolve.batch import run_batch
from Evolve.budget import BudgetExceeded, check_budget, token_reservation, usage_snapshot
from Evolve.calls import request_model
from Evolve.context import build_context, message_bytes
from Evolve.contracts import Candidate
from Evolve.controller import run_search
from Evolve.experiment import create_experiment
from Evolve.generation import generate_once, process_saved_response
from Evolve.meta_prompts import available_templates, maybe_evolve_template, template_credit
from Evolve.recovery import recover_experiment
from Evolve.routing import select_model
from Evolve.session import ExperimentSession
from Evolve.settings import expanded_config
from Evolve.storage import encode_json
from Examples.run_search import ReviewedFixtureModel


PROJECT = Path(__file__).resolve().parents[1]
REAL_CONNECT = socket.socket.connect


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-pipeline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("Evolve", "Examples", "tasks"):
            shutil.copytree(PROJECT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        self.config = expanded_config(json.loads((PROJECT / "configs/circle_packing.json").read_text(encoding="utf-8")))
        self.config["search"]["max_generations"] = 6
        self.config["archive"].update(islands=1, parent_sampling="puct_cell")
        output = redirect_stdout(StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        guard = patch("socket.socket.connect", side_effect=AssertionError("unexpected network"))
        guard.start()
        self.addCleanup(guard.stop)

    def create(self):
        path = self.root / "config.json"
        path.write_bytes(encode_json(self.config))
        directory, _ = create_experiment(path, self.root)
        self.session = ExperimentSession(directory, self.root)
        self.model = ReviewedFixtureModel(self.session.task.initial_program.read_text(encoding="utf-8"))
        return self.session

    def run_offline(self, **kwargs):
        return run_search(self.session, self.model, reviewed_fixtures=True, **kwargs)

    def test_context_uses_complete_deduplicated_history_and_freezes_selection(self):
        self.create()
        first, _ = generate_once(self.session, self.model)
        second, _ = generate_once(self.session, self.model)
        plan = sorted((record["plan"] for _, record in self.session.storage.all("generation")), key=lambda item: item["created_at_utc"])[-1]
        self.assertIn(first.candidate_id, plan["inspiration_ids"])
        first_record = self.session.storage.get("candidate", first.candidate_id)
        self.assertEqual(plan["context"]["included"][0]["source_sha256"], first_record["source_sha256"])
        self.assertIn(first.code, plan["messages"][-1]["content"])
        self.assertEqual(plan["context"]["input_bytes"], message_bytes(plan["messages"]))
        self.assertIn("不是父代", plan["messages"][0]["content"])
        selection_before = deepcopy(plan["selection"])
        counts = self.session.storage.stats()
        process_saved_response(self.session, plan["generation_attempt_id"])
        self.assertEqual(self.session.storage.stats(), counts)
        self.assertEqual(self.session.storage.get("generation", plan["generation_attempt_id"])["plan"]["selection"], selection_before)

    def test_context_refuses_to_truncate_mandatory_parent_or_protocol(self):
        self.config["context"]["max_bytes"] = 100
        self.create()
        with self.assertRaisesRegex(ValueError, "拒绝截断"):
            generate_once(self.session, self.model)
        self.assertEqual(self.session.storage.all("call"), [])
        self.assertEqual(self.session.generation_count(), 0)

    def test_references_are_snapshotted_and_truncation_is_recorded(self):
        reference = self.root / "tasks/circle_packing/references.md"
        reference.write_text("固定材料" * 100, encoding="utf-8")
        self.config["context"]["reference_max_bytes"] = 30
        self.create()
        reference.write_text("later changed", encoding="utf-8")
        generate_once(self.session, self.model)
        plan = self.session.storage.all("generation")[0][1]["plan"]
        self.assertTrue(plan["context"]["reference"]["included"])
        self.assertTrue(plan["context"]["reference"]["truncated"])
        self.assertIn("固定材料", plan["messages"][-1]["content"])
        self.assertNotIn("later changed", plan["messages"][-1]["content"])

    def test_call_budget_stops_before_second_request_and_survives_resume(self):
        self.config["budget"]["max_calls"] = 1
        self.create()
        result = self.run_offline()
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual(self.session.generation_count(), 1)
        self.assertEqual(len(self.session.storage.all("call")), 1)
        self.session = ExperimentSession(self.session.directory, self.root)
        again = self.run_offline()
        self.assertEqual(again["new_generations"], 0)
        self.assertEqual(len(self.session.storage.all("call")), 1)

    def test_token_reservation_blocks_before_call(self):
        self.config["budget"]["max_tokens"] = 1
        self.create()
        result = self.run_offline()
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual(self.session.storage.all("call"), [])
        self.assertEqual(self.session.generation_count(), 0)

    def test_evaluation_budget_blocks_execution(self):
        self.config["budget"]["max_evaluation_seconds"] = 0.001
        self.create()
        with patch("Evolve.session.run_candidate", side_effect=AssertionError("must not execute")):
            result = self.run_offline()
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual(self.session.storage.all("evaluation"), [])

    def test_usage_missing_charges_reservation_and_unknown_calls_never_resend(self):
        self.create()
        model = type("MissingUsage", (), {"generate": lambda _, **kwargs: {"text": "x", "finish_reason": "stop"}})()
        messages = [{"role": "user", "content": "测试"}]
        identifier, _ = request_model(self.session, model, self.config["model"], messages, purpose="test", owner_id="owner")
        used = usage_snapshot(self.session)
        self.assertEqual(used["charged_tokens"], token_reservation(messages, self.config["model"]["max_tokens"])["total_tokens"])
        self.assertIn(identifier, used["unknown_usage_call_ids"])
        with patch.object(model, "generate", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                request_model(self.session, model, self.config["model"], messages, purpose="test", owner_id="another", call_id="call_unknown")
        with patch.object(model, "generate", side_effect=AssertionError("must not resend")):
            with self.assertRaisesRegex(ValueError, "不自动重发"):
                request_model(self.session, model, self.config["model"], messages, purpose="test", owner_id="another", call_id="call_unknown")
        with self.assertRaisesRegex(RuntimeError, "未知"):
            self.run_offline(steps=1)

    def test_reference_cost_budget_needs_source_and_prevents_spend(self):
        self.config["budget"].update(max_estimated_cost=0.000001, input_cost_per_million=10.0,
                                     output_cost_per_million=20.0, price_source="offline test upper rates; 2026-11-12")
        self.create()
        result = self.run_offline()
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual(self.session.storage.all("call"), [])

    def test_remaining_wall_budget_must_cover_complete_request_window(self):
        self.config["budget"]["max_wall_seconds"] = 119.0
        self.create()
        result = self.run_offline()
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual(self.session.storage.all("call"), [])

    def test_pending_confirmation_is_reused_after_completion_commit_interruption(self):
        self.config["confirmation"].update(enabled=True, repeats=2)
        self.create()
        original = self.session.save_generation
        def interrupt_after_group(record, extra_files=None):
            if record["state"].get("confirmation_state") == "completed":
                raise KeyboardInterrupt
            return original(record, extra_files)
        with patch.object(self.session, "save_generation", side_effect=interrupt_after_group):
            with self.assertRaises(KeyboardInterrupt):
                self.run_offline(steps=4)
        self.assertEqual(len(self.session.storage.all("group")), 1)
        before = len([item for _, item in self.session.storage.all("evaluation") if item["purpose"] == "confirmation"])
        self.session = ExperimentSession(self.session.directory, self.root)
        self.run_offline(stop_requested=lambda: True)
        after = len([item for _, item in self.session.storage.all("evaluation") if item["purpose"] == "confirmation"])
        self.assertEqual(before, after)
        self.assertEqual(len(self.session.storage.all("group")), 1)
        self.assertEqual(self.session.storage.get("archive", "current")["snapshot"], self.session.archive_snapshot())

    def test_interrupted_control_wall_time_is_conservatively_recovered(self):
        self.create()
        record = {"control_id": "control_old", "state": "running", "started_at_utc": "2026-01-01T00:00:00+00:00"}
        self.session.storage.put("control", "control_old", record, {})
        recover_experiment(self.session)
        self.assertEqual(self.session.storage.get("control", "control_old")["state"], "interrupted")
        with self.assertRaises(BudgetExceeded):
            check_budget(self.session)

    def test_call_response_commit_before_generation_response_is_recoverable(self):
        self.create()
        original = self.session.save_generation
        def interrupt(record, extra_files=None):
            if record["state"]["status"] == "response_received":
                raise KeyboardInterrupt
            return original(record, extra_files)
        with patch.object(self.session, "save_generation", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                generate_once(self.session, self.model)
        identifier, record = self.session.storage.all("generation")[0]
        self.assertNotIn("response", record)
        self.assertEqual(self.session.storage.get("call", record["plan"]["call_id"])["state"], "completed")
        recover_experiment(self.session)
        self.assertIn("response", self.session.storage.get("generation", identifier))
        self.assertEqual(self.run_offline(steps=1)["new_generations"], 1)
        self.assertEqual(self.session.generation_count(), 2)

    def test_format_failure_continues_but_provider_error_stops(self):
        self.create()
        original = self.model.generate
        count = 0
        def generate(**kwargs):
            nonlocal count
            count += 1
            return {"text": "bad format", "finish_reason": "stop", "usage": {"total_tokens": 0}} if count == 1 else original(**kwargs)
        with patch.object(self.model, "generate", side_effect=generate):
            result = self.run_offline(steps=2)
        self.assertEqual(result["new_generations"], 2)
        self.assertIn("parse_error", {record["state"]["status"] for _, record in self.session.storage.all("generation")})

    def test_meta_calls_are_budgeted_and_credit_uses_parent_delta(self):
        self.config["meta"].update(enabled=True, interval=1, evaluation_window=2)
        self.create()
        self.run_offline(steps=3)
        calls = [record for _, record in self.session.storage.all("call")]
        self.assertEqual(sum(record["plan"]["purpose"] == "program" for record in calls), 3)
        self.assertEqual(sum(record["plan"]["purpose"] == "meta" for record in calls), 2)
        accepted = [record for _, record in self.session.storage.all("meta") if record["state"] == "accepted"]
        self.assertEqual(len(accepted), 1)  # 第二次相同指令必须被去重拒绝。
        self.assertGreater(len(available_templates(self.session)), len(self.config["context"]["templates"]))
        for credit in template_credit(self.session).values():
            self.assertLessEqual(len(credit["observations"]), 2)
            for value in credit["observations"]:
                self.assertAlmostEqual(value["improvement"], value["child_score"] - value["parent_score"])

    def test_confirmation_of_improved_child_is_separate_and_idempotent(self):
        self.config["confirmation"].update(enabled=True, repeats=2)
        self.create()
        self.run_offline(steps=4)
        groups = self.session.storage.all("group")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][1]["summary"]["all_valid_median"], self.session.best()[1].score)
        confirm = [record for _, record in self.session.storage.all("evaluation") if record["purpose"] == "confirmation"]
        self.assertEqual(len(confirm), 2)
        self.run_offline(steps=1)
        self.assertEqual(len(self.session.storage.all("group")), 1)

    def test_stop_request_prevents_new_calls_and_real_local_loop_is_rejected(self):
        self.create()
        result = self.run_offline(stop_requested=lambda: True)
        self.assertEqual(result["stop_reason"], "stop_requested")
        self.assertEqual(self.session.storage.all("call"), [])
        with self.assertRaisesRegex(RuntimeError, "Docker"):
            run_search(self.session, steps=1)

    def test_routing_is_deterministic_and_respects_frozen_model_config(self):
        self.config["routing"]["models"] = [dict(self.config["model"], id="a", weight=1.0),
                                                   dict(self.config["model"], id="b", weight=2.0)]
        self.config["routing"]["models"][1]["model"] = "offline-second"
        routes = [select_model(self.config, index) for index in range(100)]
        self.assertEqual(routes, [select_model(deepcopy(self.config), index) for index in range(100)])
        self.assertEqual({item[1]["id"] for item in routes}, {"a", "b"})
        self.create()
        generate_once(self.session, self.model)
        plan = self.session.storage.all("generation")[0][1]["plan"]
        self.assertEqual((plan["model"], plan["route"]), routes[0])

    def test_independent_batch_runs_are_bounded_and_cannot_duplicate_experiment(self):
        first = self.create()
        second = self.create()
        def loopback_only(sock, address):
            # Windows asyncio 的内部socketpair需要本机回环，不允许外网请求。
            if address[0] not in {"127.0.0.1", "::1"}:
                raise AssertionError("unexpected remote network")
            return REAL_CONNECT(sock, address)
        with patch("socket.socket.connect", new=loopback_only):
            results = asyncio.run(run_batch([first, second], concurrency=1, steps=1,
                        reviewed_fixtures=True, model_factory=lambda session: ReviewedFixtureModel(session.task.initial_program.read_text(encoding="utf-8"))))
            self.assertEqual([result["status"] for result in results], ["stopped", "stopped"])
            self.assertEqual(first.generation_count(), 1)
            self.assertEqual(second.generation_count(), 1)
            with self.assertRaisesRegex(ValueError, "重复"):
                asyncio.run(run_batch([first, first]))


if __name__ == "__main__":
    unittest.main()
