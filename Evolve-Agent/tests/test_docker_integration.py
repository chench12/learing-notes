"""真实 Linux 容器验收；显式设置 EVOLVE_DOCKER_TEST_IMAGE 后运行。

仅使用本地已有镜像，不拉取、不调用模型。未配置时必须显示 skipped。
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from Evolve.contracts import Candidate, ExecutionLimits
from Evolve.docker_execution import docker_command, inspect_image
from Evolve.loader import load_task
from Evolve.runner import run_candidate


PROJECT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("EVOLVE_DOCKER_TEST_IMAGE")


@unittest.skipUnless(IMAGE, "需要 Docker 和 EVOLVE_DOCKER_TEST_IMAGE；未验证真实隔离")
class DockerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.limits = replace(ExecutionLimits(), backend="docker", docker_image=IMAGE)
        inspect_image(IMAGE)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-docker-real-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task = load_task(PROJECT / "tasks/circle_packing", "circle_packing_26", 3)
        self.seed = self.task.initial_program.read_text(encoding="utf-8")
        self.artifacts = self.root / "artifacts"

    def run_code(self, code, *, limits=None, task=None):
        candidate = Candidate(self.task.task_id, code)
        result = run_candidate(task or self.task, candidate, artifact_dir=self.artifacts,
                               limits=limits or self.limits)
        self.record = json.loads((self.artifacts / "execution.json").read_text(encoding="utf-8"))
        # 确认本次 UUID 容器都已消失；只清理本测试创建的容器。
        for phase in self.record["phases"].values():
            name = phase["container_name"]
            self.assertTrue(phase["cleanup_succeeded"], phase)
            self.assertFalse(docker_command(["ps", "--all", "--quiet", "--filter", f"name=^/{name}$"]).strip())
        return result

    def test_seed_runs_in_distinct_candidate_and_evaluator_containers(self):
        result = self.run_code(self.seed)
        self.assertEqual(result.status, "ok", result.feedback)
        self.assertAlmostEqual(result.score, 26 / 12)
        self.assertTrue(self.record["network_isolated"])
        self.assertTrue(self.record["filesystem_isolated"])
        phases = self.record["phases"]
        self.assertNotEqual(phases["candidate"]["container_name"], phases["evaluator"]["container_name"])
        for phase in phases.values():
            self.assertIn("Linux", phase["runtime"]["platform"])
            self.assertEqual(phase["image"]["id"], IMAGE)

    def test_host_canary_env_network_and_root_write_are_blocked(self):
        host_canary = self.root / "host-canary.txt"
        host_canary.write_text("host-only-test-data", encoding="utf-8")
        code = (
            "import os, socket\nfrom pathlib import Path\n"
            "assert 'EVOLVE_SANDBOX_TEST_SECRET' not in os.environ\n"
            f"assert not Path({host_canary.as_posix()!r}).exists()\n"
            f"assert not Path({self.task.evaluator.as_posix()!r}).exists()\n"
            "assert os.getuid() == 65534\n"
            "try:\n    Path('/root-write-probe').write_text('test')\n"
            "except OSError:\n    pass\nelse:\n    raise AssertionError('root filesystem writable')\n"
            "with socket.socket() as sock:\n    sock.settimeout(0.3)\n"
            "    assert sock.connect_ex(('198.51.100.1', 80)) != 0\n" + self.seed
        )
        with patch.dict(os.environ, {"EVOLVE_SANDBOX_TEST_SECRET": "fake-sentinel"}):
            result = self.run_code(code)
        self.assertEqual(result.status, "ok", result.feedback)
        self.assertEqual(host_canary.read_text(), "host-only-test-data")

    def test_timeout_with_descendant_removes_container(self):
        result = self.run_code(
            "import subprocess,sys,time\ndef solve():\n"
            "    subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            "    time.sleep(60)\n",
            task=replace(self.task, timeout_seconds=0.5),
        )
        self.assertEqual(result.status, "timeout", result.feedback)
        self.assertIsNone(result.score)

    def test_large_output_is_partial_and_never_evaluated(self):
        result = self.run_code("def solve(): return 'x' * 100000\n",
                               limits=replace(self.limits, max_solution_bytes=1024))
        self.assertEqual(result.status, "error", result.feedback)
        self.assertEqual((self.artifacts / "solution.json.partial").stat().st_size, 1024)
        self.assertNotIn("evaluator", self.record["phases"])

    def test_log_is_capped(self):
        result = self.run_code("def solve():\n    print('x' * 100000, flush=True)\n    return []\n",
                               limits=replace(self.limits, max_log_bytes=1024))
        self.assertEqual(result.status, "error", result.feedback)
        self.assertEqual((self.artifacts / "candidate.log").stat().st_size, 1024)

    def test_tmpfs_size_limit_rejects_large_file(self):
        result = self.run_code(
            "from pathlib import Path\ndef solve():\n"
            "    Path('/work/large.bin').write_bytes(b'x' * 4000000)\n    return []\n",
            limits=replace(self.limits, max_workdir_bytes=262144),
        )
        self.assertEqual(result.status, "error", result.feedback)
        self.assertIsNone(result.score)

    def test_memory_limit_rejects_large_allocation(self):
        result = self.run_code("def solve():\n    data = bytearray(256 * 1024 * 1024)\n    return []\n",
                               limits=replace(self.limits, memory_mb=96))
        self.assertEqual(result.status, "error", result.feedback)
        self.assertIsNone(result.score)
        self.assertTrue(self.record["phases"]["candidate"]["memory_limit_enforced"])

    def test_evaluator_has_separate_timeout(self):
        evaluator = self.root / "slow_evaluator.py"
        evaluator.write_text("def evaluate(solution, candidate_id):\n    while True: pass\n")
        result = self.run_code(self.seed, task=replace(self.task, evaluator=evaluator),
                               limits=replace(self.limits, evaluator_timeout_seconds=0.5))
        self.assertEqual(result.status, "timeout", result.feedback)
        self.assertIn("固定评测器", result.feedback)
        self.assertTrue((self.artifacts / "solution.json").exists())


if __name__ == "__main__":
    unittest.main()
