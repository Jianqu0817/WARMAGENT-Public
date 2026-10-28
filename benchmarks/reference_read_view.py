"""One verified, read-only archive view for a synchronous assembly pass.

Never retained across an await or a turn. The authoritative store validates
files on entry; consumers receive independent records, including old revisions.
"""
from copy import deepcopy

from myatri.storage import ConflictError
from myatri.storage.archive import ArchiveError, _safe_id


class ContextReadView:
    def __init__(self, store):
        self._source = store
        self.allowed = None
        self._manifests, self._records = store._load()
        self._snapshots = {}
        self._latest_cache = {}
        self._by_id_cache = {}
        self.refresh_on_get = False
        for name in ("root", "domain_path", "db_path", "agent_id", "user_id"):
            setattr(self, name, getattr(store, name))

    def snapshot(self):
        return self._manifests[-1]["commit_id"] if self._manifests else None

    def _at(self, snapshot):
        if snapshot is None:
            return self._manifests, self._records
        if snapshot not in self._snapshots:
            manifests, refs = [], set()
            for manifest in self._manifests:
                manifests.append(manifest)
                refs.update(entry["ref"] for entry in manifest["records"])
                if manifest["commit_id"] == snapshot:
                    break
            else:
                raise ArchiveError(f"Unknown snapshot in current isolation domain: {snapshot}")
            self._snapshots[snapshot] = manifests, {ref: self._records[ref] for ref in refs}
        return self._snapshots[snapshot]

    def _load(self, snapshot=None, *, force=False):
        if force:
            raise RuntimeError("Forced integrity checks require the authoritative archive")
        return deepcopy(self._at(snapshot))

    def _refresh(self):
        self._manifests, self._records = self._source._load()
        self._snapshots.clear()
        self._latest_cache.clear()
        self._by_id_cache.clear()

    def commit(self, records, idempotency_key, expected=None, **kwargs):
        """Keep writes authoritative while allowing core CAS reads on this view."""
        try:
            return self._source.commit(records, idempotency_key, expected=expected, **kwargs)
        except ConflictError:
            # The selector retries after a CAS conflict. Refresh its read set
            # before that retry, just as an authoritative store read would.
            self._refresh()
            raise

    def list_records(self, kind=None, snapshot=None):
        if snapshot not in self._latest_cache:
            latest = {}
            for record in self._at(snapshot)[1].values():
                if record.record_id not in latest or record.revision > latest[record.record_id].revision:
                    latest[record.record_id] = record
            self._latest_cache[snapshot] = tuple(sorted(latest.values(), key=lambda r: (r.created_at, r.record_id)))
        rows = (r for r in self._latest_cache[snapshot]
                if (kind is None or r.record_kind == kind)
                and (self.allowed is None or self.allowed(r)))
        return deepcopy(list(rows))

    def get_record(self, record_id, revision=None, snapshot=None):
        if self.refresh_on_get:
            # Activation uses get_record only after its optional model await.
            # Revalidate once at that boundary so revisions made during the
            # await invalidate the original read set before CAS.
            self.refresh_on_get = False
            self._refresh()
        _safe_id(record_id)
        if snapshot not in self._by_id_cache:
            by_id = {}
            for record in self._at(snapshot)[1].values():
                by_id.setdefault(record.record_id, []).append(record)
            self._by_id_cache[snapshot] = {
                key: tuple(sorted(value, key=lambda r: r.revision)) for key, value in by_id.items()
            }
        rows = [r for r in self._by_id_cache[snapshot].get(record_id, ())
                if revision is None or r.revision == revision]
        if not rows:
            raise KeyError(f"Record unavailable in current domain/snapshot: {record_id}@{revision}")
        record = max(rows, key=lambda r: r.revision)
        if self.allowed is not None and not self.allowed(record):
            raise KeyError("record outside channel visibility")
        return deepcopy(record)
