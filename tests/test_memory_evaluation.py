"""Check evaluator diagnostics using explicit candidate/answer fixtures."""
from myatri.contracts import RecallBundle, Record
from warmagent.memory_evaluation import score_case


def bundle(refs):
    records = [Record(record_id=key, record_kind='raw_message', agent_id='eval', user_id='fixture',
        author_kind='human', body_markdown=key, payload={'role': 'user'}) for key in refs]
    return RecallBundle(records=records)


def test_stage_diagnostics_distinguish_miss_rank_budget_and_unused_answer():
    case = {'id': 'stages', 'expected_refs': ['absent@1', 'late@1', 'pruned@1', 'unused@1'],
        'answer_citations': []}
    report = score_case(case, bundle(['unused', 'pruned', 'late']), bundle(['unused']), k=2)
    assert {row['ref']: row['stage'] for row in report['diagnostics']} == {
        'absent@1': 'unretrieved', 'late@1': 'rank_below_k',
        'pruned@1': 'budget_pruned', 'unused@1': 'answer_unused'}
    assert report['answer_evaluated']


def test_no_answer_is_not_reported_as_answer_success_and_scope_is_checked():
    report = score_case({'id': 'scope', 'expected_refs': ['target@1'], 'forbidden_refs': ['secret@1']},
        bundle(['target', 'secret']), bundle(['target', 'secret']))
    assert not report['answer_evaluated']
    assert report['forbidden_returned'] == ['secret@1']
    assert report['recall_at_1'] == 1


def test_original_evidence_hit_is_reported_separately_from_event_hit():
    report = score_case({'id': 'original', 'expected_refs': ['event@1'],
        'evidence_groups': {'event@1': ['source@1']}}, bundle(['source']), bundle(['source']))
    assert report['recall_at_1'] == 0
    assert report['evidence_recall_at_1'] == 1
    assert report['diagnostics'] == []
