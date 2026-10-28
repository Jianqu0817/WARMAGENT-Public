import json
from copy import deepcopy
import unittest
from types import SimpleNamespace

from myatri.contracts import Record
from myatri.persona.activation import ActivationSelector
from myatri.storage import ConflictError
from warmagent.context import GroupContextBuilder
from warmagent.context_read_view import ContextReadView


def state_record(number, revision=1, status="open"):
    return Record(record_id=f"state_{number}", revision=revision, record_kind="temporary_state",
                  created_at='2026-10-25T00:00:00+00:00',
                  agent_id="agent", user_id="user", author_kind="system",
                  source_refs=["raw_evidence@1"],
                  body_markdown=f"state {number}", payload={"status": status, "scope": "test"})


class CountingStore:
    agent_id = "agent"
    user_id = "user"
    root = "synthetic"
    domain_path = "synthetic/domain"
    db_path = "synthetic/projection.sqlite3"

    def __init__(self, count=5):
        self.records = {r.ref: r for r in (state_record(i) for i in range(count))}
        self.loads = 0
        self.manifests = [{"commit_id": "snap-1", "records": [{"ref": ref} for ref in self.records]}]
        self.fail_commit_once = False

    def _load(self, snapshot=None, *, force=False):
        self.loads += 1
        return deepcopy(self.manifests), deepcopy(self.records)

    def snapshot(self):
        return self.manifests[-1]["commit_id"]

    def list_records(self, kind=None, snapshot=None):
        view = ContextReadView(self)
        return view.list_records(kind, snapshot)

    def get_record(self, record_id, revision=None, snapshot=None):
        self._load(snapshot)
        rows = [r for r in self.records.values() if r.record_id == record_id
                and (revision is None or r.revision == revision)]
        if not rows:
            raise KeyError(record_id)
        return max(rows, key=lambda r: r.revision)

    def close_state(self, record_id):
        current = self.get_record(record_id)
        updated = current.model_copy(update={"revision": current.revision + 1,
            "payload": {**current.payload, "status": "closed"}})
        self.records[updated.ref] = updated
        self.manifests.append({"commit_id": "snap-2", "records": [{"ref": ref} for ref in self.records]})

    def commit(self, records, idempotency_key, expected=None, **kwargs):
        if self.fail_commit_once:
            self.fail_commit_once = False
            raise ConflictError("synthetic CAS conflict")
        for record in records:
            expected_revision = (expected or {}).get(record.record_id)
            current = [r for r in self.records.values() if r.record_id == record.record_id]
            actual = max((r.revision for r in current), default=0)
            if expected_revision is not None and actual != expected_revision:
                raise ConflictError("synthetic stale read")
            self.records[record.ref] = record
        self.manifests.append({"commit_id": f"snap-{len(self.manifests) + 1}",
                               "records": [{"ref": ref} for ref in self.records]})
        return self.snapshot()


class CountingIndex:
    def __init__(self):
        self.queries = 0

    def query(self, *args, **kwargs):
        self.queries += 1
        return []


class ClosingGateway:
    def __init__(self, store):
        self.store = store

    async def generate(self, request):
        self.store.close_state("state_0")
        return SimpleNamespace(provider="fixture", model="test", tool_calls=[],
            content=json.dumps({"decision": "select", "selected": [
                {"ref": "state_0@1", "reason": "fixture selection"}], "reason": "fixture"}))


class ContextLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_activation_verification_uses_one_archive_refresh_instead_of_one_per_state(self):
        store = CountingStore(12)
        index = CountingIndex()
        original = ActivationSelector(store, index, llm_enabled=False)
        original_selected, original_activation = await original.aselect_active_entries(
            "test", snapshot="snap-1", persist=False)
        original_loads = store.loads

        store = CountingStore(12)
        builder = GroupContextBuilder(store, None, index=CountingIndex(), options={"llm_activation": False})
        view_selected, view_activation = await builder.selector.aselect_active_entries(
            "test", snapshot="snap-1", persist=False)

        self.assertEqual(original_loads, 14)
        self.assertEqual(store.loads, 2)
        self.assertEqual([r.ref for r in view_selected], [r.ref for r in original_selected])
        self.assertEqual(view_activation.model_dump(), original_activation.model_dump())

    async def test_state_closed_during_model_await_is_removed_after_view_refresh(self):
        store = CountingStore(2)
        builder = GroupContextBuilder(store, None, index=CountingIndex(), gateway=ClosingGateway(store),
                                      options={"llm_activation": True})
        selected, activation = await builder.selector.aselect_active_entries(
            "test", snapshot="snap-1", persist=False)

        self.assertNotIn("state_0@1", [record.ref for record in selected])
        self.assertIn("snapshot_candidates_changed_after_await", " ".join(activation.limitations))
        self.assertEqual(store.get_record("state_0").payload["status"], "closed")

    async def test_activation_cas_conflict_refreshes_view_and_retries_authoritative_commit(self):
        store = CountingStore(2)
        store.fail_commit_once = True
        builder = GroupContextBuilder(store, None, index=CountingIndex(), options={"llm_activation": False})
        _selected, activation = await builder.selector.aselect_active_entries(
            "test", snapshot="snap-1", persist=True)

        traces = [r for r in store.records.values() if r.record_kind == "activation"]
        self.assertEqual(len(traces), 1)
        self.assertIn("activation_trace_CAS_conflict", " ".join(activation.limitations))

    async def test_candidate_closed_at_cas_is_not_committed_as_selected(self):
        store = CountingStore(2)
        commit = store.commit
        first = True

        def concurrent_commit(records, idempotency_key, expected=None, **kwargs):
            nonlocal first
            if first:
                first = False
                store.close_state('state_0')
                raise ConflictError('state changed immediately before commit')
            return commit(records, idempotency_key, expected, **kwargs)

        store.commit = concurrent_commit
        builder = GroupContextBuilder(store, None, index=CountingIndex(), options={'llm_activation': False})
        selected, activation = await builder.selector.aselect_active_entries('test', snapshot='snap-1', persist=True)
        self.assertNotIn('state_0@1', [record.ref for record in selected])
        self.assertNotIn('state_0@1', activation.selected_refs)
        self.assertIn('snapshot_candidates_changed_after_await', ' '.join(activation.limitations))

    async def test_view_reuses_latest_projection_and_keeps_acl_dynamic(self):
        store = CountingStore(3)
        view = ContextReadView(store)
        view.allowed = lambda row: row.record_id != "state_1"
        first = view.list_records(snapshot="snap-1")
        second = view.list_records("temporary_state", snapshot="snap-1")
        self.assertEqual([r.ref for r in first], [r.ref for r in second])
        self.assertEqual(len(first), 2)
        first[0].payload["status"] = "mutated-copy"
        self.assertEqual(view.get_record(first[0].record_id, snapshot="snap-1").payload["status"], "open")
        self.assertEqual(store.loads, 1)


if __name__ == "__main__":
    unittest.main()
