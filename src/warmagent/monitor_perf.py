"""Read saved offline measurements; opening the dashboard never runs benchmarks."""
import json
import math
from pathlib import Path


def _milliseconds(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def performance_reports(project):
    from .native_text import backend

    result = {"measurement": "saved_offline_benchmark", "text_backend": backend(),
        "record_backend": "pydantic-core Rust serializer", "reports": []}
    root = Path(project) / "reports/perf"
    for prefix, optimized, label in (
            ("native-text-", "cpp", "C++ 文本评分与摘要选择"),
            ("native-record-", "rust_serializer", "Rust 记录副本")):
        paths = sorted(root.glob(prefix + "*/summary.json"))
        if not paths:
            continue
        path = paths[-1]
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            environment = json.loads(path.with_name("environment.json").read_text(encoding="utf-8"))
            rows = []
            for row in summary:
                before = _milliseconds(row.get("python_ms"))
                after = _milliseconds(row.get(optimized + "_ms"))
                if before is None or after is None or before == 0:
                    continue
                raw = row.get("raw", {})
                rounds = len(raw["python"]) if "python" in raw else environment.get("rounds")
                rows.append({"case": str(row.get("case", ""))[:80],
                    "fixture": str(row.get("fixture", ""))[:40], "python_ms": before,
                    "optimized_ms": after, "ratio": after / before,
                    "rounds": rounds if type(rounds) is int and rounds > 0 else None})
            result["reports"].append({"name": label, "report": path.parent.name, "rows": rows})
        except (OSError, ValueError, TypeError, AttributeError):
            result["reports"].append({"name": label, "report": path.parent.name,
                "rows": [], "status": "unavailable"})
    return result
