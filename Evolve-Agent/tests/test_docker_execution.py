"""Docker 调度与传输的离线测试；使用协议替身，不声称验证了容器隔离。"""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from Evolve.contracts import Candidate, ExecutionLimits
from Evolve.docker_execution import DockerUnavailable, create_arguments, inspect_image, inspect_runtime, run_docker_process
from Evolve.loader import load_task
from Evolve.runner import run_candidate


IMAGE = "sha256:" + "a" * 64
PROJECT = Path(__file__).resolve().parents[1]
REAL_POPEN = subprocess.Popen
HEADER = "import json,sys,time\nprint(json.dumps({'protocol':'evolve-container-v1','python':sys.version,'platform':'test-double'}), flush=True)\npayload=json.loads(sys.stdin.buffer.readline())\n"


class DockerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolve-docker-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "container.log"
        self.output = self.root / "output.json"
        self.limits = replace(ExecutionLimits(), backend="docker", docker_image=IMAGE)
        self.commands = []
        self.process = None
        self.fixture = HEADER + "print(json.dumps(payload), end='', flush=True)\n"
        self.remove_fails = False
        self.create_fails = False

    def fake_command(self, arguments, timeout=10):
        self.commands.append(arguments)
        if arguments[0] == "info":
            return json.dumps({"OSType": "linux", "MemoryLimit": True, "SwapLimit": True,
                               "PidsLimit": True, "CpuCfsQuota": True,
                               "SecurityOptions": ["name=seccomp,profile=builtin"]}).encode()
        if arguments[:2] == ["image", "inspect"]:
            return json.dumps({"Id": IMAGE, "Os": "linux", "Architecture": "amd64"}).encode()
        if arguments[0] == "create":
            if self.create_fails:
                raise DockerUnavailable("offline create failure")
            return b"fake-container-id"
        if arguments[0] == "inspect":
            code = self.process.poll()
            return json.dumps({"Running": code is None, "ExitCode": code, "OOMKilled": False, "Error": ""}).encode()
        if arguments[0] == "rm":
            if self.remove_fails:
                raise DockerUnavailable("offline remove failure")
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            return b"removed"
        if arguments[0] == "ps":
            return b"fake-container-id" if self.remove_fails else b""
        raise AssertionError(arguments)

    def fake_popen(self, arguments, **kwargs):
        self.assertEqual(arguments[1:4], ["start", "--attach", "--interactive"])
        self.process = REAL_POPEN([sys.executable, "-I", "-u", "-X", "utf8", "-c", self.fixture], **kwargs)
        self.addCleanup(self.cleanup_process)
        return self.process

    def cleanup_process(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()

    def run_fixture(self, *, timeout=2):
        with patch("Evolve.docker_execution.docker_command", side_effect=self.fake_command), \
             patch("Evolve.docker_execution.shutil.which", return_value="fake-docker"), \
             patch("Evolve.docker_execution.subprocess.Popen", side_effect=self.fake_popen):
            return run_docker_process({"mode": "candidate", "code": "offline-example"},
                                      self.log, self.output, timeout, self.limits)

    def test_execution_requires_pinned_image_and_known_backend(self):
        for values in [
            {"backend": "unknown"}, {"backend": "docker", "docker_image": None},
            {"backend": "docker", "docker_image": "python:latest"},
            {"backend": "local-reviewed", "docker_image": IMAGE},
            {"startup_timeout_seconds": False},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ExecutionLimits(**values)

    def test_container_has_no_host_mounts_or_inherited_secrets(self):
        with patch.dict(os.environ, {"RELAY_API_KEY": "offline-sentinel"}):
            args = create_arguments("evolve-test", self.limits)
        for flag, value in [
            ("--network", "none"), ("--cap-drop", "ALL"), ("--user", "65534:65534"),
            ("--memory", "512m"), ("--memory-swap", "512m"), ("--pids-limit", "8"),
            ("--cpus", "1"), ("--log-driver", "none"), ("--pull", "never"),
        ]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--read-only", args)
        self.assertIn("no-new-privileges:true", args)
        self.assertIn("noexec,nosuid,nodev", args[args.index("--tmpfs") + 1])
        for forbidden in ["--mount", "--volume", "-v", "--privileged", "--env-file"]:
            self.assertNotIn(forbidden, args)
        self.assertNotIn("offline-sentinel", " ".join(args))
        self.assertNotIn(str(PROJECT), " ".join(args))

    def test_image_inspection_rejects_non_linux_or_mismatched_id(self):
        for info in [{"Id": IMAGE, "Os": "windows"}, {"Id": "sha256:" + "b" * 64, "Os": "linux"}]:
            with patch("Evolve.docker_execution.docker_command", return_value=json.dumps(info).encode()):
                with self.assertRaises(DockerUnavailable):
                    inspect_image(IMAGE)

    def test_daemon_without_cgroup_limits_is_rejected(self):
        info = {"OSType": "linux", "MemoryLimit": False, "SwapLimit": True,
                "PidsLimit": True, "CpuCfsQuota": True, "SecurityOptions": ["name=seccomp"]}
        with patch("Evolve.docker_execution.docker_command", return_value=json.dumps(info).encode()):
            with self.assertRaisesRegex(DockerUnavailable, "memorylimit"):
                inspect_runtime()

    def test_missing_runtime_never_falls_back_to_host_execution(self):
        task = load_task(PROJECT / "tasks/circle_packing", "circle_packing_26", 3)
        candidate = Candidate(task.task_id, task.initial_program.read_text(encoding="utf-8"))
        with patch("Evolve.docker_execution.shutil.which", return_value=None), \
             patch("Evolve.runner.run_process", side_effect=AssertionError("must not run locally")):
            result = run_candidate(task, candidate, artifact_dir=self.root / "artifacts", limits=self.limits)
        self.assertEqual(result.status, "error")
        self.assertIn("Docker CLI", result.feedback)
        record = json.loads((self.root / "artifacts/execution.json").read_text(encoding="utf-8"))
        self.assertFalse(record["filesystem_isolated"])
        self.assertNotIn("evaluator", record["phases"])

    def test_transport_keeps_output_and_logs_separate_and_removes_container(self):
        self.fixture = HEADER + "print('candidate-log', file=sys.stderr)\nprint(json.dumps(payload), end='', flush=True)\n"
        record = self.run_fixture()
        self.assertEqual(record["status"], "exited", record)
        self.assertEqual(json.loads(self.output.read_text())["code"], "offline-example")
        self.assertIn("candidate-log", self.log.read_text())
        self.assertEqual(record["returncode"], 0)
        self.assertTrue(record["cleanup_succeeded"])
        self.assertIsNotNone(record["runtime"])
        self.assertTrue(any(cmd[0] == "rm" for cmd in self.commands))

    def test_output_overflow_is_bounded_and_marked_even_after_fast_exit(self):
        self.limits = replace(self.limits, max_solution_bytes=1024)
        self.fixture = HEADER + "print('x' * 100000, end='', flush=True)\n"
        record = self.run_fixture()
        self.assertEqual(record["status"], "resource_limit", record)
        self.assertTrue(record["output_truncated"])
        self.assertEqual(self.output.stat().st_size, 1024)
        self.assertTrue(record["cleanup_succeeded"])

    def test_log_overflow_is_bounded(self):
        self.limits = replace(self.limits, max_log_bytes=1024)
        self.fixture = HEADER + "print('x' * 100000, file=sys.stderr, flush=True)\nprint('[]', flush=True)\n"
        record = self.run_fixture()
        self.assertEqual(record["status"], "resource_limit", record)
        self.assertTrue(record["log_truncated"])
        self.assertEqual(self.log.stat().st_size, 1024)

    def test_timeout_removes_daemon_container_instead_of_only_cli(self):
        self.fixture = HEADER + "time.sleep(30)\n"
        record = self.run_fixture(timeout=0.2)
        self.assertEqual(record["status"], "timeout", record)
        self.assertTrue(record["cleanup_succeeded"])
        self.assertTrue(any(cmd[:3] == ["rm", "--force", "--volumes"] for cmd in self.commands))

    def test_startup_timeout_does_not_send_candidate_and_cleans_up(self):
        self.limits = replace(self.limits, startup_timeout_seconds=0.2)
        self.fixture = "import time; time.sleep(30)"
        record = self.run_fixture()
        self.assertEqual(record["status"], "launch_error", record)
        self.assertIsNone(record["runtime"])
        self.assertTrue(record["cleanup_succeeded"])

    def test_malformed_handshake_is_rejected(self):
        self.fixture = "print('not-json', flush=True)"
        record = self.run_fixture()
        self.assertEqual(record["status"], "launch_error", record)
        self.assertTrue(record["cleanup_succeeded"])

    def test_create_failure_still_attempts_to_remove_uncertain_container(self):
        self.create_fails = True
        record = self.run_fixture()
        self.assertEqual(record["status"], "launch_error", record)
        self.assertTrue(any(cmd[0] == "rm" for cmd in self.commands))

    def test_cleanup_failure_cannot_report_success(self):
        self.remove_fails = True
        record = self.run_fixture()
        self.assertEqual(record["status"], "cleanup_error", record)
        self.assertFalse(record["cleanup_succeeded"])
        self.assertTrue(record["container_name"].startswith("evolve-"))

    def test_truncated_transport_is_saved_as_partial_and_never_evaluated(self):
        task = load_task(PROJECT / "tasks/circle_packing", "circle_packing_26", 3)
        candidate = Candidate(task.task_id, task.initial_program.read_text(encoding="utf-8"))
        payloads = []

        def fake_phase(payload, log, output, timeout, limits):
            payloads.append(payload)
            log.write_text("")
            output.write_bytes(b"x" * 16)
            return {"status": "resource_limit", "resource_limit": "output_bytes",
                    "returncode": 0, "runtime": {"protocol": "evolve-container-v1"}, "output_truncated": True}

        with patch("Evolve.runner.run_docker_process", side_effect=fake_phase):
            result = run_candidate(task, candidate, artifact_dir=self.root / "artifacts", limits=self.limits)
        self.assertEqual(result.status, "error")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(set(payloads[0]), {"mode", "code"})
        folder = self.root / "artifacts"
        self.assertTrue((folder / "solution.json.partial").exists())
        self.assertFalse((folder / "solution.json").exists())
        record = json.loads((folder / "execution.json").read_text(encoding="utf-8"))
        self.assertFalse(record["solution"]["complete"])
        self.assertIsNone(record["solution"]["original_bytes"])


if __name__ == "__main__":
    unittest.main()
