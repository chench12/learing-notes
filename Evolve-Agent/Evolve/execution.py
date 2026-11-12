"""受控的本地进程运行：环境白名单、进程树清理和有界日志。"""

import os
from pathlib import Path
import subprocess
import sys
import threading
from time import perf_counter

from Evolve.process_group import ProcessGroup


def workspace_bytes(workdir):
    total = 0
    for path in workdir.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            # 候选可能在采样期间删除自己的临时文件。
            continue
    return total


def output_bytes(output_path):
    try:
        return output_path.stat().st_size if output_path.is_file() else 0
    except FileNotFoundError:
        return 0


def candidate_environment(workdir):
    # 不继承 API Key、代理凭据等调用端变量。
    environment = {
        name: os.environ[name]
        for name in ("SystemRoot", "WINDIR", "COMSPEC", "SYSTEMDRIVE")
        if name in os.environ
    }
    paths = [str(Path(sys.executable).parent), str(Path(sys.prefix) / "Library" / "bin")]
    if "SystemRoot" in environment:
        paths.append(str(Path(environment["SystemRoot"]) / "System32"))
    environment.update({
        "PATH": os.pathsep.join(paths),
        "TEMP": str(workdir),
        "TMP": str(workdir),
        "LANG": "C.UTF-8",
    })
    return environment


def run_process(command, workdir, log_path, output_path, timeout, limits):
    started = perf_counter()
    group = None
    process = None
    reader = None
    log_exceeded = threading.Event()
    reader_failed = threading.Event()
    record = {
        "status": "launch_error",
        "returncode": None,
        "peak_memory_bytes": None,
        "memory_limit_enforced": False,
        "process_limit_enforced": False,
        "log_truncated": False,
        "resource_limit": None,
    }

    def collect_log():
        saved = 0
        try:
            with log_path.open("wb") as log:
                while chunk := process.stdout.read(65536):
                    remaining = max(0, limits.max_log_bytes - saved)
                    log.write(chunk[:remaining])
                    saved += min(remaining, len(chunk))
                    if len(chunk) > remaining:
                        log_exceeded.set()
        except Exception:
            reader_failed.set()

    try:
        log_path.touch(exist_ok=False)
        group = ProcessGroup(limits.memory_mb, limits.max_processes)
        process = subprocess.Popen(
            command,
            cwd=workdir,
            env=candidate_environment(workdir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=os.name != "nt",
        )
        group.attach(process)
        record["memory_limit_enforced"] = group.memory_limit_enforced
        record["process_limit_enforced"] = group.process_limit_enforced
        reader = threading.Thread(target=collect_log, daemon=True)
        reader.start()
        # worker 等待握手，先关联资源限制，再开始运行候选/评测器。
        process.stdin.write(b"START\n")
        process.stdin.close()
        record["status"] = "exited"
        while process.poll() is None:
            if perf_counter() - started >= timeout:
                record["status"] = "timeout"
                break
            reason = None
            if log_exceeded.is_set():
                reason = "log_bytes"
            elif reader_failed.is_set():
                reason = "log_capture_error"
            elif output_bytes(output_path) > limits.max_solution_bytes:
                reason = "output_bytes"
            else:
                size = workspace_bytes(workdir)
                if size > limits.max_workdir_bytes:
                    reason = "workdir_bytes"
            if reason:
                record["status"] = "resource_limit"
                record["resource_limit"] = reason
                break
            # 使用事件等待可在日志达到上限时提前唤醒。
            log_exceeded.wait(0.01)
    except Exception as exc:
        record["status"] = "launch_error"
        record["error_type"] = type(exc).__name__
    finally:
        if group is not None:
            record["peak_memory_bytes"] = group.peak_memory_bytes()
            group.close()
        if process is not None:
            # 正常退出也关闭进程组，清理候选遗留的后代。
            if process.poll() is None:
                process.kill()
            process.wait()
            record["returncode"] = process.returncode
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if reader is not None:
                reader.join(timeout=2)
            process.stdout.close()
        record["elapsed_seconds"] = perf_counter() - started
        record["log_truncated"] = log_exceeded.is_set()
    # 快速退出的程序也不能绕过日志或结果大小检查。
    if record["status"] == "exited":
        if log_exceeded.is_set() or reader_failed.is_set():
            record["status"] = "resource_limit"
            record["resource_limit"] = "log_bytes" if log_exceeded.is_set() else "log_capture_error"
        elif output_bytes(output_path) > limits.max_solution_bytes:
            record["status"] = "resource_limit"
            record["resource_limit"] = "output_bytes"
        elif workspace_bytes(workdir) > limits.max_workdir_bytes:
            record["status"] = "resource_limit"
            record["resource_limit"] = "workdir_bytes"
    return record
