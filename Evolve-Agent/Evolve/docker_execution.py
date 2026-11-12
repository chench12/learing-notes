"""Docker CLI 后端；无宿主挂载，无网络，不安装或拉取任何镜像。

每阶段独立容器，输入/输出通过有界管道传输。失败不回退本地执行。
"""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
from time import perf_counter
from uuid import uuid4


class DockerUnavailable(RuntimeError):
    pass


def docker_command(arguments, timeout=10):
    executable = shutil.which("docker")
    if executable is None:
        raise DockerUnavailable("未找到 Docker CLI；请先配置 Docker Desktop 的 Linux 容器后端")
    try:
        process = subprocess.run(
            [executable, *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerUnavailable(f"Docker 命令不可用：{type(exc).__name__}") from None
    if process.returncode:
        # daemon 错误可能包含本机地址/配置，不保存原始诊断体。
        raise DockerUnavailable(f"Docker {arguments[0]} 失败（退出码 {process.returncode}）；请检查 daemon 和本地镜像")
    return process.stdout


def inspect_image(reference):
    """只检查本地镜像。允许检查命令用 tag；执行配置必须使用返回的 ID。"""
    value = json.loads(docker_command(["image", "inspect", reference, "--format", "{{json .}}"], timeout=10))
    if value.get("Os") != "linux":
        raise DockerUnavailable("只支持 Linux 容器镜像；请切换 Docker Desktop 到 Linux containers")
    if value.get("Config", {}).get("Volumes"):
        raise DockerUnavailable("执行镜像不能声明 VOLUME，以免创建不受工作目录配额约束的匿名卷")
    if not isinstance(value.get("Id"), str) or not value["Id"].startswith("sha256:"):
        raise DockerUnavailable("Docker 未返回可固定的镜像 ID")
    if reference.startswith("sha256:") and value["Id"] != reference:
        raise DockerUnavailable("本地镜像 ID 与实验配置不一致")
    return {"id": value["Id"], "os": value["Os"], "architecture": value.get("Architecture")}


def inspect_runtime():
    info = json.loads(docker_command(["info", "--format", "{{json .}}"], timeout=10))
    fields = {key.lower(): value for key, value in info.items()}
    if fields.get("ostype") != "linux":
        raise DockerUnavailable("Docker daemon 必须运行 Linux 容器")
    required = ["memorylimit", "swaplimit", "pidslimit", "cpucfsquota"]
    missing = [name for name in required if fields.get(name) is not True]
    if missing:
        raise DockerUnavailable("Docker daemon 缺少资源限制支持：" + ", ".join(missing))
    if not any("seccomp" in item for item in fields.get("securityoptions", [])):
        raise DockerUnavailable("Docker daemon 未启用默认 seccomp 支持")
    return {
        "server_version": fields.get("serverversion"), "os_type": "linux",
        "cgroup_version": fields.get("cgroupversion"),
        "cgroup_driver": fields.get("cgroupdriver"),
        "resource_support": {name: True for name in required},
        "seccomp_available": True,
    }


def create_arguments(name, limits):
    script = Path(__file__).with_name("container_worker.py").read_text(encoding="utf-8")
    return [
        "create", "--name", name, "--pull", "never", "--interactive",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "65534:65534",
        "--pids-limit", str(limits.max_processes),
        "--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m",
        "--cpus", "1", "--ulimit", "nofile=64:64", "--shm-size", "1m", "--log-driver", "none",
        "--workdir", "/work", "--tmpfs",
        f"/work:rw,noexec,nosuid,nodev,size={limits.max_workdir_bytes},mode=1777",
        "--env", "TMPDIR=/work", "--env", "TMP=/work", "--env", "TEMP=/work",
        "--env", "LANG=C.UTF-8", "--entrypoint", "python3",
        limits.docker_image, "-I", "-u", "-X", "utf8", "-c", script,
    ]


def run_docker_process(payload, log_path, output_path, timeout, limits):
    started = perf_counter()
    name = "evolve-" + uuid4().hex
    process = None
    threads = []
    ready = threading.Event()
    output_finished = threading.Event()
    exceeded = threading.Event()
    io_failed = threading.Event()
    record = {
        "status": "launch_error", "returncode": None, "peak_memory_bytes": None,
        "memory_limit_enforced": False, "process_limit_enforced": False,
        "log_truncated": False, "output_truncated": False, "resource_limit": None,
        "container_name": name, "cleanup_succeeded": False,
        "runtime": None, "execution_seconds": None,
        "cli_returncode": None,
    }
    timing = {}
    create_attempted = False

    def collect_output():
        try:
            header = process.stdout.readline(8193)
            if len(header) > 8192:
                raise ValueError("容器握手过长")
            runtime = json.loads(header)
            if runtime.get("protocol") != "evolve-container-v1":
                raise ValueError("容器握手协议错误")
            record["runtime"] = runtime
            timing["ready_at"] = perf_counter()
            ready.set()
            with output_path.open("wb") as stream:
                saved = 0
                while chunk := process.stdout.read(65536):
                    remaining = max(0, limits.max_solution_bytes - saved)
                    stream.write(chunk[:remaining])
                    saved += min(remaining, len(chunk))
                    if len(chunk) > remaining:
                        record["output_truncated"] = True
                        record["resource_limit"] = "output_bytes"
                        exceeded.set()
        except Exception:
            io_failed.set()
        finally:
            output_finished.set()

    def collect_log():
        try:
            with log_path.open("wb") as stream:
                saved = 0
                while chunk := process.stderr.read(65536):
                    remaining = max(0, limits.max_log_bytes - saved)
                    stream.write(chunk[:remaining])
                    saved += min(remaining, len(chunk))
                    if len(chunk) > remaining:
                        record["log_truncated"] = True
                        record["resource_limit"] = "log_bytes"
                        exceeded.set()
        except Exception:
            io_failed.set()

    def send_input():
        try:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            remaining = memoryview(data)
            while remaining:
                count = process.stdin.write(remaining)
                if not count:
                    raise OSError("容器输入管道已关闭")
                remaining = remaining[count:]
            process.stdin.close()
        except (OSError, ValueError):
            io_failed.set()

    try:
        log_path.touch(exist_ok=False)
        record["daemon"] = inspect_runtime()
        record["image"] = inspect_image(limits.docker_image)
        create_attempted = True
        docker_command(create_arguments(name, limits), timeout=limits.startup_timeout_seconds)
        record["memory_limit_enforced"] = True
        record["process_limit_enforced"] = True
        process = subprocess.Popen(
            [shutil.which("docker"), "start", "--attach", "--interactive", name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        for function in (collect_output, collect_log):
            thread = threading.Thread(target=function, daemon=True)
            threads.append(thread)
            thread.start()
        startup = perf_counter()
        while not ready.wait(0.01):
            if io_failed.is_set() or output_finished.is_set() or process.poll() is not None:
                raise DockerUnavailable("容器未完成启动握手；请查看本阶段日志")
            if perf_counter() - startup >= limits.startup_timeout_seconds:
                raise DockerUnavailable("容器启动握手超时")
        sender = threading.Thread(target=send_input, daemon=True)
        threads.append(sender)
        sender.start()
        record["status"] = "exited"
        while process.poll() is None:
            if exceeded.is_set():
                record["status"] = "resource_limit"
                break
            if io_failed.is_set():
                record["status"] = "transport_error"
                break
            if perf_counter() - timing["ready_at"] >= timeout:
                record["status"] = "timeout"
                break
            exceeded.wait(0.01)
        record["execution_seconds"] = perf_counter() - timing["ready_at"]
        # 在移除前保留 daemon 记录，尤其是 OOMKilled；不凭退出码猜测。
        state = json.loads(docker_command(["inspect", "--format", "{{json .State}}", name]))
        record["container_state"] = {key: state.get(key) for key in ("Running", "ExitCode", "OOMKilled", "Error")}
        record["returncode"] = state.get("ExitCode") if not state.get("Running") else None
        record["cli_returncode"] = process.poll()
        if record["status"] == "exited" and (state.get("Running") or process.returncode != record["returncode"]):
            record["status"] = "transport_error"
    except Exception as exc:
        record["status"] = "launch_error"
        record["error_type"] = type(exc).__name__
        record["error"] = str(exc) if isinstance(exc, DockerUnavailable) else type(exc).__name__
    finally:
        # Docker CLI 终止不会终止 daemon 中的容器，必须显式移除自己的 UUID 容器。
        if not create_attempted:
            record["cleanup_succeeded"] = True
        else:
            try:
                docker_command(["rm", "--force", "--volumes", name])
                record["cleanup_succeeded"] = True
            except DockerUnavailable:
                # create 失败时也可能已经创建成功；检查同名容器是否确实不存在。
                try:
                    existing = docker_command(["ps", "--all", "--quiet", "--filter", f"name=^/{name}$"])
                    record["cleanup_succeeded"] = not existing.strip()
                except DockerUnavailable:
                    pass
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for thread in threads:
                thread.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        if not record["cleanup_succeeded"] and record["status"] != "launch_error":
            record["status"] = "cleanup_error"
        if record["status"] == "exited":
            if exceeded.is_set():
                record["status"] = "resource_limit"
            elif io_failed.is_set():
                record["status"] = "transport_error"
        record["elapsed_seconds"] = perf_counter() - started
        if output_path.is_file():
            # 流已被截断时文件大小仍等于上限，必须额外标记。
            record["saved_output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return record
