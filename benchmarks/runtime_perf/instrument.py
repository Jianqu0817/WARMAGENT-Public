"""计时、内存采样、计数与CSV记录；本模块只包装真实函数，不替代执行。"""

from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import platform
import statistics
import time
from contextlib import contextmanager
from pathlib import Path


class ProcessMemory(ctypes.Structure):
    _fields_ = [("cb", ctypes.wintypes.DWORD), ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


_PSAPI = ctypes.WinDLL("psapi.dll", use_last_error=True)
_KERNEL32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
_KERNEL32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
_KERNEL32.GetCurrentProcess.argtypes = []
_PSAPI.GetProcessMemoryInfo.argtypes = [ctypes.wintypes.HANDLE,
                                        ctypes.POINTER(ProcessMemory),
                                        ctypes.wintypes.DWORD]
_PSAPI.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
MEMORY_SAMPLE_ERROR = ""


def memory_sample() -> tuple[int | None, int | None]:
    """返回 (WorkingSetBytes, PrivateBytes)，采样失败时返回None。"""
    global MEMORY_SAMPLE_ERROR
    handle = _KERNEL32.GetCurrentProcess()
    info = ProcessMemory()
    info.cb = ctypes.sizeof(ProcessMemory)
    if not _PSAPI.GetProcessMemoryInfo(handle, ctypes.byref(info), info.cb):
        MEMORY_SAMPLE_ERROR = f"GetProcessMemoryInfo failed: {ctypes.get_last_error()}"
        return (None, None)
    MEMORY_SAMPLE_ERROR = ""
    return (int(info.WorkingSetSize), int(info.PrivateUsage))


class Counters:
    """实例方法级计数包装：继续执行原函数，只在外部累计次数。"""

    def __init__(self):
        self.values: dict[str, int] = {}
        self._originals: list[tuple[object, str, object]] = []

    def bump(self, key: str, amount: int = 1):
        self.values[key] = self.values.get(key, 0) + amount

    def wrap(self, obj, method: str, key: str):
        original = getattr(obj, method)
        counters = self

        def wrapped(*args, **kwargs):
            counters.bump(key)
            return original(*args, **kwargs)

        setattr(obj, method, wrapped)
        self._originals.append((obj, method, original))

    def restore(self):
        for obj, method, original in self._originals:
            setattr(obj, method, original)
        self._originals.clear()

    def snapshot(self) -> dict[str, int]:
        return dict(self.values)


@contextmanager
def measure(*, sample_interval: float = 0.1):
    """墙钟+CPU+内存峰值采样。短操作(<采样间隔)的峰值只代表起止值,在报告中注明。"""
    result = {"wall_ms": None, "cpu_ms": None, "python_working_set_start": None,
              "python_working_set_peak": None, "private_bytes_peak": None}
    import threading

    stop = threading.Event()
    peaks = {"ws": None, "pb": None}

    def sampler():
        while not stop.is_set():
            ws, pb = memory_sample()
            if ws is not None:
                peaks["ws"] = max(peaks["ws"] or 0, ws)
            if pb is not None:
                peaks["pb"] = max(peaks["pb"] or 0, pb)
            stop.wait(sample_interval)

    ws0, pb0 = memory_sample()
    result["python_working_set_start"] = ws0
    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()
    wall0 = time.perf_counter_ns()
    cpu0 = time.process_time_ns()
    try:
        yield result
    finally:
        wall1 = time.perf_counter_ns()
        cpu1 = time.process_time_ns()
        stop.set()
        thread.join(timeout=sample_interval * 3)
        ws1, pb1 = memory_sample()
        for key, values in (("ws", (ws0, ws1)), ("pb", (pb0, pb1))):
            valid = [v for v in values if v is not None]
            if valid:
                peaks[key] = max([peaks[key] or 0, *valid])
        result["wall_ms"] = (wall1 - wall0) / 1e6
        result["cpu_ms"] = (cpu1 - cpu0) / 1e6
        result["python_working_set_peak"] = peaks["ws"]
        result["private_bytes_peak"] = peaks["pb"]
        result["memory_sample_error"] = MEMORY_SAMPLE_ERROR or None


RAW_FIELDS = ["run_id", "case_id", "condition", "fixture_id", "snapshot", "scope_label",
              "principal_label", "cache_state", "config_hash", "repeat", "status", "wall_ms",
              "cpu_ms", "validation_ms", "python_working_set_start", "python_working_set_peak",
              "child_working_set_peak", "private_bytes_peak", "memory_sample_error", "event_loop_max_drift_ms", "maintenance_overlap", "archive_load_calls", "snapshot_calls", "records_read",
              "bytes_read", "index_rebuild_calls", "embedding_calls", "sqlite_busy_count",
              "model_calls", "tool_calls", "input_json_bytes", "estimated_tokens", "result_count",
              "timeout_ms", "error_kind", "note"]


class RunRecorder:
    def __init__(self, run_id: str, raw_path: Path):
        self.run_id = run_id
        self.rows: list[dict] = []
        self._raw_path = raw_path

    def record(self, **row):
        base = {field: None for field in RAW_FIELDS}
        base.update({"run_id": self.run_id, "status": "ok", "error_kind": ""})
        base.update({key: value for key, value in row.items() if key in RAW_FIELDS})
        integer_fields = {"repeat", "python_working_set_start", "python_working_set_peak",
                          "child_working_set_peak", "private_bytes_peak", "archive_load_calls",
                          "snapshot_calls", "records_read", "bytes_read", "index_rebuild_calls",
                          "embedding_calls", "sqlite_busy_count", "model_calls", "tool_calls",
                          "input_json_bytes", "estimated_tokens", "result_count", "timeout_ms"}
        float_fields = {"wall_ms", "cpu_ms", "validation_ms", "event_loop_max_drift_ms"}
        for field in integer_fields | float_fields:
            value = base[field]
            if value not in (None, ""):
                base[field] = float(value) if field in float_fields else int(value)
        if isinstance(base["maintenance_overlap"], str):
            base["maintenance_overlap"] = base["maintenance_overlap"].lower() == "true"
        self.rows.append(base)
        self.flush()

    def flush(self):
        self._raw_path.parent.mkdir(parents=True, exist_ok=True)
        with self._raw_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=RAW_FIELDS)
            writer.writeheader()
            for row in self.rows:
                writer.writerow(row)

    def summarize(self) -> list[dict]:
        groups: dict[tuple, list[dict]] = {}
        for row in self.rows:
            key = (row["case_id"], row["condition"], row["fixture_id"], row["cache_state"])
            groups.setdefault(key, []).append(row)
        summary = []
        for (case_id, condition, fixture_id, cache_state), rows in groups.items():
            ok = [r for r in rows if r["status"] == "ok"]
            walls = [float(r["wall_ms"]) for r in ok]
            entry = {"case_id": case_id, "condition": condition, "fixture_id": fixture_id,
                     "cache_state": cache_state, "runs": len(rows), "success": len(ok),
                     "failed": len(rows) - len(ok),
                     "mean_ms": None, "median_ms": None, "sd_ms": None,
                     "min_ms": None, "max_ms": None, "raw_walls": [round(w, 3) for w in walls]}
            if len(walls) == len(rows) and walls:
                entry["mean_ms"] = round(statistics.fmean(walls), 3)
                entry["median_ms"] = round(statistics.median(walls), 3)
                entry["sd_ms"] = round(statistics.stdev(walls), 3) if len(walls) > 1 else None
                entry["min_ms"] = round(min(walls), 3)
                entry["max_ms"] = round(max(walls), 3)
            entry["peak_working_set_max"] = max((r["python_working_set_peak"] or 0) for r in rows)
            summary.append(entry)
        return summary

    def write_summary_csv(self, path: Path):
        rows = self.summarize()
        fields = ["case_id", "condition", "fixture_id", "cache_state", "runs", "success",
                  "mean_ms", "median_ms", "sd_ms", "min_ms", "max_ms"]
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return rows


