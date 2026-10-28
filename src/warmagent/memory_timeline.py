"""Read-only, source-linked timeline projections over an archive snapshot."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import OrderedDict
import json
from .context_read_view import ContextReadView
from .shared_runtime import MemoryVisibility


def _time(record):
    value = record.payload.get("occurred_at") if record.record_kind == "event" else None
    value = value or record.created_at
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


class MemoryTimeline:
    """Build recent and monthly source indexes; never writes or calls a model."""
    def __init__(self, store, allowed):
        self.store, self.allowed = store, allowed
        self._snapshot = object()
        self._index_cache = {}
        self._cache = OrderedDict()

    def _is_allowed(self, record, allowed=None):
        allowed = self.allowed if allowed is None else allowed
        method = getattr(allowed, "allowed", None)
        return bool(method(record) if callable(method) else allowed(record))

    def _load(self, scope):
        allowed_scope = getattr(self.allowed, "scope", scope)
        if allowed_scope != scope:
            raise ValueError("allowed visibility scope does not match requested scope")
        view = self.store if isinstance(self.store, ContextReadView) else ContextReadView(self.store)
        snapshot = view.snapshot()
        allowed = (MemoryVisibility(view, self.allowed.scope, self.allowed.private)
                   if isinstance(self.allowed, MemoryVisibility) else self.allowed)
        # Latest snapshot is checked every access, so newly published manifests invalidate projections.
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self._index_cache.clear()
            self._cache.clear()
        key = (scope, snapshot)
        if key not in self._index_cache:
            raw_records = view.list_records(snapshot=snapshot)
            visible = tuple(record for record in raw_records if self._is_allowed(record, allowed))
            self._index_cache[key] = (visible, {record.ref: record for record in visible})
            if len(self._index_cache) > 8:
                self._index_cache.pop(next(iter(self._index_cache)))
        records, by_ref = self._index_cache[key]
        return snapshot, records, by_ref, view, allowed

    def view(self, scope, now, budget_chars=4000, *, month_offset=0, recent_offset=0):
        if budget_chars < 1:
            raise ValueError("budget_chars must be positive")
        snapshot, records, by_ref, _view, _allowed = self._load(scope)
        observed = _time(now) if hasattr(now, "created_at") else now
        if not isinstance(observed, datetime):
            observed = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        observed = observed.replace(tzinfo=observed.tzinfo or timezone.utc).astimezone(timezone.utc)
        # Cache at minute precision; all records are evaluated against that same rounded watermark.
        observed = observed.replace(second=0, microsecond=0)
        budget_bytes = min(int(budget_chars), 5500)
        key = (snapshot, scope, observed.isoformat(), budget_bytes, month_offset, recent_offset)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        cutoff = observed - timedelta(days=7)
        recent, months, missing = [], {}, []
        for record in records:
            if record.record_kind not in {"event", "cognitive_run", "interpretation", "reflection"}:
                continue
            when = _time(record)
            if when is None:
                continue
            month = when.strftime("%Y-%m")
            bucket = months.setdefault(month, {"record_refs": [], "source_refs": set(), "count": 0})
            bucket["record_refs"].append(record.ref)
            bucket["source_refs"].update(record.source_refs)
            bucket["count"] += 1
            if when >= cutoff and when <= observed:
                linked = [ref for ref in record.source_refs if ref in by_ref]
                missing.extend(ref for ref in record.source_refs if ref not in by_ref)
                # The event text is a derived index label; source refs remain the evidence.
                item = {"observed_at": when.isoformat(), "record_ref": record.ref,
                    "kind": record.record_kind, "summary": record.body_markdown[:500],
                    "summary_is_original_fact": False, "source_refs": linked[:4],
                    "source_refs_total": len(linked)}
                recent.append(item)
        recent.sort(key=lambda item: (item["observed_at"], item["record_ref"]), reverse=True)
        recent_total = len(recent)
        recent_start = max(0, recent_offset)
        recent_page = recent[recent_start:recent_start + 20]
        ordered_months = sorted(months.items(), reverse=True)
        month_total = len(ordered_months)
        month_page = ordered_months[max(0, month_offset):max(0, month_offset) + 12]
        month_index = {month: {"count": data["count"],
            "record_refs": sorted(data["record_refs"])[:3], "record_refs_total": len(data["record_refs"]),
            "source_refs": sorted(data["source_refs"])[:3], "source_refs_total": len(data["source_refs"]),
            "refs_truncated": len(data["record_refs"]) > 3 or len(data["source_refs"]) > 3}
            for month, data in month_page}
        missing_preview = sorted(set(missing))[:8]
        omitted_preview = [item["record_ref"] for item in recent_page[20:28]]
        result = {"scope": scope, "observed_at": observed.isoformat(), "source_watermark": snapshot,
            "recent": [], "recent_total": recent_total, "recent_offset": max(0, recent_offset),
            "recent_next_offset": max(0, recent_offset) + 20 if recent_total > max(0, recent_offset) + 20 else None,
            "months": {}, "month_total": month_total, "month_offset": max(0, month_offset),
            "month_next_offset": max(0, month_offset) + 12 if month_total > max(0, month_offset) + 12 else None,
            "missing": missing_preview, "missing_total": len(set(missing)),
            "limitations": ["摘要字段来自派生记忆记录，不是原始事实；请用source_refs回查原文。",
                            "时间线以分钟为缓存粒度；月度引用为预览，可按月expand。"],
            "omitted_refs": omitted_preview, "omitted_total": max(0, recent_total - recent_start)}
        # Add complete rows only while the complete serialized result fits the transport budget.
        consumed_recent = 0
        for relative_index, item in enumerate(recent_page):
            candidate = {**result, "recent": [*result["recent"], item]}
            if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > budget_bytes:
                result["omitted_refs"] = [*result["omitted_refs"][:7], item["record_ref"]]
                result["omitted_total"] += 1
                break
            result["recent"].append(item)
            consumed_recent = relative_index + 1
        result["recent_next_offset"] = (recent_start + consumed_recent
            if recent_start + consumed_recent < recent_total else None)
        result["omitted_total"] = max(0, recent_total - (recent_start + consumed_recent))
        consumed_months = 0
        for month, data in month_index.items():
            candidate = {**result, "months": {**result["months"], month: data}}
            if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= budget_bytes:
                result["months"][month] = data
                consumed_months += 1
            else:
                break
        result["month_next_offset"] = (max(0, month_offset) + consumed_months
            if max(0, month_offset) + consumed_months < month_total else None)
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > 64:
            self._cache.popitem(last=False)
        return result

    def expand(self, month, scope=None, budget_chars=12000, *, offset=0, max_items=20,
               source_offset=0, source_limit=8):
        scope = scope or getattr(self.allowed, "scope", "default")
        snapshot, records, _, view, allowed = self._load(scope)
        if budget_chars < 1:
            raise ValueError("budget_chars must be positive")
        if max_items < 1 or source_limit < 1:
            raise ValueError("page sizes must be positive")
        max_items = min(max_items, 20)
        source_limit = min(source_limit, 16)
        budget_bytes = min(int(budget_chars), 5500)
        selected_all = [record for record in records if _time(record) and _time(record).strftime("%Y-%m") == month
                    and record.record_kind in {"event", "cognitive_run", "interpretation", "reflection"}]
        selected_all.sort(key=lambda row: (row.created_at, row.ref))
        selected = selected_all[max(0, offset):max(0, offset) + max_items]
        refs = list(dict.fromkeys(ref for record in selected for ref in record.source_refs))
        # Include exact linked revisions, respecting ACL on the concrete revision.
        sources, missing = [], []
        refs_total = len(refs)
        source_page = refs[max(0, source_offset):max(0, source_offset) + source_limit]
        for ref in source_page:
            try:
                record_id, revision = ref.rsplit("@", 1)
                source = view.get_record(record_id, int(revision), snapshot=snapshot)
            except (KeyError, ValueError):
                missing.append(ref)
                continue
            if not self._is_allowed(source, allowed):
                continue
            row = {"ref": source.ref, "kind": source.record_kind,
                   "body_markdown": source.body_markdown, "channel_source": source.payload.get("channel_source")}
            candidate = {"sources": [*sources, row]}
            if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > budget_bytes:
                row = {"ref": source.ref, "kind": source.record_kind,
                       "excerpt": source.body_markdown[:240], "truncated": True,
                       "read_record_ref": source.ref}
            sources.append(row)
        result = {"scope": scope, "month": month, "observed_at": datetime.now(timezone.utc).isoformat(),
            "source_watermark": snapshot, "records": [{"ref": row.ref, "kind": row.record_kind,
                "summary": row.body_markdown[:300], "summary_is_original_fact": False,
            "source_refs": row.source_refs[:4], "source_refs_total": len(row.source_refs)}
                for row in selected],
            "record_total": len(selected_all), "offset": max(0, offset),
            "next_offset": max(0, offset) + len(selected) if max(0, offset) + len(selected) < len(selected_all) else None,
            "sources": sources, "source_total": refs_total, "source_offset": max(0, source_offset),
            "source_next_offset": max(0, source_offset) + source_limit if max(0, source_offset) + source_limit < refs_total else None,
            "missing": sorted(set(missing))[:8], "missing_total": len(set(missing)),
            "limitations": ["索引摘要不是原始事实；超长原文返回excerpt及read_record_ref。"]}
        while len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > budget_bytes:
            if result["records"]:
                result["records"].pop()
            elif result["sources"]:
                result["sources"].pop()
            else:
                break
        result["next_offset"] = (max(0, offset) + len(result["records"])
            if max(0, offset) + len(result["records"]) < len(selected_all) else None)
        result["source_next_offset"] = (max(0, source_offset) + len(result["sources"])
            if max(0, source_offset) + len(result["sources"]) < refs_total else None)
        result["source_next_offset"] = (max(0, source_offset) + len(source_page)
            if max(0, source_offset) + len(source_page) < refs_total else None)
        return result
