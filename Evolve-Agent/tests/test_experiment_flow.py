"""离线验证记录隔离、生成失败及继续实验；不调用模型 API。"""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from Evolve.experiment import create_experiment
from Evolve.generation import generate_once
from Evolve.session import ExperimentSession


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INITIAL_CODE = (PROJECT_ROOT / "tasks/circle_packing/initial.py").read_text(encoding="utf-8")
ALGORITHM_START = INITIAL_CODE.index("def initialize():")
ALGORITHM_END = INITIAL_CODE.index("# EVOLVE-BLOCK-END", ALGORITHM_START)


def with_algorithm(algorithm):
    return INITIAL_CODE[:ALGORITHM_START] + algorithm + INITIAL_CODE[ALGORITHM_END:]


GOOD_CODE = with_algorithm("""def initialize():
    circles = [[(i % 5 + 0.5) / 5, (i // 5 + 0.5) / 5, 0.1]
               for i in range(25)]
    circles.append([0.2, 0.2, (math.sqrt(2) - 1) / 10])
    return circles

def optimize(circles):
    return circles
""")


class OfflineModel:
    def __init__(self, text=GOOD_CODE, finish_reason="stop", error=None):
        self.text = text
        self.finish_reason = finish_reason
        self.error = error
        self.calls = 0
        self.last_request = None

    def generate(self, **kwargs):
        self.calls += 1
        self.last_request = kwargs
        if self.error:
            raise self.error
        return {
            "text": self.text,
            "finish_reason": self.finish_reason,
            "model": "offline-fixture",
            "usage": {"total_tokens": 0},
        }


class ExperimentFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-flow-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ["Evolve", "Examples", "tasks"]:
            shutil.copytree(
                PROJECT_ROOT / name, self.root / name,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        config = json.loads(
            (PROJECT_ROOT / "configs/circle_packing.json").read_text(encoding="utf-8")
        )
        config["search"]["max_generations"] = 2
        # 保留原有整文件重写测试；补丁流程在下面单独验证。
        config["search"]["generation_mode"] = "rewrite"
        # 本模块保留原贪心对照；混合档案的选择与恢复另行验证。
        config["archive"]["strategy"] = "greedy"
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.directory, _ = create_experiment("config.json", self.root)
        self.session = ExperimentSession(self.directory, self.root)
        # 任何误用真实网络的路径都会令测试失败。
        guard = patch("socket.socket.connect", side_effect=AssertionError("unexpected network"))
        guard.start()
        self.addCleanup(guard.stop)

    def generate(self, model):
        with redirect_stdout(StringIO()):
            return generate_once(self.session, model)

    def states(self):
        return [
            json.loads(p.read_text(encoding="utf-8"))
            for p in self.directory.glob("generations/*/state.json")
        ]

    def use_patch_mode(self):
        path = self.root / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["search"]["generation_mode"] = "patch"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.directory, _ = create_experiment("config.json", self.root)
        self.session = ExperimentSession(self.directory, self.root)

    def test_patch_generation_records_parent_and_applied_modifications(self):
        self.use_patch_mode()
        search = INITIAL_CODE[ALGORITHM_START:ALGORITHM_END]
        end = GOOD_CODE.index("# EVOLVE-BLOCK-END", ALGORITHM_START)
        replacement = GOOD_CODE[ALGORITHM_START:end]
        text = f"改进布局。\n<<<<<<< SEARCH\n{search}=======\n{replacement}>>>>>>> REPLACE\n"
        model = OfflineModel(text=text)
        child, result = self.generate(model)
        self.assertEqual(result.status, "ok")
        self.assertGreater(result.score, 2.5)
        self.assertEqual(child.code, GOOD_CODE)
        self.assertEqual(self.session.candidates()[child.parent_id].code, INITIAL_CODE)
        folder = next(self.directory.glob("generations/*"))
        self.assertEqual(json.loads((folder / "plan.json").read_text(encoding="utf-8"))["generation_mode"], "patch")
        self.assertEqual(len(json.loads((folder / "patches.json").read_text(encoding="utf-8"))), 1)
        self.assertIn("SEARCH/REPLACE", model.last_request["messages"][0]["content"])

    def test_patch_failure_keeps_parent_and_does_not_save_partial_child(self):
        self.use_patch_mode()
        text = (
            "<<<<<<< SEARCH\n    radius = 1 / 12\n=======\n    radius = 1 / 13\n>>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\nnot present\n=======\nnew text\n>>>>>>> REPLACE\n"
        )
        # 第一个 SEARCH 的缩进同样必须精确匹配实际源代码。
        text = text.replace("    radius", "        radius")
        with self.assertRaisesRegex(ValueError, "补丁应用失败"):
            self.generate(OfflineModel(text=text))
        self.assertEqual(self.states()[0]["status"], "patch_error")
        self.assertEqual(len(self.session.candidates()), 1)
        self.assertEqual(next(iter(self.session.candidates().values())).code, INITIAL_CODE)
        self.assertEqual(len(list(self.directory.glob("evaluations/*/evaluation.json"))), 1)

    def test_patch_mode_does_not_silently_accept_full_rewrite(self):
        self.use_patch_mode()
        with self.assertRaisesRegex(ValueError, "补丁解析失败"):
            self.generate(OfflineModel(text=GOOD_CODE))
        self.assertEqual(self.states()[0]["status"], "parse_error")

    def test_patch_cannot_modify_fixed_entrypoint(self):
        self.use_patch_mode()
        text = "<<<<<<< SEARCH\ndef solve():\n    return optimize(initialize())\n=======\ndef solve():\n    return []\n>>>>>>> REPLACE\n"
        with self.assertRaisesRegex(ValueError, "越过可进化区域"):
            self.generate(OfflineModel(text=text))
        self.assertEqual(self.states()[0]["status"], "patch_error")
        self.assertEqual(len(self.session.candidates()), 1)

    def test_patch_with_syntax_error_is_saved_but_not_evaluated(self):
        self.use_patch_mode()
        text = "<<<<<<< SEARCH\ndef initialize():\n=======\ndef initialize(:\n>>>>>>> REPLACE\n"
        with self.assertRaisesRegex(ValueError, "语法错误"):
            self.generate(OfflineModel(text=text))
        self.assertEqual(self.states()[0]["status"], "syntax_error")
        self.assertEqual(len(self.session.candidates()), 2)
        self.assertEqual(len(list(self.directory.glob("evaluations/*/evaluation.json"))), 1)

    def test_success_resume_and_budget(self):
        model = OfflineModel()
        first, result = self.generate(model)
        self.assertEqual(result.status, "ok")
        self.assertGreater(result.score, 2.5)
        self.assertEqual(len(list(self.directory.glob("evaluations/*/evaluation.json"))), 2)
        self.assertTrue(all(s["status"] == "completed" for s in self.states()))
        self.session = ExperimentSession(self.directory, self.root)
        second, _ = self.generate(model)
        self.assertEqual(second.parent_id, first.candidate_id)
        self.assertEqual(len(list(self.directory.glob("evaluations/*/evaluation.json"))), 4)
        with self.assertRaisesRegex(ValueError, "max_generations"):
            self.generate(model)
        self.assertEqual(model.calls, 2)

    def test_repeated_evaluations_do_not_overwrite(self):
        parent = self.session.parent()
        first_id, _ = self.session.evaluate(parent, "parent")
        path = self.directory / "evaluations" / first_id / "evaluation.json"
        original = path.read_bytes()
        first_artifacts = {
            p.name: p.read_bytes() for p in (path.parent / "artifacts").iterdir()
        }
        record = json.loads(original)
        from Evolve.experiment import digest
        for name, expected in record["artifacts"].items():
            self.assertEqual(digest((path.parent / name).read_bytes()), expected)
        self.assertIn("solution.json", first_artifacts)
        second_id, _ = self.session.evaluate(parent, "parent")
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(first_artifacts, {
            p.name: p.read_bytes() for p in (path.parent / "artifacts").iterdir()
        })

    def test_unexpected_runner_failure_does_not_leave_running_record(self):
        parent = self.session.parent()
        with patch("Evolve.session.run_candidate", side_effect=OSError("offline IO failure")):
            with self.assertRaises(OSError):
                self.session.evaluate(parent, "parent")
        path = next(self.directory.glob("evaluations/*/evaluation.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error_type"], "OSError")
        self.assertNotIn("result", record)

    def test_generation_failures_keep_records(self):
        cases = [
            (OfflineModel(finish_reason="length"), "incomplete"),
            (OfflineModel(text=""), "parse_error"),
        ]
        for model, status in cases:
            with self.assertRaises(ValueError):
                self.generate(model)
            self.assertIn(status, [s["status"] for s in self.states()])
        self.assertEqual(self.session.generation_count(), 2)
        self.assertEqual(len(list(self.directory.glob("generations/*/response.json"))), 2)

    def test_syntax_error_keeps_source_without_child_evaluation(self):
        with self.assertRaisesRegex(ValueError, "语法错误"):
            self.generate(OfflineModel(text="def solve(:"))
        self.assertEqual(self.states()[0]["status"], "syntax_error")
        self.assertEqual(len(self.session.candidates()), 2)
        self.assertEqual(len(list(self.directory.glob("evaluations/*/evaluation.json"))), 1)

    def test_unknown_request_failure_is_uncertain_without_error_body(self):
        with self.assertRaisesRegex(RuntimeError, "RuntimeError"):
            self.generate(OfflineModel(error=RuntimeError("test-private-error-body")))
        self.assertEqual(self.states()[0]["status"], "uncertain")
        state = next(self.directory.glob("generations/*/state.json"))
        self.assertNotIn("test-private-error-body", state.read_text(encoding="utf-8"))

    def test_invalid_child_does_not_replace_valid_parent(self):
        code = with_algorithm("def initialize(): return []\ndef optimize(circles): return circles\n")
        child, result = self.generate(OfflineModel(text=code))
        self.assertEqual(result.status, "invalid")
        self.assertEqual(self.session.best()[0].candidate_id, child.parent_id)

    def test_skeleton_error_does_not_execute_child(self):
        code = GOOD_CODE.replace(
            "def solve():\n    return optimize(initialize())",
            "def solve():\n    raise RuntimeError('must not execute')",
        )
        with self.assertRaisesRegex(ValueError, "骨架校验失败"):
            self.generate(OfflineModel(text=code))
        self.assertEqual(self.states()[0]["status"], "skeleton_error")
        self.assertEqual(len(list(self.directory.glob("evaluations/*/evaluation.json"))), 1)
        self.assertEqual(len(self.session.candidates()), 2)

    def test_parent_code_also_checked_against_initial_skeleton(self):
        parent = self.session.parent()
        from Evolve.contracts import Candidate
        changed = Candidate(
            task_id=parent.task_id,
            code=parent.code.replace("return optimize(initialize())", "return []"),
        )
        self.session.save_candidate(changed)
        with self.assertRaisesRegex(ValueError, "固定骨架"):
            self.session.evaluate(changed, "parent")
        self.assertEqual(len(list(self.directory.glob("evaluations/*/evaluation.json"))), 0)

    def test_task_uses_snapshot_and_framework_changes_are_rejected(self):
        (self.root / "tasks/circle_packing/evaluator.py").write_text(
            "raise RuntimeError('must not execute live evaluator')", encoding="utf-8",
        )
        self.assertEqual(self.generate(OfflineModel())[1].status, "ok")
        (self.root / "Evolve/runner.py").write_text("# changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "框架版本"):
            ExperimentSession(self.directory, self.root)

    def test_modified_manifest_and_snapshot_are_rejected(self):
        path = self.directory / "manifest.json"
        original = path.read_bytes()
        manifest = json.loads(original)
        manifest["config"]["task"]["timeout_seconds"] = 5
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "指纹"):
            ExperimentSession(self.directory, self.root)
        path.write_bytes(original)
        (self.directory / "snapshot/task/initial.py").write_text("# changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "快照"):
            ExperimentSession(self.directory, self.root)


if __name__ == "__main__":
    unittest.main()