DEFAULT_REPEATS = 5  # 方案标准为5；用户可显式降为1做冒烟轮(n=1无统计意义)


def timed_repeats(recorder: RunRecorder, *, case_id: str, condition: str, fixture_id: str,
                  cache_state: str, scope_label: str, principal_label: str, snapshot_label: str,
                  config_hash: str, repeats: int | None = None, warmup: int = 0, timeout_ms: float = 60000.0,
                  prepare=None, run=None, validate=None, counters_from=None, extras=None,
                  measured_run=False):
    """统一5次重复执行协议。prepare()建立初态,run()执行被测操作,validate()做正确性断言。

    warmup>0时先执行不计分预热(热条件)。冷条件由prepare提供独立进程/对象初态。
    全部重复保留,失败值不剔除。
    """
    repeats = DEFAULT_REPEATS if repeats is None else repeats
    if warmup:
        for _ in range(warmup):
            run(prepare() if prepare else None)
    for repeat in range(1, repeats + 1):
        prepare_result = prepare() if prepare else None
        counters = counters_from(prepare_result) if counters_from else None
        row = {"case_id": case_id, "condition": condition, "fixture_id": fixture_id,
               "cache_state": cache_state, "scope_label": scope_label,
               "principal_label": principal_label, "snapshot": snapshot_label,
               "config_hash": config_hash, "repeat": repeat, "timeout_ms": timeout_ms,
               "validation_ms": 0.0}
        row.update(extras or {})
        status, error_kind, validation_ms = "ok", "", 0.0
        try:
            if measured_run:
                # 用例在自身事件循环内用measure()计时，返回(outcome, metrics)
                outcome, metrics = run(prepare_result) if prepare else run()
            else:
                with measure() as metrics:
                    outcome = run(prepare_result) if prepare else run()
            row.update({k: round(v, 3) if isinstance(v, float) else v
                        for k, v in metrics.items()})
            validation0 = time.perf_counter_ns()
            if validate:
                validate(outcome, prepare_result)
            validation_ms = (time.perf_counter_ns() - validation0) / 1e6
        except AssertionError as error:
            status = "validation_failed"
            error_kind = type(error).__name__ + ":" + str(error)[:120]
            row.setdefault("wall_ms", None)
        except Exception as error:  # noqa: BLE001 - 记录失败类别,保留样本
            status = "error"
            error_kind = type(error).__name__ + ":" + str(error)[:120]
            row.setdefault("wall_ms", None)
        row["status"] = status
        row["error_kind"] = error_kind
        row["validation_ms"] = round(validation_ms, 3)
        if counters:
            for key, value in counters.snapshot().items():
                row[key] = value
            counters.restore()
        recorder.record(**row)


