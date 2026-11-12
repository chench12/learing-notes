"""在临时目录验证容器入口协议；这些测试不验证 Docker 隔离。"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class ContainerWorkerTests(unittest.TestCase):
    def run_worker(self, payload):
        with tempfile.TemporaryDirectory(prefix="evolve-container-protocol-") as directory:
            # 仅在本测试中替换容器内路径，正式入口始终使用 /work。
            source = (PROJECT / "Evolve/container_worker.py").read_text(encoding="utf-8")
            source = source.replace('"/work"', repr(directory))
            result = subprocess.run(
                [sys.executable, "-I", "-u", "-X", "utf8", "-c", source],
                input=json.dumps(payload).encode("utf-8") + b"\n", stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=5, cwd=directory,
            )
            header, body = result.stdout.split(b"\n", 1)
            self.assertEqual(json.loads(header)["protocol"], "evolve-container-v1")
            return result, body

    def test_print_and_raw_stdout_writes_do_not_corrupt_solution(self):
        result, output = self.run_worker({
            "mode": "candidate", "code":
            "import os\ndef solve():\n    print('普通日志')\n"
            "    os.write(1, b'raw-log\\n')\n    return [[0.5, 0.5, 0.1]]\n",
        })
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(json.loads(output), [[0.5, 0.5, 0.1]])
        self.assertIn("普通日志", result.stderr.decode("utf-8"))
        self.assertIn(b"raw-log", result.stderr)

    def test_fixed_evaluator_imports_contract_and_verifies_id(self):
        result, output = self.run_worker({
            "mode": "evaluator", "candidate_id": "offline-id", "solution_json": "[1,2,3]",
            "code": "from Evolve.contracts import Evaluation\n"
                    "def evaluate(solution, candidate_id):\n"
                    "    print('evaluator-log')\n    return Evaluation(candidate_id, 'ok', score=sum(solution))\n",
            "contracts": (PROJECT / "Evolve/contracts.py").read_text(encoding="utf-8"),
        })
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(json.loads(output)["score"], 6)
        self.assertEqual(json.loads(output)["candidate_id"], "offline-id")
        self.assertIn(b"evaluator-log", result.stderr)

    def test_fixed_evaluator_rejects_invalid_json_before_evaluate(self):
        result, output = self.run_worker({
            "mode": "evaluator", "candidate_id": "offline-id", "solution_json": "[NaN]",
            "code": "raise AssertionError('evaluator must not be loaded')",
            "contracts": (PROJECT / "Evolve/contracts.py").read_text(encoding="utf-8"),
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, b"")
        self.assertIn("JSON 不允许 NaN", result.stderr.decode("utf-8"))
        self.assertNotIn(b"AssertionError", result.stderr)


if __name__ == "__main__":
    unittest.main()
