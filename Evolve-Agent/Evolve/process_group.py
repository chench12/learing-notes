"""管理执行进程及其后代。Windows 使用 Job Object，POSIX 使用进程组。"""

import os
import signal


class ProcessGroup:
    def __init__(self, memory_mb, max_processes):
        self.handle = None
        self.process = None
        self.memory_limit_enforced = False
        self.process_limit_enforced = False
        if os.name != "nt":
            return

        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in [
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            ]]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.ctypes = ctypes
        self.limit_type = ExtendedLimits
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.api.QueryInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL

        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        # KILL_ON_JOB_CLOSE | JOB_MEMORY | ACTIVE_PROCESS。
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x200 | 0x8
        limits.BasicLimitInformation.ActiveProcessLimit = max_processes
        limits.JobMemoryLimit = memory_mb * 1024 * 1024
        if not self.api.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error
        self.memory_limit_enforced = True
        self.process_limit_enforced = True

    def attach(self, process):
        self.process = process
        if os.name == "nt":
            # Popen 在 Windows 持有目标进程句柄，支持嵌套 Job 的系统可直接关联。
            if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise self.ctypes.WinError(self.ctypes.get_last_error())

    def peak_memory_bytes(self):
        if os.name != "nt" or self.handle is None:
            return None
        info = self.limit_type()
        if not self.api.QueryInformationJobObject(
            self.handle, 9, self.ctypes.byref(info), self.ctypes.sizeof(info), None,
        ):
            return None
        return info.PeakJobMemoryUsed

    def close(self):
        if os.name == "nt":
            if self.handle is not None:
                self.api.CloseHandle(self.handle)
                self.handle = None
        elif self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
