"""Differential contracts for the optional C++ scoring boundary."""
import random

import pytest

from benchmarks import native_reference as reference
from warmagent import native_text, topics


@pytest.mark.skipif(native_text.backend() != 'c++', reason='Build-Native.cmd not run')
@pytest.mark.parametrize('query,texts', [
    ('', ['', 'a', '中文']),
    ('小温暖 周五🙂', ['小溫暖，周五🙂', '周五\t🙂', '陌生话题', '周五🙂']),
    ('İΣς\u2003中文', ['i\u0307σς中文', 'İΣς\u00a0中文']),
    ('\x00\x00😀\ud800', ['\x00\x00', '😀\ud800', '\ud800\udfff', '']),
    ('重复重复重复', ['重复', '重复重复', '不相干', '重复']),
])
def test_native_unicode_codepoints_and_duplicates_match_python(query, texts):
    cleaned = [topics._normalize(text) for text in texts]
    actual = native_text.overlap_batch(topics._normalize(query), cleaned)
    assert actual == [reference.overlap(query, text) for text in texts]


@pytest.mark.skipif(native_text.backend() != 'c++', reason='Build-Native.cmd not run')
def test_native_differential_random_unicode():
    rng = random.Random(20261026)
    alphabet = list('中文读书会ABCİΣς小温溫暖\x00\t\n，。！？🙂🌈') + ['\ud800', '\udfff']
    for _ in range(100):
        query = ''.join(rng.choices(alphabet, k=rng.randrange(0, 50)))
        texts = [''.join(rng.choices(alphabet, k=rng.randrange(0, 160))) for _ in range(40)]
        assert native_text.overlap_batch(topics._normalize(query),
            [topics._normalize(text) for text in texts]) == [reference.overlap(query, text) for text in texts]


def test_missing_binary_uses_same_python_scores(monkeypatch):
    monkeypatch.setattr(native_text, '_library', None)
    texts = ['读书会🙂' * 100, '别的话题' * 100] * 20
    assert topics.overlap_many('读书会', texts) == [reference.overlap('读书会', text) for text in texts]
    assert native_text.overlap_batch('query', []) == []


def test_small_batch_does_not_cross_native_boundary(monkeypatch):
    def unexpected(*_args):
        raise AssertionError('small inputs must remain on Python')
    monkeypatch.setattr(topics, 'overlap_batch', unexpected)
    assert topics.overlap_many('周五', ['周五', '周六']) == [1.0, 0.0]


def test_summary_selection_preserves_corrections_ties_budget_and_sources():
    summaries = [{'window_end': i, 'created_at': '2026-10-26T00:00:00Z', 'items': [{
        'text': ('更正：周五读书会改为周六。' if i % 3 else '检查电脑内存运行状态。') * 20,
        'speaker_id': 'owner', 'subject_id': 'owner' if i % 2 else 'other',
        'attribution': 'self_report', 'source_message_ids': [f'message-{i}'],
        'source_quotes': [{'message_id': f'message-{i}', 'quote': '保留原文来源'}]}]}
        for i in range(100)]
    for budget in (0, 1000, 30000):
        assert topics.select_summaries(summaries, '周五读书会', 'owner', max_chars=budget) == (
            reference.select_summaries(summaries, '周五读书会', 'owner', max_chars=budget))
