"""Small retrieval baseline over the real archive, index, and recall service.

The fixture measures lexical plus local TF-IDF candidate retrieval and the
service's deterministic rank fusion on four synthetic memories. It does not
measure a learned reranker or establish general recall quality.
"""

import asyncio
import json
from pathlib import Path

from myatri.contracts import Record
from myatri.memory import MemoryIndex
from myatri.retrieval import RecallService
from myatri.storage import ArchiveStore


FIXTURE = Path(__file__).parent / "fixtures" / "runtime_slice.json"


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _scope_recall(tmp_path, scope):
    fixture = _fixture()
    memories = [item for item in fixture["memories"] if item["scope"] == scope]
    store = ArchiveStore(tmp_path / scope, agent_id="warmagent", user_id=f"synthetic-{scope}")
    records = []
    refs = {}
    for item in memories:
        raw_id = f"source_{item['id']}"
        raw = Record(record_id=raw_id, record_kind="raw_message", agent_id="warmagent",
            user_id=f"synthetic-{scope}", author_kind="human", created_at=item["occurred_at"],
            body_markdown=item["source_text"], payload={"role": "user", "sent_at": item["occurred_at"]})
        event = Record(record_id=item["id"], record_kind="event", agent_id="warmagent",
            user_id=f"synthetic-{scope}", author_kind="system", created_at=item["occurred_at"],
            source_refs=[raw.ref], body_markdown=item["fact_summary"],
            payload={"occurred_at": item["occurred_at"], "fact_summary": item["fact_summary"],
                "source_spans": [{"message_ref": raw.ref, "start": 0,
                    "end": len(item["source_text"]), "quote": item["source_text"]}],
                "event_fingerprint": item["id"], "reality_scope": "reported"})
        records.extend((raw, event))
        refs[item["id"]] = event.ref
    for edge in fixture["contradictions"]:
        if edge["from"] in refs and edge["to"] in refs:
            records.append(Record(record_id=edge["id"], record_kind="edge", agent_id="warmagent",
                user_id=f"synthetic-{scope}", author_kind="system", source_refs=[refs[edge["from"]], refs[edge["to"]]],
                body_markdown="合成的更正关系", payload={"edge_id": edge["id"], "from_ref": refs[edge["from"]],
                    "to_ref": refs[edge["to"]], "type": edge["type"], "evidence_refs": [refs[edge["from"]], refs[edge["to"]]]}))
    store.commit(records, idempotency_key=f"runtime-slice-{scope}")
    index = MemoryIndex(store)
    asyncio.run(index.rebuild())
    return RecallService(store, index), refs


def test_direct_fact_recall_keeps_cited_source_and_recall_at_k(tmp_path):
    recall, refs = _scope_recall(tmp_path, "group")
    bundle = recall.recall("群里约定周五下午几点开读书会？")
    expected = refs["group_friday_meeting"]
    returned = [record.ref for record in bundle.records]
    assert expected in returned[:3]
    event = next(record for record in bundle.records if record.ref == expected)
    assert event.source_refs[0] in returned
    assert [event.ref, event.source_refs[0]] in bundle.paths
    recall_at_3 = len({expected} & set(returned[:3])) / 1
    assert recall_at_3 == 1.0


def test_correction_and_counterevidence_remain_retrievable(tmp_path):
    recall, refs = _scope_recall(tmp_path, "group")
    bundle = recall.recall("后来明确纠正后，他更喜欢红茶还是绿茶？")
    returned = {record.ref for record in bundle.records}
    corrected = refs["group_tea_correction"]
    initial = refs["group_tea_initial"]
    assert corrected in returned
    assert initial in returned
    assert initial in bundle.counter_refs
    assert corrected in bundle.counter_refs
    assert any(ref in returned for ref in next(record for record in bundle.records
        if record.ref == corrected).source_refs)


def test_group_and_private_recall_are_isolated(tmp_path):
    group_recall, group_refs = _scope_recall(tmp_path, "group")
    private_recall, private_refs = _scope_recall(tmp_path, "private")
    group_result = group_recall.recall("秋日暗号是什么？")
    private_result = private_recall.recall("秋日暗号是什么？")
    group_returned = {record.ref for record in group_result.records}
    private_returned = {record.ref for record in private_result.records}
    assert private_refs["private_secret_word"] in private_returned
    assert private_refs["private_secret_word"] not in group_returned
    assert all(ref.startswith("group_") or ref.startswith("source_group_") for ref in group_returned)
    assert all(ref.startswith("private_") or ref.startswith("source_private_") for ref in private_returned)


def test_fixture_exposes_multiround_long_native_tool_receipts():
    scenario = _fixture()["native_tool_receipt_scenario"]
    assert scenario["scope"] == "synthetic-warmagent-runtime/group"
    assert len(scenario["rounds"]) == 2
    for round_data in scenario["rounds"]:
        body = round_data["receipt_seed"] * round_data["receipt_repeat_count"]
        assert round_data["turn_id"] and round_data["call_id"]
        assert 1400 <= len(body) <= 1700
