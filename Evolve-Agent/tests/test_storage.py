"""事务、导出中断、幂等写入及 OS 锁；不执行模型请求。"""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from Evolve.storage import Storage


PROJECT = Path(__file__).resolve().parents[1]


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-storage-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Storage(self.root, "experiment", "fingerprint", create=True)

    def test_commit_survives_export_failure_and_repairs_without_duplicate_event(self):
        with patch.object(self.store, "publish_pending", side_effect=OSError("offline export failure")):
            with self.assertRaises(OSError):
                self.store.put("candidate", "candidate", {"code": "fixture"}, {"candidates/candidate/candidate.py": b"fixture"})
        self.assertEqual(self.store.get("candidate", "candidate"), {"code": "fixture"})
        self.assertEqual(self.store.stats()["pending_exports"], 1)
        self.assertFalse((self.root / "candidates/candidate/candidate.py").exists())
        reopened = Storage(self.root, "experiment", "fingerprint")
        reopened.publish_pending()
        reopened.verify_files()
        self.assertEqual((self.root / "candidates/candidate/candidate.py").read_bytes(), b"fixture")
        self.assertEqual(reopened.stats()["events"], 1)
        self.assertEqual(reopened.stats()["pending_exports"], 0)

    def test_identical_write_is_idempotent_and_old_revisions_remain_in_events(self):
        files = {"generations/gen/state.json": b"first"}
        self.store.put("generation", "gen", {"state": {"status": "planned"}}, files)
        counts = self.store.stats()
        self.store.put("generation", "gen", {"state": {"status": "planned"}}, files)
        self.assertEqual(self.store.stats(), counts)
        self.store.put("generation", "gen", {"state": {"status": "requesting"}}, {"generations/gen/state.json": b"second"})
        with self.store.connection() as connection:
            rows = connection.execute("SELECT revision,state FROM events ORDER BY sequence").fetchall()
        self.assertEqual(rows, [(1, "planned"), (2, "requesting")])

    def test_file_ownership_conflict_rolls_back_record_and_event(self):
        path = "candidates/candidate/candidate.py"
        self.store.put("candidate", "candidate", {"code": "fixture"}, {path: b"fixture"})
        counts = self.store.stats()
        with self.assertRaisesRegex(ValueError, "接管"):
            self.store.put("generation", "gen", {"state": "bad"}, {path: b"replacement"})
        self.assertIsNone(self.store.get("generation", "gen"))
        self.assertEqual(self.store.stats(), counts)
        self.assertEqual((self.root / path).read_bytes(), b"fixture")

    def test_path_escape_and_tampered_completed_export_are_rejected(self):
        for path in ["../outside", "candidates/../../outside", "C:/outside", "candidates\\bad", "/outside"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.store.put("candidate", "candidate", {}, {path: b"bad"})
        self.store.put("candidate", "candidate", {}, {"candidates/candidate/code.py": b"original"})
        (self.root / "candidates/candidate/code.py").write_bytes(b"modified")
        self.store.publish_pending()
        with self.assertRaisesRegex(ValueError, "修改或缺失"):
            self.store.verify_files()

    def test_missing_or_wrong_identity_database_is_not_silently_rebuilt(self):
        with self.assertRaisesRegex(ValueError, "身份"):
            Storage(self.root, "different", "fingerprint")
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "缺少"):
            Storage(empty, "experiment", "fingerprint")

    def test_lock_blocks_other_process_and_releases_without_deleting_lockfile(self):
        script = (
            f"import sys; sys.path.insert(0, {str(PROJECT)!r})\n"
            "from Evolve.storage import Storage\n"
            f"try:\n    Storage({str(self.root)!r}, 'experiment', 'fingerprint')\n"
            "except RuntimeError:\n    print('blocked')\nelse:\n    print('opened')\n"
        )
        command = [sys.executable, "-I", "-X", "utf8", "-c", script]
        with self.store.operation():
            result = subprocess.run(command, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), b"blocked")
        self.assertTrue((self.root / ".operation.lock").exists())
        result = subprocess.run(command, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b"opened")

    def test_hard_process_exit_after_commit_leaves_recoverable_exports(self):
        script = (
            f"import sys,os; sys.path.insert(0, {str(PROJECT)!r})\n"
            "from Evolve.storage import Storage\n"
            f"store=Storage({str(self.root)!r}, 'experiment', 'fingerprint')\n"
            "store.publish_pending=lambda: os._exit(23)\n"
            "store.put('candidate','hard_exit',{'state':'recorded'}, {'candidates/hard_exit/code.py':b'fixture'})\n"
        )
        process = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, timeout=5)
        self.assertEqual(process.returncode, 23, process.stderr)
        reopened = Storage(self.root, "experiment", "fingerprint")
        self.assertIsNotNone(reopened.get("candidate", "hard_exit"))
        reopened.publish_pending()
        reopened.verify_files()
        self.assertEqual((self.root / "candidates/hard_exit/code.py").read_bytes(), b"fixture")


if __name__ == "__main__":
    unittest.main()
