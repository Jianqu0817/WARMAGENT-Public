"""Read-only token and budget-reservation audit for WARMAGENT SQLite snapshots."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    db.execute("PRAGMA query_only=ON")
    return db


def _tables(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    with closing(_connect(path)) as db:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _number(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _stamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else None
        except ValueError:
            return None
    return None


def purpose_group(purpose: Any) -> str:
    """Broad reporting bucket; preserve the original purpose beside this value."""
    if not isinstance(purpose, str) or not purpose:
        return "unknown"
    if purpose == "conversation":
        return "chat"
    if purpose == "proactive_speak":
        return "proactive_chat"
    if purpose == "autonomous_activity":
        return "autonomous_activity"
    if purpose == "computer_report":
        return "tool_report"
    if purpose.startswith("computer_"):
        return "tool_execution"
    if purpose.endswith("_schema_retry") or purpose.endswith("_retry"):
        return "retry"
    if purpose in {"group_short_memory", "emotion_batch", "emotion", "group_reply_guard",
                   "group_memory_support_review", "behavior_attribution", "A1", "A1_batch",
                   "A1_support", "context_compaction", "temporary_state_activation", "temporary_state",
                   "reflection", "persona", "interpretation", "experience", "theme", "association"}:
        return "memory_maintenance"
    return "unknown"


def _read_usage(path: Path) -> list[dict[str, Any]]:
    if "usage" not in _tables(path):
        return []
    out = []
    with closing(_connect(path)) as db:
        for (raw,) in db.execute("SELECT payload FROM usage"):
            try:
                row = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def _read_reservations(path: Path) -> list[dict[str, Any]]:
    if "budget_reservations" not in _tables(path):
        return []
    out = []
    with closing(_connect(path)) as db:
        for row in db.execute("SELECT id,scope,day,reserved_tokens,charged_tokens,status,expires_at,created_at "
                              "FROM budget_reservations"):
            out.append(dict(zip(("id", "scope", "day", "reserved_tokens", "charged_tokens", "status",
                                 "expires_at", "created_at"), row)))
    return out


def _source_paths(manifest: Path | None, state_root: Path | None) -> tuple[list[Path], list[Path]]:
    if manifest:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        root = manifest.parent
        snapshot = root / "snapshot"
        dbs = [snapshot / item["relative"] for item in data.get("snapshots", [])]
        return dbs, dbs
    if state_root is None:
        raise ValueError("provide --manifest or --state-root")
    # A state root is read-only; discover only known SQLite state files.
    dbs = sorted(state_root.rglob("*.sqlite3"))
    return dbs, dbs


def build_report(*, manifest: Path | None = None, state_root: Path | None = None) -> dict[str, Any]:
    usage_paths, reservation_paths = _source_paths(manifest, state_root)
    usages: dict[tuple[str, str], dict[str, Any]] = {}
    no_trace: list[dict[str, Any]] = []
    for path in usage_paths:
        for row in _read_usage(path):
            trace = row.get("trace_id")
            reservation_id = str(row.get("budget_reservation_id") or "")
            # A trace can legitimately cover retries/follow-ups with distinct reservation IDs.
            # Deduplicate only a copied usage event with the same trace AND reservation.
            key = (str(trace), reservation_id) if trace else ("", reservation_id)
            if not trace:
                no_trace.append(row)
            elif key not in usages:
                usages[key] = row
            else:
                # Prefer the row with more provider-reported fields; never sum copies.
                old = usages[key]
                if len(row.get("usage", {})) > len(old.get("usage", {})):
                    usages[key] = row
    rows = list(usages.values()) + no_trace
    usage_by_reservation: dict[str, dict[str, Any]] = {}
    for row in rows:
        rid = str(row.get("budget_reservation_id") or "")
        if rid and rid not in usage_by_reservation:
            usage_by_reservation[rid] = row

    reservations: dict[str, dict[str, Any]] = {}
    reservation_rows_seen = 0
    for path in reservation_paths:
        for row in _read_reservations(path):
            reservation_rows_seen += 1
            rid = str(row.get("id") or "")
            if rid and rid not in reservations:
                reservations[rid] = row
            elif rid and row.get("status") == "settled":
                reservations[rid] = row

    reservation_by_id = reservations
    seen_res_ids: set[str] = set()
    token_totals = Counter()
    purpose_totals: dict[str, Counter] = defaultdict(Counter)
    hourly: dict[str, Counter] = defaultdict(Counter)
    daily: dict[str, Counter] = defaultdict(Counter)
    model_totals: dict[str, Counter] = defaultdict(Counter)
    traces: dict[str, Counter] = defaultdict(Counter)
    unknown_reasons = Counter()
    largest_calls: list[dict[str, Any]] = []
    for row in rows:
        purpose = row.get("purpose") if isinstance(row.get("purpose"), str) else "unknown"
        group = purpose_group(purpose)
        model = str(row.get("model") or "unknown")
        reservation_id = str(row.get("budget_reservation_id") or "")
        reservation = reservation_by_id.get(reservation_id)
        stamp = _stamp(row.get("recorded_at")) or (_stamp(reservation.get("created_at")) if reservation else None)
        time_source = "recorded_at" if _stamp(row.get("recorded_at")) else ("reservation_created_at" if stamp else "unknown")
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        prompt, completion = _number(usage.get("prompt_tokens")), _number(usage.get("completion_tokens"))
        cache = _number(usage.get("prompt_cache_hit_tokens"))
        if cache is None:
            details = usage.get("prompt_tokens_details")
            cache = _number(details.get("cached_tokens")) if isinstance(details, dict) else None
        confirmed = row.get("status") == "received" and prompt is not None and completion is not None
        metric = Counter(requests=1, confirmed_requests=int(confirmed), failed_or_unknown_requests=int(not confirmed),
                         prompt_tokens=prompt or 0, completion_tokens=completion or 0,
                         reported_total_tokens=(prompt or 0) + (completion or 0),
                         cached_input_tokens=cache or 0, cache_reported=int(cache is not None),
                         undated_requests=int(stamp is None), reservation_time_fallback=int(time_source == "reservation_created_at"))
        purpose_totals[f"{group}:{purpose}"].update(metric)
        model_totals[model].update(metric)
        if stamp:
            local = stamp.astimezone(BEIJING)
            day, hour = local.date().isoformat(), f"{local.hour:02d}"
            daily[day].update(metric)
            hourly[f"{day} {hour}:00"].update(metric)
        token_totals.update(metric)
        trace_hash = hashlib.sha256(str(row.get("trace_id") or "no-trace").encode()).hexdigest()[:12]
        traces[trace_hash].update(metric)
        if prompt is not None and completion is not None:
            largest_calls.append({"trace_hash": trace_hash, "purpose": purpose, "purpose_group": group,
                "model": model, "beijing_time": stamp.astimezone(BEIJING).isoformat(timespec="seconds") if stamp else None,
                "prompt_tokens": prompt, "completion_tokens": completion,
                "reported_total_tokens": prompt + completion, "status": row.get("status")})
        if not confirmed:
            unknown_reasons[str(row.get("status") or "missing_status") + ("_missing_tokens" if prompt is None or completion is None else "")]+=1
        if reservation_id:
            seen_res_ids.add(reservation_id)

    ledger = Counter(settled_reservations=0, settled_charged_tokens=0, reserved_reservations=0,
                     reserved_tokens=0, unknown_reservations=0, unknown_tokens=0,
                     unmatched_reservations=0)
    ledger["duplicate_reservation_ids"] = reservation_rows_seen - len(reservations)
    ledger_groups: dict[str, Counter] = defaultdict(Counter)
    budget_days: dict[str, Counter] = defaultdict(Counter)
    budget_hours: dict[str, Counter] = defaultdict(Counter)
    budget_purpose_models: dict[str, Counter] = defaultdict(Counter)
    for rid, row in reservations.items():
        status = row.get("status")
        amount = _number(row.get("charged_tokens"))
        reserve = _number(row.get("reserved_tokens")) or 0
        group = str(row.get("scope") or "unknown")
        usage_row = usage_by_reservation.get(rid, {})
        purpose = usage_row.get("purpose") if isinstance(usage_row.get("purpose"), str) else "unknown"
        model = str(usage_row.get("model") or "unknown")
        stamp = _stamp(usage_row.get("recorded_at")) or _stamp(row.get("created_at"))
        attribution = Counter(reservations=1, settled_charged_tokens=0, reserved_tokens=0, unknown_tokens=0)
        if rid not in seen_res_ids:
            ledger["unmatched_reservations"] += 1
        if status == "settled":
            ledger["settled_reservations"] += 1
            if amount is not None:
                ledger["settled_charged_tokens"] += amount
                ledger_groups[group]["settled_charged_tokens"] += amount
                attribution["settled_charged_tokens"] = amount
        elif status == "reserved":
            ledger["reserved_reservations"] += 1
            ledger["reserved_tokens"] += reserve
            ledger_groups[group]["reserved_tokens"] += reserve
            attribution["reserved_tokens"] = reserve
        else:
            ledger["unknown_reservations"] += 1
            # Ledger retains charged_tokens for unknown rows; report as conservative exposure only.
            conservative = amount if amount is not None else reserve
            ledger["unknown_tokens"] += conservative
            ledger_groups[group]["unknown_tokens"] += conservative
            attribution["unknown_tokens"] = conservative
        budget_purpose_models[f"{purpose}:{model}"].update(attribution)
        if stamp:
            local = stamp.astimezone(BEIJING)
            budget_days[local.date().isoformat()].update(attribution)
            budget_hours[f"{local.date().isoformat()} {local.hour:02d}:00"].update(attribution)

    return {
        "generated_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "source": str(manifest or state_root),
        "scope": "metadata and token counts only; no prompt/completion text read",
        "usage": {"unique_usage_events": len(rows), "deduplicated_trace_reservation_events": len(usages),
                  "prompt_tokens_reported": token_totals["prompt_tokens"],
                  "completion_tokens_reported": token_totals["completion_tokens"],
                  "cached_input_tokens_reported": token_totals["cached_input_tokens"],
                  "cache_detail_requests": token_totals["cache_reported"],
                  "requests": token_totals["requests"], "confirmed_requests": token_totals["confirmed_requests"],
                  "failed_or_unknown_requests": token_totals["failed_or_unknown_requests"],
                  "undated_requests": token_totals["undated_requests"],
                  "reservation_timestamp_fallbacks": token_totals["reservation_time_fallback"],
                  "unknown_status_counts": dict(unknown_reasons)},
        "budget_ledger": {"note": "budget ledger accounting only; not a supplier invoice or actual monetary charge",
                          **dict(ledger), "by_scope": {k: dict(v) for k, v in ledger_groups.items()},
                          "by_beijing_day": {k: dict(v) for k, v in sorted(budget_days.items())},
                          "by_beijing_hour": {k: dict(v) for k, v in sorted(budget_hours.items())},
                          "by_purpose_model": {k: dict(v) for k, v in sorted(budget_purpose_models.items())}},
        "by_beijing_day": {k: dict(v) for k, v in sorted(daily.items())},
        "by_beijing_hour": {k: dict(v) for k, v in sorted(hourly.items())},
        "largest_reported_calls": sorted(largest_calls, key=lambda x: x["reported_total_tokens"], reverse=True)[:10],
        "configured_cost_estimate": {"available": False, "note": "No nonzero, attributable price estimate was emitted; currency and supplier billing are unknown."},
        "by_purpose": {k: dict(v) for k, v in sorted(purpose_totals.items())},
        "by_model": {k: dict(v) for k, v in sorted(model_totals.items())},
        "trace_hashes": {k: dict(v) for k, v in traces.items()},
    }


def markdown_report(report: dict[str, Any]) -> str:
    u, b = report["usage"], report["budget_ledger"]
    lines = ["# 本机账单归因审计", "", f"- 生成时间：{report['generated_at']}",
             "- 时间口径：北京时间（Asia/Shanghai）；对有 reservation_created_at 的旧记录以该时间补齐小时归类。",
             "- 隐私：只导出统计字段及 trace 哈希，不含聊天内容、真实账号或 trace 原值。", "",
             "## 已报告用量", "",
             f"去重后 usage 事件：{u['unique_usage_events']}；确认请求：{u['confirmed_requests']}；失败或未知：{u['failed_or_unknown_requests']}。",
             f"Reported prompt/completion/cache tokens：{u['prompt_tokens_reported']} / {u['completion_tokens_reported']} / {u['cached_input_tokens_reported']}（cache 字段有记录的请求 {u['cache_detail_requests']}）。",
             f"缺记录时间 {u['undated_requests']} 条；其中用预约创建时间归类 {u['reservation_timestamp_fallbacks']} 条。未知状态：`{json.dumps(u['unknown_status_counts'], ensure_ascii=False)}`。", "",
             "## 预算账本（不是供应商账单）", "",
             f"Settled charged_tokens：{b['settled_charged_tokens']}（{b['settled_reservations']} 笔）；仍 reserved：{b['reserved_tokens']}（{b['reserved_reservations']} 笔）；unknown 保守暴露：{b['unknown_tokens']}（{b['unknown_reservations']} 笔）。未匹配 usage 的预约：{b['unmatched_reservations']} 笔。",
             "预算数据亦在 JSON 中按北京时间日/小时、关联 purpose 与模型分组；未关联项标记 unknown。它们是本地账本口径，不能当作供应商实际扣费；无可归因的非零价格信息，因此不估算元或美元。", "",
             "## 北京时间每日用量", "", "| 日期 | 请求 | Prompt | Completion | Cache hit |", "|---|---:|---:|---:|---:|"]
    for day, v in report["by_beijing_day"].items():
        lines.append(f"| {day} | {v['requests']} | {v['prompt_tokens']} | {v['completion_tokens']} | {v['cached_input_tokens']} |")
    lines += ["", "## 分组用量（Purpose 原值保留）", "", "| 归因组:purpose | 请求 | Prompt | Completion | P+C 占比 |", "|---|---:|---:|---:|---:|"]
    for name, v in report["by_purpose"].items():
        total = v.get("reported_total_tokens", 0)
        grand_total = u["prompt_tokens_reported"] + u["completion_tokens_reported"]
        share = f"{total / grand_total:.1%}" if grand_total else "—"
        lines.append(f"| `{name}` | {v['requests']} | {v['prompt_tokens']} | {v['completion_tokens']} | {share} |")
    lines += ["", "## 按小时", "", "小时按北京时间自然小时分桶，JSON 覆盖所有发生调用的小时；以下列出高峰窗口 09:00–12:00 与 14:00–17:00（左闭右开）：", "",
              "| 北京时间小时 | 请求 | Prompt | Completion |", "|---|---:|---:|---:|"]
    for hour in ("09", "10", "11", "14", "15", "16"):
        parts = [v for k, v in report["by_beijing_hour"].items() if k.endswith(f" {hour}:00")]
        if parts:
            vals = {key: sum(p.get(key, 0) for p in parts) for key in ("requests", "prompt_tokens", "completion_tokens")}
            lines.append(f"| {hour}:00 | {vals['requests']} | {vals['prompt_tokens']} | {vals['completion_tokens']} |")
    grand_total = u["prompt_tokens_reported"] + u["completion_tokens_reported"]
    lines += ["", "## 最大单次 reported token 调用", "", "按 prompt + completion 排序（不是货币成本）：", "",
              "| 时间（北京时间） | Purpose | Model | P+C | 占总 P+C | Trace 哈希 |", "|---|---|---|---:|---:|---|"]
    for call in report["largest_reported_calls"]:
        share = f"{call['reported_total_tokens'] / grand_total:.2%}" if grand_total else "—"
        lines.append(f"| {call['beijing_time'] or '未知'} | `{call['purpose']}` | `{call['model']}` | {call['reported_total_tokens']} | {share} | `{call['trace_hash']}` |")
    lines += ["", "## 模型", "", "| Model | 请求 | Prompt | Completion |", "|---|---:|---:|---:|"]
    for name, v in report["by_model"].items():
        lines.append(f"| `{name}` | {v['requests']} | {v['prompt_tokens']} | {v['completion_tokens']} |")
    lines += ["", "## 边界", "", "Usage 记录缺 `recorded_at` 时，以匹配 reservation 的 `created_at` 归时；没有有效时间则保留为未定时。跨数据库快照各自一致，但不具备跨库原子性。Usage 事件按 trace 与 reservation 联合去重，因此同 trace 下不同 reservation 的重试仍分别计数；重复 reservation ID 只计一次。Purpose 只能反映实际记录值；未知值单独保留，不能从聊天正文推断。" , ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path, help="诊断快照 manifest.json")
    source.add_argument("--state-root", type=Path, help="只读状态根目录；扫描 SQLite 文件")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args(argv)
    report = build_report(manifest=args.manifest, state_root=args.state_root)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    if args.md_out:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(markdown_report(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
