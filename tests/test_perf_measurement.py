import csv

from benchmarks.runtime_perf.instrument import RunRecorder, memory_sample
import benchmarks.runtime_perf.instrument as instrument
from benchmarks.runtime_perf.cases_runtime import _validate_p16_outcome
from benchmarks.runtime_perf.runner import _has_run_failures


def test_memory_failure_is_missing_not_zero(monkeypatch):
    monkeypatch.setattr(instrument._KERNEL32, "GetCurrentProcess", lambda: 1)
    monkeypatch.setattr(instrument._PSAPI, "GetProcessMemoryInfo", lambda *_args: 0)
    assert memory_sample() == (None, None)
    assert "failed" in instrument.MEMORY_SAMPLE_ERROR


def test_recorder_persists_each_sample(tmp_path):
    path = tmp_path / "raw.csv"
    recorder = RunRecorder("run", path)
    recorder.record(case_id="P03", repeat=1, wall_ms=12.5)
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["wall_ms"] == "12.5"


def test_csv_import_numeric_timeout_rows_summarize(tmp_path):
    recorder = RunRecorder("run", tmp_path / "raw.csv")
    recorder.record(case_id="P03", fixture_id="M", repeat="1", wall_ms="10.5",
                    python_working_set_peak="100")
    recorder.record(case_id="P03", fixture_id="M", repeat="2", status="timeout",
                    timeout_ms="120000", python_working_set_peak=None)
    assert recorder.rows[0]["wall_ms"] == 10.5
    summary = recorder.summarize()[0]
    assert summary["success"] == 1
    assert summary["peak_working_set_max"] == 100


def test_p16_validates_four_field_measured_outcome():
    _validate_p16_outcome(({"id": "prepared"}, 12.0, True, 1.5), "monitor")


def test_failure_status_survives_csv_merge_and_fails_run(tmp_path):
    raw = tmp_path / "worker.csv"
    worker = RunRecorder("worker", raw)
    worker.record(case_id="P16", fixture_id="M", repeat=1,
                  status="validation_failed", wall_ms=12.0)
    parent = RunRecorder("parent", tmp_path / "parent.csv")
    with raw.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            parent.record(**row)
    assert parent.summarize()[0]["failed"] == 1
    assert parent.summarize()[0]["sd_ms"] is None
    assert _has_run_failures({"P16": {"status": "sample_failure"}})
    assert not _has_run_failures({"P10": {"status": "skipped"}})