def record_precomputed(recorder: RunRecorder, *, case_id: str, condition: str,
                       fixture_id: str, cache_state: str, scope_label: str,
                       principal_label: str, snapshot_label: str, config_hash: str,
                       repeat: int, metrics: dict, outcome=None, validate=None,
                       extras: dict | None = None, timeout_ms: float = 60000.0,
                       status: str = "ok", error_kind: str = ""):
    """记录用例在自身事件循环内用measure()预采集的重复样本。"""
    row = {"case_id": case_id, "condition": condition, "fixture_id": fixture_id,
           "cache_state": cache_state, "scope_label": scope_label,
           "principal_label": principal_label, "snapshot": snapshot_label,
           "config_hash": config_hash, "repeat": repeat, "timeout_ms": timeout_ms,
           "validation_ms": 0.0}
    row.update(extras or {})
    row.update({key: (round(value, 3) if isinstance(value, float) else value)
                for key, value in metrics.items()})
    if status == "ok" and validate is not None:
        try:
            validate(outcome)
        except AssertionError as error:
            status, error_kind = "validation_failed", type(error).__name__ + ":" + str(error)[:120]
        except Exception as error:  # noqa: BLE001
            status, error_kind = "error", type(error).__name__ + ":" + str(error)[:120]
    row["status"] = status
    row["error_kind"] = error_kind
    recorder.record(**row)


def environment(project_root: Path, run_dir: Path, notes: dict) -> dict:
    def file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    watched = ["src/warmagent/group_memory.py", "src/warmagent/context.py",
               "src/warmagent/context_read_view.py", "src/warmagent/memory_timeline.py",
               "src/warmagent/tool_context.py", "src/warmagent/artifacts.py",
               "src/warmagent/background_jobs.py", "src/warmagent/document_tools.py",
               "src/warmagent/monitor.py", "src/warmagent/bridge.py",
               "src/warmagent/activities.py", "src/warmagent/runtime_inbox.py",
               "src/myatri/storage/archive.py", "src/myatri/memory/index.py",
               "src/myatri/retrieval/service.py"]
    code = {rel: file_hash(project_root / rel) for rel in watched
            if (project_root / rel).exists()}
    import sys
    disk = None
    for candidate in ("D:\\", "C:\\"):
        try:
            free = shutil_disk_free(candidate)
            disk = {"path": candidate, "free_gb": round(free / 1024 ** 3, 1)}
            break
        except Exception:  # noqa: BLE001
            continue
    cpu_info = platform.processor() or "unknown"
    return {"python": sys.version, "platform": platform.platform(), "processor": cpu_info,
            "machine_cores": os.cpu_count(), "clock": "time.perf_counter_ns",
            "cpu_clock": "time.process_time_ns (python process only)",
            "memory_metric": "WorkingSet / PrivateBytes(PagefileUsage) via GetProcessMemoryInfo",
            "sample_interval_ms": 100, "code_hashes": code, "disk": disk,
            "timeout_policy": {"normal_repeat_ms": 60000, "index_repeat_ms": 180000},
            "concurrent_load_note": "生产WARMAGENT/微信/reader保持运行,仅记录存在,不停止",
            "notes": notes, "run_dir": str(run_dir)}


def shutil_disk_free(path: str) -> int:
    import shutil
    _, free, _ = shutil.disk_usage(path)
    return free


def write_json(path: Path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
