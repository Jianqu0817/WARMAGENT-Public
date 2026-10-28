"""基准运行入口。

用法：
  PYTHONPATH=src .venv/Scripts/python.exe -m benchmarks.runtime_perf.runner \
      --cases P01,P03,P05,P06 --size M --tag first-round

只新增benchmarks/与reports/产物；不改业务代码、生产配置与实库。
"""

from __future__ import annotations

import argparse
import json
import sys
import subprocess
import csv
import time
import traceback
import shutil
from datetime import datetime
from pathlib import Path

from .instrument import RunRecorder, environment, write_json
from . import cases as core_cases
from . import cases_context
from . import cases_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASE_FUNCS = {"P01": core_cases.case_p01, "P03": core_cases.case_p03,
              "P05": core_cases.case_p05, "P06": core_cases.case_p06,
              "P08": cases_context.case_p08, "P09": cases_context.case_p09,
              "P02": cases_runtime.case_p02, "P10": cases_runtime.case_p10,
              "P11": cases_runtime.case_p11, "P15": cases_runtime.case_p15,
              "P16": cases_runtime.case_p16}


def _has_run_failures(case_notes: dict) -> bool:
    return any(note.get("status") not in ("ok", "skipped")
               for note in case_notes.values())


class CaseContext:
    def __init__(self, run_dir: Path, recorder: RunRecorder, size: str):
        self.run_dir = run_dir
        self.work_dir = run_dir / "work"
        self.fixtures_dir = run_dir / "fixtures"
        self.recorder = recorder
        self.size = size
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)

    def load_questions(self, fixture_dir: Path) -> dict:
        manifest = json.loads((fixture_dir / "fixture.json").read_text(encoding="utf-8"))
        return manifest["questions"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="P01,P03,P05,P06")
    parser.add_argument("--size", default="M", choices=["S", "M", "L"])
    parser.add_argument("--tag", default="first-round")
    parser.add_argument("--repeats", type=int, default=None,
                        help="覆盖每条件重复次数；方案标准5，冒烟轮可传1")
    parser.add_argument("--case-timeout", type=int, default=120,
                        help="每个case的实际worker超时秒数")
    parser.add_argument("--quick", action="store_true", help="只复验出错的关键条件")
    parser.add_argument("--p16-mode", choices=("both", "alone", "index"), default="both",
                        help="拆分聊天基线与索引竞争条件，分别受worker超时约束")
    parser.add_argument("--fixture-source", type=Path,
                        help="已有夹具目录，需包含fixture.json及archive/；仅复制到本轮目录")
    parser.add_argument("--worker-case", help=argparse.SUPPRESS)
    parser.add_argument("--worker-run-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats is not None:
        from . import instrument
        instrument.DEFAULT_REPEATS = args.repeats

    if args.worker_case:
        child_recorder = RunRecorder("worker", args.worker_output)
        ctx = CaseContext(args.worker_run_dir, child_recorder, args.size)
        ctx.quick = args.quick
        ctx.p16_mode = args.p16_mode
        CASE_FUNCS[args.worker_case](ctx)
        child_recorder.flush()
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"perf-{stamp}-{args.tag}-{args.size}"
    run_dir = PROJECT_ROOT / "var" / "benchmarks" / run_id
    report_dir = PROJECT_ROOT / "reports" / "perf" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    if args.fixture_source:
        metadata = json.loads((args.fixture_source / 'fixture.json').read_text(encoding='utf-8'))
        if metadata['size_label'] != args.size:
            parser.error('fixture size_label does not match --size')
        destination = run_dir / "fixtures" / args.size
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(args.fixture_source, destination, dirs_exist_ok=True)
    recorder = RunRecorder(run_id, report_dir / "raw.csv")

    print(f"[perf] run_id={run_id}", flush=True)
    selected = [name.strip() for name in args.cases.split(",") if name.strip()]
    unknown = [name for name in selected if name not in CASE_FUNCS]
    if unknown:
        print(f"[perf] unknown cases: {unknown}; available={list(CASE_FUNCS)}")
        return 2

    started = time.perf_counter()
    case_notes = {}
    for name in selected:
        ctx = CaseContext(run_dir, recorder, args.size)
        case_started = time.perf_counter()
        if name == "P10":
            case_notes[name] = {"status": "skipped",
                                "reason": "no real runtime tables in the synthetic fixture"}
            recorder.record(case_id=name, fixture_id=args.size, status="skipped",
                            error_kind=case_notes[name]["reason"])
            recorder.flush()
            recorder.write_summary_csv(report_dir / "summary.csv")
            continue
        try:
            worker_raw = report_dir / f"{name}.worker.csv"
            command = [sys.executable, "-m", "benchmarks.runtime_perf.runner",
                       "--cases", name, "--size", args.size, "--tag", args.tag,
                       "--worker-case", name, "--worker-run-dir", str(run_dir),
                       "--worker-output", str(worker_raw)]
            if args.repeats is not None:
                command.extend(["--repeats", str(args.repeats)])
            if args.quick:
                command.append("--quick")
            command.extend(["--p16-mode", args.p16_mode])
            subprocess.run(command, cwd=PROJECT_ROOT, timeout=args.case_timeout,
                           check=True)
            if worker_raw.exists():
                with worker_raw.open(newline="", encoding="utf-8") as stream:
                    for row in csv.DictReader(stream):
                        recorder.record(**row)
            case_notes[name] = {"status": "ok",
                                "seconds": round(time.perf_counter() - case_started, 1)}
        except subprocess.TimeoutExpired as error:
            worker_raw = report_dir / f"{name}.worker.csv"
            if worker_raw.exists():
                with worker_raw.open(newline="", encoding="utf-8") as stream:
                    for row in csv.DictReader(stream):
                        recorder.record(**row)
            case_notes[name] = {"status": "timeout", "timeout_seconds": error.timeout,
                                "seconds": round(time.perf_counter() - case_started, 1)}
            recorder.record(case_id=name, fixture_id=args.size, repeat=None,
                            status="timeout", timeout_ms=args.case_timeout * 1000,
                            error_kind=f"case worker exceeded {args.case_timeout}s")
        except subprocess.CalledProcessError as error:
            case_notes[name] = {"status": "error", "worker_exit": error.returncode,
                                "seconds": round(time.perf_counter() - case_started, 1)}
            worker_raw = report_dir / f"{name}.worker.csv"
            if worker_raw.exists():
                with worker_raw.open(newline="", encoding="utf-8") as stream:
                    for row in csv.DictReader(stream):
                        recorder.record(**row)
            recorder.record(case_id=name, fixture_id=args.size, status="error",
                            error_kind=f"case worker exit {error.returncode}")
        except Exception as error:  # noqa: BLE001 - 单case失败保留其余结果
            case_notes[name] = {"status": "error", "error": repr(error)[:300],
                                "traceback": traceback.format_exc()[-2000:],
                                "seconds": round(time.perf_counter() - case_started, 1)}
            print(f"[perf] case {name} failed: {error!r}", flush=True)
        print(f"[perf] {name} done in {case_notes[name]['seconds']}s "
              f"(rows={len(recorder.rows)})", flush=True)
        failed_samples = [row for row in recorder.rows
                          if row["case_id"] == name and row["status"] not in ("ok", "skipped")]
        if failed_samples and case_notes[name]["status"] == "ok":
            case_notes[name] = {"status": "sample_failure", "failed_rows": len(failed_samples),
                                "seconds": round(time.perf_counter() - case_started, 1)}
        # 每个case结束即落盘，进程崩溃不丢已完成数据
        recorder.flush()
        recorder.write_summary_csv(report_dir / "summary.csv")

    recorder.flush()
    summary_rows = recorder.write_summary_csv(report_dir / "summary.csv")
    notes = {"tag": args.tag, "size": args.size,
             "repeats": __import__('benchmarks.runtime_perf.instrument',
                                   fromlist=['DEFAULT_REPEATS']).DEFAULT_REPEATS,
             "cases": case_notes,
             "total_seconds": round(time.perf_counter() - started, 1),
             "repeat_policy": f"请求重复次数={__import__('benchmarks.runtime_perf.instrument', fromlist=['DEFAULT_REPEATS']).DEFAULT_REPEATS}；每条件实际次数见summary.csv；热条件预热不计分",
             "fake_boundaries": "假Gateway仅替代外部模型边界；归档/检索/文件/SQLite全部真实执行"}
    write_json(report_dir / "environment.json",
               environment(PROJECT_ROOT, run_dir, notes))
    manifest = _fixture_manifest(run_dir)
    write_json(report_dir / "fixture_manifest.json", manifest)
    _write_report(report_dir, run_id, summary_rows, case_notes)
    print(f"[perf] report -> {report_dir / 'report.md'}", flush=True)
    return 1 if _has_run_failures(case_notes) else 0


def _fixture_manifest(run_dir: Path) -> dict:
    entries = {}
    fixtures = run_dir / "fixtures"
    if fixtures.exists():
        for path in sorted(fixtures.rglob("*")):
            if path.is_file() and path.name == "fixture.json":
                meta = json.loads(path.read_text(encoding="utf-8"))
                entries[path.parent.name] = {
                    "size": meta.get("size_label"), "commits": meta.get("commit_count"),
                    "latest_records": meta.get("latest_records"), "seed": meta.get("seed"),
                    "content": "raw+event按配方生成，10%带interpretation旧修订"}
    return {"fixtures": entries,
            "files_note": "工作历史/文件/图片夹具按用例惰性生成于work/目录"}


def _write_report(report_dir: Path, run_id: str, summary_rows: list, case_notes: dict):
    lines = [f"# 性能验证首轮报告 · {run_id}", "",
             "实际重复次数见汇总列；raw.csv保留每条样本与失败状态。", ""]
    lines.append("## 用例状态")
    for name, note in case_notes.items():
        status = note.get("status")
        lines.append(f"- {name}: {status} ({note.get('seconds')}s)"
                     + (f" — {note.get('error') or note.get('reason') or note.get('failed_rows')}"
                        if status != "ok" else ""))
    lines.append("")
    lines.append("## 汇总")
    lines.append("| case | condition | cache | 成功/次数 | 平均ms | 中位ms | SD | min | max |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    ordered = sorted(summary_rows, key=lambda row: -(row["mean_ms"] or 0))
    for row in ordered:
        lines.append(
            f"| {row['case_id']} | {row['condition']} | {row['cache_state']} "
            f"| {row['success']}/{row['runs']} | {row['mean_ms']} | {row['median_ms']} "
            f"| {row['sd_ms']} | {row['min_ms']} | {row['max_ms']} |")
    lines.append("")
    lines.append("## 结论")
    lines.append("(待数据核对后填写；失败与超时条件如实列出，不以子集均值冒充完整均值。)")
    (report_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
