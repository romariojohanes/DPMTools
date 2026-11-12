"""Low-overhead local resource monitoring and benchmark safety limits."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import threading
from ctypes import wintypes
from pathlib import Path
from time import monotonic, process_time

GIB = 1024 ** 3


def _directory_bytes(path: Path) -> int:
    total = 0
    try:
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _windows_memory_io():
    if os.name != 'nt':
        return None

    class MemoryCounters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class MemoryStatus(ctypes.Structure):
        _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in (
                'ullTotalPhys', 'ullAvailPhys', 'ullTotalPageFile', 'ullAvailPageFile',
                'ullTotalVirtual', 'ullAvailVirtual', 'ullAvailExtendedVirtual')]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IoCounters)]
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    handle = kernel.GetCurrentProcess()
    memory = MemoryCounters()
    memory.cb = ctypes.sizeof(memory)
    io = IoCounters()
    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
        return None
    kernel.GetProcessIoCounters(handle, ctypes.byref(io))
    kernel.GlobalMemoryStatusEx(ctypes.byref(status))
    return {
        'working_set_bytes': int(memory.WorkingSetSize),
        'peak_working_set_bytes': int(memory.PeakWorkingSetSize),
        'pagefile_bytes': int(memory.PagefileUsage),
        'read_bytes': int(io.ReadTransferCount),
        'write_bytes': int(io.WriteTransferCount),
        'available_memory_bytes': int(status.ullAvailPhys),
    }


class ResourceMonitor:
    """Samples the current process and interrupts DuckDB at a hard safety limit."""

    def __init__(self, run, cancel_event=None, *, append=False):
        self.run = run
        self.settings = run.settings
        self.stop_event = threading.Event()
        self.sample_lock = threading.Lock()
        self.thread = None
        self.samples = []
        self.elapsed_offset = 0.0
        target = self.run.directory / 'resource_samples.jsonl'
        if append and target.exists():
            try:
                self.samples = [json.loads(line) for line in target.read_text(encoding='utf-8').splitlines() if line]
                if self.samples:
                    self.elapsed_offset = float(self.samples[-1].get('elapsed_seconds', 0))
            except (OSError, ValueError, TypeError):
                self.samples = []
        self.stop_reason = None
        self.cancel_event = cancel_event
        self.started = monotonic()
        self.previous_wall = self.started
        self.previous_cpu = process_time()

    def _sample(self):
        with self.sample_lock:
            return self._sample_locked()

    def _sample_locked(self):
        now = monotonic()
        cpu = process_time()
        elapsed = now - self.previous_wall
        # Immediate safety checkpoints can be closer than the Windows process
        # CPU clock resolution. Do not turn that quantization into a false spike.
        cpu_percent = None if elapsed < 0.01 else max(
            0.0, (cpu - self.previous_cpu) / elapsed * 100 / max(os.cpu_count() or 1, 1))
        self.previous_wall, self.previous_cpu = now, cpu
        values = _windows_memory_io() or {}
        disk = shutil.disk_usage(self.run.temp_directory)
        values.update(
            elapsed_seconds=round(self.elapsed_offset + now - self.started, 3),
            cpu_percent=round(cpu_percent, 2) if cpu_percent is not None else None,
            temp_bytes=_directory_bytes(self.run.temp_directory),
            run_bytes=_directory_bytes(self.run.directory),
            temp_disk_free_bytes=disk.free,
            stage=self.run.current_stage,
        )
        run_disk = shutil.disk_usage(self.run.directory)
        values['run_disk_free_bytes'] = run_disk.free
        values['disk_free_bytes'] = min(disk.free, run_disk.free)
        previous = self.samples[-1] if self.samples else None
        if previous:
            delta = values['elapsed_seconds'] - previous.get('elapsed_seconds', 0)
            for key in ('read_bytes', 'write_bytes'):
                if delta >= 0.01 and values.get(key) is not None and previous.get(key) is not None:
                    values[key + '_per_second'] = max(0, round((values[key] - previous[key]) / delta, 2))
        return values

    def _limit_reason(self, sample):
        if sample.get('working_set_bytes', 0) > self.settings.process_memory_limit_gb * GIB:
            return 'PROCESS_MEMORY_LIMIT'
        available = sample.get('available_memory_bytes')
        if available is not None and available < self.settings.available_memory_floor_gb * GIB:
            return 'AVAILABLE_MEMORY_FLOOR'
        if sample['disk_free_bytes'] < self.settings.free_space_reserve_gb * GIB:
            return 'FREE_SPACE_RESERVE'
        maximum = self.settings.max_elapsed_seconds
        if maximum is not None and sample['elapsed_seconds'] > maximum:
            return 'ELAPSED_TIME_LIMIT'
        return None

    def checkpoint(self):
        """Sample immediately and make asynchronous limit/cancellation stops observable."""
        if self.stop_reason:
            raise RuntimeError(f'Resource monitor stopped the operation: {self.stop_reason}.')
        if self.cancel_event is not None and self.cancel_event.is_set():
            self.stop_reason = 'USER_CANCELLED'
            self.run.con.interrupt()
            raise RuntimeError('Operation cancelled by user.')
        sample = self._sample()
        self.samples.append(sample)
        reason = self._limit_reason(sample)
        if reason:
            self.stop_reason = reason
            self.run.con.interrupt()
            raise RuntimeError(f'Resource limit reached: {reason}.')
        return sample

    def _run(self):
        while not self.stop_event.wait(self.settings.monitor_interval_seconds):
            if self.cancel_event is not None and self.cancel_event.is_set():
                self.stop_reason = 'USER_CANCELLED'
                try:
                    self.run.con.interrupt()
                finally:
                    self.stop_event.set()
                return
            sample = self._sample()
            self.samples.append(sample)
            reason = self._limit_reason(sample)
            if reason:
                self.stop_reason = reason
                try:
                    self.run.con.interrupt()
                finally:
                    self.stop_event.set()

    def start(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            self.stop_reason = 'USER_CANCELLED'
            raise RuntimeError('Run cancelled before processing started.')
        try:
            self.checkpoint()
        except RuntimeError as exc:
            raise RuntimeError(f'Resource preflight failed: {exc}') from exc
        self.thread = threading.Thread(target=self._run, name='dpm-resource-monitor', daemon=True)
        self.thread.start()
        return self

    def finish(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=max(self.settings.monitor_interval_seconds * 2, 2))
        self.samples.append(self._sample())
        numeric = ('working_set_bytes', 'peak_working_set_bytes', 'pagefile_bytes', 'read_bytes',
                   'write_bytes', 'available_memory_bytes', 'temp_bytes', 'run_bytes', 'disk_free_bytes',
                   'temp_disk_free_bytes', 'run_disk_free_bytes', 'read_bytes_per_second',
                   'write_bytes_per_second', 'cpu_percent')
        summary = {'sample_count': len(self.samples), 'stop_reason': self.stop_reason}
        for key in numeric:
            found = [s[key] for s in self.samples if s.get(key) is not None]
            if found:
                summary[key + '_min'] = min(found)
                summary[key + '_max'] = max(found)
                summary[key + '_final'] = found[-1]
        self.run.meta['resource_summary'] = summary
        target = self.run.directory / 'resource_samples.jsonl'
        with target.open('w', encoding='utf-8') as handle:
            for sample in self.samples:
                handle.write(json.dumps(sample, separators=(',', ':')) + '\n')
        return summary
