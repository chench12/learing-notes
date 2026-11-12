"""同实验异步准入、乱序、单写入者、排空与恢复；禁止远程网络。"""

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from Evolve.async_controller import run_search_async
from Evolve.budget import evaluation_reservation, usage_snapshot
from Evolve.contracts import Evaluation
from Evolve.experiment import create_experiment, digest, _validate_config
from Evolve.recovery import recover_experiment
from Evolve.selection import rebuild_search_statistics
from Evolve.session import ExperimentSession
from Evolve.settings import expanded_config
from Evolve.storage import encode_json
from Examples.run_search import ReviewedFixtureModel


PROJECT = Path(__file__).resolve().parents[1]


def synthetic_evaluation(task, candidate, **kwargs):
    """仅用于调度/预算反例，不代表执行过几何求解。"""
    return Evaluation(candidate.candidate_id, "ok", 2.0 if candidate.parent_id is None else 2.4,
                      features={"radius_cv": 0.1, "boundary_fraction": 0.5})


class AsyncPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Windows fsync/快照写入是协调器的同步事务，0.1s调试阈值太低。
        asyncio.get_running_loop().slow_callback_duration = 5.0

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-async-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("Evolve", "Examples", "tasks"):
            shutil.copytree(PROJECT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        self.config = expanded_config(json.loads((PROJECT / "configs/circle_packing.json").read_text(encoding="utf-8")))
        self.config["pipeline"].update(mode="async", max_inflight=3, model_concurrency=2, evaluation_concurrency=1)
        self.config["search"]["max_generations"] = 6
        self.config["archive"].update(islands=2, parent_sampling="puct_cell")
        output = redirect_stdout(StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        guard = patch("socket.socket.connect", side_effect=AssertionError("unexpected remote call"))
        guard.start()
        self.addCleanup(guard.stop)

    def create(self):
        (self.root / "config.json").write_bytes(encode_json(self.config))
        directory, _ = create_experiment("config.json", self.root)
        self.session = ExperimentSession(directory, self.root)
        self.model = ReviewedFixtureModel(self.session.task.initial_program.read_text(encoding="utf-8"))

    async def run_offline(self, **kwargs):
        return await run_search_async(self.session, self.model, reviewed_fixtures=True, **kwargs)

    async def until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), 8)

    async def test_complete_real_execution_replay_and_confirmation_meta_barriers(self):
        self.config["confirmation"].update(enabled=True, repeats=2)
        self.config["meta"].update(enabled=True, interval=2)
        self.create()
        result = await self.run_offline()
        self.assertEqual((result["state"], result["stop_reason"], result["new_generations"]), ("stopped", "generation_budget", 6))
        self.assertEqual(result["peak_evaluations"], 1)
        self.assertLessEqual(result["peak_inflight"], 3)
        self.assertTrue(self.session.storage.all("group"))
        self.assertTrue(self.session.storage.all("meta"))
        self.assertEqual(usage_snapshot(self.session)["pending_evaluation_seconds"], 0)
        snapshot = self.session.archive_snapshot()
        self.assertEqual(self.session.storage.get("archive", "current")["snapshot"], snapshot)
        self.session = ExperimentSession(self.session.directory, self.root)
        self.assertEqual(self.session.archive_snapshot(), snapshot)
        recover_experiment(self.session)
        before = self.session.storage.stats()
        recover_experiment(self.session)
        self.assertEqual(self.session.storage.stats(), before)

    async def test_models_overlap_with_evaluation_out_of_order_and_only_owner_writes(self):
        self.create()
        first_started, release_first = threading.Event(), threading.Event()
        self.addCleanup(release_first.set)
        original = self.model.generate
        counter, lock = 0, threading.Lock()
        overlap = []
        def generate(**kwargs):
            nonlocal counter
            with lock:
                counter += 1
                index = counter
            if index == 1:
                first_started.set()
                if not release_first.wait(8):
                    raise TimeoutError("test gate")
            return dict(original(**kwargs), fixture_index=index)
        def evaluate(task, candidate, **kwargs):
            overlap.append(first_started.is_set() and not release_first.is_set())
            return synthetic_evaluation(task, candidate, **kwargs)
        owner = threading.get_ident()
        put = self.session.storage.put
        def write(*args, **kwargs):
            self.assertEqual(threading.get_ident(), owner)
            return put(*args, **kwargs)
        with patch.object(self.model, "generate", side_effect=generate), patch("Evolve.async_controller.run_candidate", side_effect=evaluate), patch.object(self.session.storage, "put", side_effect=write):
            task = asyncio.create_task(self.run_offline(steps=3))
            try:
                await self.until(lambda: any(call.get("response", {}).get("fixture_index") == 2 for _, call in self.session.storage.all("call")))
            finally:
                release_first.set()
            result = await task
        self.assertEqual(result["peak_model_calls"], 2)
        self.assertTrue(any(overlap))
        responses = [event["payload"]["response"]["fixture_index"] for event in self.session.storage.events()
                     if event["kind"] == "call" and event["payload"]["state"] == "completed"]
        self.assertLess(responses.index(2), responses.index(1))
        indices = []
        for _, generation in self.session.storage.all("generation"):
            plan = generation["plan"]
            indices.append(plan["selection"]["selection_index"])
            parent = self.session.candidates()[plan["parent_id"]]
            self.assertEqual(digest(parent.code.encode()), plan["parent_source_sha256"])
            self.assertEqual(plan["messages"], self.session.storage.get("call", plan["call_id"])["plan"]["messages"])
        self.assertEqual(sorted(indices), [0, 1, 2])

    async def test_concurrent_requests_cannot_exceed_call_budget(self):
        self.config["budget"]["max_calls"] = 1
        self.create()
        with patch("Evolve.async_controller.run_candidate", side_effect=synthetic_evaluation):
            result = await self.run_offline()
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual(len(self.session.storage.all("call")), 1)
        self.assertEqual(usage_snapshot(self.session)["pending_evaluation_seconds"], 0)
        stats = rebuild_search_statistics(self.session.storage.events(), 2)
        self.assertEqual(sum(island["expansions"] for island in stats["islands"]), 1)

    async def test_child_budget_is_held_before_request_and_not_spent_by_next_parent(self):
        self.config["budget"]["max_evaluation_seconds"] = 14.0
        self.create()
        self.assertEqual(evaluation_reservation(self.session), 12.0)
        reserved = []
        original = self.model.generate
        def generate(**kwargs):
            reserved.append(usage_snapshot(self.session)["pending_evaluation_seconds"])
            return original(**kwargs)
        with patch.object(self.model, "generate", side_effect=generate), patch("Evolve.async_controller.run_candidate", side_effect=synthetic_evaluation):
            result = await self.run_offline()
        self.assertTrue(reserved)
        self.assertGreaterEqual(reserved[0], 12.0)
        self.assertEqual(len(self.session.storage.all("call")), 1)
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual([row["state"]["status"] for _, row in self.session.storage.all("generation")], ["completed"])

    async def test_stop_and_coroutine_cancellation_drain_issued_call_before_unlock(self):
        self.create()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.model.generate
        def generate(**kwargs):
            started.set()
            if not release.wait(8):
                raise TimeoutError("test gate")
            return original(**kwargs)
        with patch.object(self.model, "generate", side_effect=generate), patch("Evolve.async_controller.run_candidate", side_effect=synthetic_evaluation):
            task = asyncio.create_task(self.run_offline(steps=1))
            await self.until(started.is_set)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            with self.assertRaisesRegex(RuntimeError, "已有异步控制器"):
                await self.run_offline(steps=1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(self.session.storage.all("call")), 1)
        self.assertEqual(self.session.storage.all("call")[0][1]["state"], "completed")
        self.assertEqual(self.session.storage.all("generation")[0][1]["state"]["status"], "completed")
        control = self.session.storage.all("control")[0][1]
        self.assertEqual((control["state"], control["stop_reason"]), ("stopped", "stop_requested"))

    async def test_provider_uncertainty_stops_and_is_never_resent(self):
        self.create()
        with patch.object(self.model, "generate", side_effect=TimeoutError), patch("Evolve.async_controller.run_candidate", side_effect=synthetic_evaluation):
            result = await self.run_offline(steps=1)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.session.storage.all("call")[0][1]["state"], "uncertain")
        self.session = ExperimentSession(self.session.directory, self.root)
        with patch.object(self.model, "generate", side_effect=AssertionError("must not resend")):
            with self.assertRaisesRegex(RuntimeError, "未知"):
                await self.run_offline()

    async def test_saved_response_resumes_after_generation_commit_failure_without_request(self):
        self.create()
        save = self.session.save_generation
        def fail(record, extra_files=None):
            if record["state"]["status"] == "response_received":
                raise OSError("test commit interruption")
            return save(record, extra_files)
        with patch.object(self.session, "save_generation", side_effect=fail), patch("Evolve.async_controller.run_candidate", side_effect=synthetic_evaluation):
            result = await self.run_offline(steps=1)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.session.storage.all("call")[0][1]["state"], "completed")
        self.session = ExperimentSession(self.session.directory, self.root)
        with patch.object(self.model, "generate", side_effect=AssertionError("must not resend")), patch("Evolve.async_controller.run_candidate", side_effect=synthetic_evaluation):
            await self.run_offline(stop_requested=lambda: True)
        self.assertEqual(len(self.session.storage.all("call")), 1)
        self.assertEqual(self.session.storage.all("generation")[0][1]["state"]["status"], "completed")

    async def test_stop_before_start_and_invalid_local_remote_mode(self):
        self.create()
        result = await self.run_offline(stop_requested=lambda: True)
        self.assertEqual(result["new_generations"], 0)
        self.assertFalse(self.session.storage.all("call"))
        with self.assertRaisesRegex(RuntimeError, "Docker"):
            await run_search_async(self.session, steps=1)

    async def test_stop_file_closes_queued_requests_and_releases_child_holds(self):
        self.config["pipeline"]["model_concurrency"] = 1
        self.create()
        release = threading.Event()
        self.addCleanup(release.set)
        original = self.model.generate
        def generate(**kwargs):
            if not release.wait(8):
                raise TimeoutError("test gate")
            return original(**kwargs)
        with patch.object(self.model, "generate", side_effect=generate), patch("Evolve.async_controller.run_candidate", side_effect=synthetic_evaluation):
            task = asyncio.create_task(self.run_offline())
            try:
                await self.until(lambda: self.session.generation_count() >= 2)
                (self.session.directory / "STOP").touch()
            finally:
                release.set()
            result = await task
        self.assertEqual(result["stop_reason"], "stop_requested")
        self.assertEqual(len(self.session.storage.all("call")), 1)
        states = [item["state"]["status"] for _, item in self.session.storage.all("generation")]
        self.assertIn("not_sent", states)
        self.assertEqual(usage_snapshot(self.session)["pending_evaluation_seconds"], 0)
        self.assertEqual(sum(island["expansions"] for island in rebuild_search_statistics(self.session.storage.events(), 2)["islands"]), 1)

    async def test_missing_usage_keeps_token_reservation_across_concurrent_admission(self):
        self.config["budget"]["max_tokens"] = 15000
        self.create()
        original = self.model.generate
        with patch.object(self.model, "generate", side_effect=lambda **kw: dict(original(**kw), usage=None)), patch("Evolve.async_controller.run_candidate", side_effect=synthetic_evaluation):
            result = await self.run_offline()
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        calls = self.session.storage.all("call")
        self.assertEqual(len(calls), 1)
        self.assertEqual(usage_snapshot(self.session)["charged_tokens"], calls[0][1]["plan"]["reservation"]["total_tokens"])

    async def test_evaluation_workers_can_overlap_within_separate_limit(self):
        self.config["pipeline"]["evaluation_concurrency"] = 2
        self.create()
        pair = threading.Event()
        current = peak = count = 0
        lock = threading.Lock()
        def evaluate(task, candidate, **kwargs):
            nonlocal current, peak, count
            with lock:
                count += 1
                index = count
                current += 1
                peak = max(peak, current)
                if current == 2:
                    pair.set()
            try:
                if index > 1 and not pair.wait(8):
                    raise TimeoutError("second evaluation worker did not start")
                return synthetic_evaluation(task, candidate, **kwargs)
            finally:
                with lock:
                    current -= 1
        with patch("Evolve.async_controller.run_candidate", side_effect=evaluate):
            result = await self.run_offline(steps=3)
        self.assertEqual((result["state"], result["peak_evaluations"], peak), ("stopped", 2, 2))

    def test_pipeline_limits_are_frozen_and_validated(self):
        for field, value in [("max_inflight", 0), ("evaluation_concurrency", True),
                             ("model_concurrency", 10), ("mode", "unknown")]:
            config = deepcopy(self.config)
            config["pipeline"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                _validate_config(config)
