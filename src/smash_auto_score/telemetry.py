"""Small process counters without a monitoring service dependency."""

import ctypes
import os
import sys
import time


def process_rss_bytes() -> int | None:
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        current_process = ctypes.windll.kernel32.GetCurrentProcess
        current_process.restype = wintypes.HANDLE
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        process = current_process()
        if get_memory(process, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
        return None
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value * (1 if sys.platform == "darwin" else 1024))
    except (ImportError, OSError):
        return None


class ProcessSampler:
    def __init__(self):
        self.wall = time.monotonic()
        self.cpu = time.process_time()
        self.cpu_percent = 0.0

    def sample(self) -> dict:
        wall, cpu = time.monotonic(), time.process_time()
        delta = max(.001, wall - self.wall)
        self.cpu_percent = max(0.0, (cpu - self.cpu) / delta * 100)
        self.wall, self.cpu = wall, cpu
        rss = process_rss_bytes()
        return {"cpu_percent_one_core": round(self.cpu_percent, 1),
                "ram_mb": round(rss / 1048576, 1) if rss is not None else None,
                "logical_cpus": os.cpu_count()}
