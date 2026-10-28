from copy import deepcopy

from myatri.contracts import Record
from warmagent.record_copy import clone_record


def test_verified_record_copy_preserves_values_hash_and_fields_metadata():
    record = Record(record_id='source', record_kind='raw_message',
        body_markdown='中文🙂\x00 original', payload={'role': 'user',
            'large_integer': 2 ** 100, 'small_float': 1e-100,
            'sources': [{'ref': 'evidence@1', 'values': [None, False, 3.5]}]})
    clone = clone_record(record)
    assert clone == deepcopy(record)
    assert clone.ref == record.ref
    assert clone.content_hash == record.content_hash
    assert clone.model_fields_set == record.model_fields_set
    for name, value in record.__dict__.items():
        if isinstance(value, (list, dict)):
            assert clone.__dict__[name] is not value, name
    clone.payload['sources'][0]['values'].append('changed')
    clone.source_refs.append('other@2')
    clone.model_fields_set.add('extra-marker')
    assert record.payload['sources'][0]['values'] == [None, False, 3.5]
    assert record.source_refs == []
    assert 'extra-marker' not in record.model_fields_set


def test_derived_record_copy_keeps_generator_and_revision_sources_independent():
    record = Record(record_id='derived', revision=2, supersedes_revision=1,
        record_kind='interpretation', source_refs=['source@1'], author_kind='model',
        generator={'provider': 'fixture', 'model': 'fixture', 'prompt_version': 'v1',
                   'run_id': 'test', 'details': {'attempts': [1, 2]}},
        body_markdown='带来源的解释', payload={'claim_state': {'change': 'correction'}})
    clone = clone_record(record)
    assert clone == deepcopy(record)
    for name, value in record.__dict__.items():
        if isinstance(value, (list, dict)):
            assert clone.__dict__[name] is not value, name
    clone.generator['details']['attempts'].append(3)
    clone.payload['claim_state']['change'] = 'retraction'
    assert record.generator['details']['attempts'] == [1, 2]
    assert record.payload['claim_state']['change'] == 'correction'
