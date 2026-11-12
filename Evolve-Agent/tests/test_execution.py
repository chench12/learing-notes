"""本地执行后端的实际进程测试，不调用模型或使用真实凭据。"""

from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

from Evolve.contracts import Candidate, ExecutionLimits
from Evolve.loader import load_task
from Evolve.runner import run_candidate


PROJECT = Path(__file__).resolve().parents[1]


def process_alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        # 孤儿进程可能等待 init 回收；僵尸已无法执行。
        status = Path(f"/proc/{pid}/stat")
        return not (status.exists() and status.read_text().split(") ", 1)[1].startswith("Z"))
    except ProcessLookupError:
        return False


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-execution-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root / "artifacts"
        self.task = load_task(PROJECT / "tasks/circle_packing", "circle_packing_26", 3)
        self.seed = self.task.initial_program.read_text(encoding="utf-8")

    def run_code(self, code, *, limits=None, task=None):
        self.candidate = Candidate(self.task.task_id, code)
        result = run_candidate(
            task or self.task, self.candidate,
            artifact_dir=self.artifacts, limits=limits,
        )
        self.record = json.loads((self.artifacts / "execution.json").read_text(encoding="utf-8"))
        self.assertEqual(self.record["result"]["status"], result.status)
        return result

    def test_seed_solution_logs_and_execution_persist(self):
        result = self.run_code(self.seed + '\nprint("candidate-log")\n')
        self.assertEqual(result.status, "ok", result.feedback)
        circles = json.loads((self.artifacts / "solution.json").read_text(encoding="utf-8"))
        self.assertEqual(len(circles), 26)
        self.assertAlmostEqual(sum(c[2] for c in circles), result.score)
        self.assertIn("candidate-log", (self.artifacts / "candidate.log").read_text())
        self.assertTrue((self.artifacts / "evaluator.log").exists())
        for phase in self.record["phases"].values():
            self.assertEqual(phase["returncode"], 0)
            self.assertGreater(phase["elapsed_seconds"], 0)
            self.assertEqual(phase["memory_limit_enforced"], os.name == "nt")
            if os.name == "nt":
                self.assertGreater(phase["peak_memory_bytes"], 0)
        self.assertFalse(self.record["filesystem_isolated"])
        self.assertFalse(self.record["network_isolated"])

    def test_invalid_solution_is_kept(self):
        result = self.run_code("def solve(): return []\n")
        self.assertEqual(result.status, "invalid")
        self.assertIsNone(result.score)
        self.assertEqual(json.loads((self.artifacts / "solution.json").read_text()), [])

    def test_runtime_error_keeps_traceback_without_evaluator(self):
        result = self.run_code("def solve(): raise RuntimeError('offline-failure')\n")
        self.assertEqual(result.status, "error")
        self.assertIn("offline-failure", result.feedback)
        self.assertIn("Traceback", (self.artifacts / "candidate.log").read_text())
        self.assertNotIn("evaluator", self.record["phases"])

    def test_syntax_error_keeps_log(self):
        result = self.run_code("def solve(:\n")
        self.assertEqual(result.status, "error")
        self.assertIn("SyntaxError", result.feedback)

    def test_clean_exit_without_output_is_error(self):
        result = self.run_code("import sys\nsys.exit(0)\n")
        self.assertEqual(result.status, "error")
        self.assertIn("没有输出", result.feedback)

    def test_nonfinite_json_cannot_reach_fixed_evaluator(self):
        result = self.run_code(
            "from pathlib import Path\nimport sys\n"
            "Path('solution.json').write_text('[NaN]')\nsys.exit(0)\n",
        )
        self.assertEqual(result.status, "error")
        self.assertIn("JSON 不允许 NaN", result.feedback)
        self.assertEqual((self.artifacts / "solution.json").read_text(), "[NaN]")

    def test_worker_rejects_nonfinite_return_value(self):
        result = self.run_code("def solve(): return [float('nan')]\n")
        self.assertEqual(result.status, "error")
        self.assertNotIn("evaluator", self.record["phases"])

    def test_environment_does_not_inherit_caller_secrets(self):
        with patch.dict(os.environ, {"EVOLVE_TEST_SECRET": "offline-sentinel", "RELAY_API_KEY": "fake-test-value"}):
            result = self.run_code(
                "import os\nassert 'EVOLVE_TEST_SECRET' not in os.environ\n"
                "assert 'RELAY_API_KEY' not in os.environ\n" + self.seed,
            )
        self.assertEqual(result.status, "ok", result.feedback)
        self.assertNotIn("offline-sentinel", (self.artifacts / "candidate.log").read_text())

    def test_log_limit_caps_saved_bytes_and_fails_fast_exit(self):
        result = self.run_code(
            "def solve():\n    print('x' * 100000)\n    return []\n",
            limits=replace(ExecutionLimits(), max_log_bytes=1024),
        )
        self.assertEqual(result.status, "error")
        self.assertEqual((self.artifacts / "candidate.log").stat().st_size, 1024)
        phase = self.record["phases"]["candidate"]
        self.assertTrue(phase["log_truncated"])
        self.assertEqual(phase["resource_limit"], "log_bytes")

    def test_large_output_keeps_only_explicit_partial_file(self):
        result = self.run_code(
            "def solve(): return 'x' * 200000\n",
            limits=replace(ExecutionLimits(), max_solution_bytes=1024),
        )
        self.assertEqual(result.status, "error")
        self.assertFalse((self.artifacts / "solution.json").exists())
        self.assertEqual((self.artifacts / "solution.json.partial").stat().st_size, 1024)
        self.assertFalse(self.record["solution"]["complete"])
        self.assertNotIn("evaluator", self.record["phases"])

    def test_workspace_monitor_also_checks_fast_exit(self):
        result = self.run_code(
            "from pathlib import Path\ndef solve():\n"
            "    Path('extra.bin').write_bytes(b'x' * 100000)\n    return []\n",
            limits=replace(ExecutionLimits(), max_workdir_bytes=5000),
        )
        self.assertEqual(result.status, "error")
        self.assertEqual(self.record["phases"]["candidate"]["resource_limit"], "workdir_bytes")

    def test_evaluator_has_independent_timeout(self):
        evaluator = self.root / "slow_evaluator.py"
        evaluator.write_text("def evaluate(solution, candidate_id):\n    while True: pass\n")
        result = self.run_code(
            self.seed, task=replace(self.task, evaluator=evaluator),
            limits=replace(ExecutionLimits(), evaluator_timeout_seconds=0.5),
        )
        self.assertEqual(result.status, "timeout")
        self.assertIn("固定评测器", result.feedback)
        self.assertTrue((self.artifacts / "solution.json").exists())
        self.assertEqual(self.record["phases"]["candidate"]["returncode"], 0)

    def test_evaluator_wrong_candidate_id_is_rejected(self):
        evaluator = self.root / "wrong_evaluator.py"
        evaluator.write_text(
            "from Evolve.contracts import Evaluation\n"
            "def evaluate(solution, candidate_id): return Evaluation('wrong-id', 'ok', score=100)\n",
        )
        result = self.run_code(self.seed, task=replace(self.task, evaluator=evaluator))
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.score)
        self.assertIn("candidate_id", result.feedback)

    def assert_descendant_cleanup(self, timeout):
        code = (
            "import subprocess, sys, time\ndef solve():\n"
            "    child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'])\n"
            "    print(child.pid, flush=True)\n"
            + ("    time.sleep(30)\n" if timeout else "") + "    return []\n"
        )
        result = self.run_code(code, task=replace(self.task, timeout_seconds=1.0))
        text = (self.artifacts / "candidate.log").read_text().strip()
        self.assertTrue(text, result.feedback)
        pid = int(text.splitlines()[0])

        def cleanup():
            if process_alive(pid):
                os.kill(pid, signal.SIGTERM)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 2
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(process_alive(pid), f"descendant {pid} survived")
        self.assertEqual(result.status, "timeout" if timeout else "invalid", result.feedback)

    def test_timeout_kills_descendants(self):
        self.assert_descendant_cleanup(timeout=True)

    def test_normal_exit_also_kills_descendants(self):
        self.assert_descendant_cleanup(timeout=False)

    @unittest.skipUnless(os.name == "nt", "Job Object quotas are Windows-specific")
    def test_windows_memory_limit_prevents_large_allocation(self):
        result = self.run_code(
            "def solve():\n    value = bytearray(256 * 1024 * 1024)\n    return []\n",
            limits=replace(ExecutionLimits(), memory_mb=96),
        )
        self.assertEqual(result.status, "error")
        self.assertTrue(self.record["phases"]["candidate"]["memory_limit_enforced"])
        self.assertIsNone(result.score)

    def test_task_mismatch_rejected_before_execution(self):
        with self.assertRaisesRegex(ValueError, "task_id"):
            run_candidate(self.task, Candidate("other", self.seed), artifact_dir=self.artifacts)
        self.assertFalse(self.artifacts.exists())

    @unittest.skipUnless(os.name == "nt", "Job Object quotas are Windows-specific")
    def test_windows_process_limit_blocks_descendant_creation(self):
        result = self.run_code(
            "import subprocess, sys\ndef solve():\n"
            "    subprocess.Popen([sys.executable, '-c', 'pass'])\n    return []\n",
            limits=replace(ExecutionLimits(), max_processes=1),
        )
        self.assertEqual(result.status, "error")
        self.assertTrue(self.record["phases"]["candidate"]["process_limit_enforced"])
        self.assertIsNone(result.score)

    def test_invalid_execution_limits_are_rejected(self):
        for values in [
            {"memory_mb": 0}, {"max_processes": True}, {"max_log_bytes": -1},
            {"max_solution_bytes": 1.2}, {"evaluator_timeout_seconds": float("nan")},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ExecutionLimits(**values)

    def test_attachment_failure_never_starts_candidate(self):
        sentinel = self.root / "should-not-exist"
        with patch("Evolve.execution.ProcessGroup.attach", side_effect=OSError("offline attach failure")):
            result = self.run_code(
                f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\ndef solve(): return []\n",
            )
        self.assertEqual(result.status, "error")
        self.assertEqual(self.record["phases"]["candidate"]["status"], "launch_error")
        self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
