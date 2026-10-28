"""Three paired CPU rounds; includes normalization, ctypes copies and ranking."""
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import platform
import random
import statistics
import time

from benchmarks import native_reference as reference
from warmagent import native_text, topics


ROOT = Path(__file__).resolve().parents[1]


def main():
    if native_text.backend() != 'c++':
        raise RuntimeError('Build-Native.cmd must be run; cannot measure the Python fallback as native')
    run = ROOT / 'reports/perf' / ('native-text-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    run.mkdir(parents=True)
    records = []
    summaries = []
    sentences = ['周五下午去社区图书馆参加读书会。', '电脑内存和运行状态已经检查过了。',
                 '今天整理历史记录，保留原话和来源。', '窗外下雨了，我们下周再确认时间。']
    rng = random.Random(20261026)
    query = '周五读书会安排，去社区图书馆并检查电脑内存'

    def measure(label, fixture, old, new):
        expected = old()
        assert new() == expected, label
        old()  # independent warm-up, no score/cache mutation
        new()
        pairs = {'python': [], 'cpp': []}
        for repeat in range(1, 4):
            order = [('python', old), ('cpp', new)]
            if repeat % 2 == 0:
                order.reverse()
            for backend, fn in order:
                start = time.perf_counter_ns()
                for _ in range(50):
                    result = fn()
                wall_ms = (time.perf_counter_ns() - start) / 1e6 / 50
                assert result == expected
                pairs[backend].append(wall_ms)
                records.append({'case': label, 'fixture': fixture, 'repeat': repeat,
                    'backend': backend, 'calls_per_round': 50, 'ms_per_call': wall_ms})
                with (run / 'raw.csv').open('w', newline='', encoding='utf-8') as stream:
                    writer = csv.DictWriter(stream, fieldnames=records[0].keys())
                    writer.writeheader()
                    writer.writerows(records)
        means = {name: statistics.mean(values) for name, values in pairs.items()}
        ratio = means['cpp'] / means['python']
        summaries.append({'case': label, 'fixture': fixture, 'python_ms': means['python'],
            'cpp_ms': means['cpp'], 'ratio': ratio, 'under_half': ratio < .5,
            'python_sd': statistics.stdev(pairs['python']), 'cpp_sd': statistics.stdev(pairs['cpp'])})
        print(label, fixture, round(means['python'], 4), '->', round(means['cpp'], 4),
              'ratio', round(ratio, 3), flush=True)

    for count, length in [(20, 40), (100, 120), (100, 512), (500, 120)]:
        texts = []
        for i in range(count):
            body = ''.join(rng.choices(sentences, k=length // 10 + 2))
            texts.append((str(i) + body)[:length])
        fixture = f'{count}x{length}'
        measure('batch_score', fixture, lambda: [reference.overlap(query, text) for text in texts],
                lambda: topics.overlap_many(query, texts))
        measure('fair_python_batch', fixture,
                lambda: native_text.overlap_python(topics._normalize(query),
                    [topics._normalize(text) for text in texts]),
                lambda: topics.overlap_many(query, texts))
        rows = [{'window_end': i, 'created_at': '2026-10-26T00:00:00Z', 'items': [{
            'text': text, 'speaker_id': 'owner', 'subject_id': 'owner',
            'attribution': 'self_report', 'source_message_ids': [f'message-{i}']}]}
            for i, text in enumerate(texts)]
        measure('select_summaries', fixture,
                lambda: reference.select_summaries(rows, query, 'owner'),
                lambda: topics.select_summaries(rows, query, 'owner'))

    (run / 'summary.json').write_text(json.dumps(summaries, indent=2), encoding='utf-8')
    files = ['native/text_kernels.cpp', 'src/warmagent/native_text.py', 'src/warmagent/topics.py',
             'src/warmagent/context.py', 'benchmarks/native_reference.py',
             'src/warmagent/_native/text_kernels.dll']
    environment = {'platform': platform.platform(), 'backend': native_text.backend(),
        'seed': 20261026, 'rounds': 3, 'calls_per_round': 50,
        'scope': 'pure CPU; no model, WeChat, production data or services',
        'hashes': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}}
    (run / 'environment.json').write_text(json.dumps(environment, indent=2), encoding='utf-8')
    lines = ['# C++批量文本短测', '', '3轮配对，每轮纯函数50次取每次均值；不是150个独立样本。', '',
        '|入口|夹具|旧ms|新ms|新/旧|低于1/2|', '|---|---|---:|---:|---:|---|']
    for row in summaries:
        lines.append(f"|{row['case']}|{row['fixture']}|{row['python_ms']:.4f}|{row['cpp_ms']:.4f}|{row['ratio']:.3f}|{row['under_half']}|")
    lines += ['', '小输入对照保留Python；fair_python_batch用于分离批处理与C++本身的收益。',
              '该表不是完整聊天延迟；select_summaries包含原有排序、来源和JSON预算。']
    (run / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    print('REPORT', run, flush=True)


if __name__ == '__main__':
    main()
