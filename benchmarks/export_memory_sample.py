"""Create a local, masked event sample without changing the live archive.

Queries/expected refs must be annotated separately; extraction is not a quality
score. Keep output under ignored reports/, never upload it as a public fixture.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from myatri.contracts import Event, Record, SourceSpan
from warmagent.index_worker import IndexReadStore


def export(root, agent, user, output, limit=100):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not root.is_dir() or output.is_relative_to(root):
        raise ValueError('Use an existing archive and a separate output directory')
    store = IndexReadStore(root, agent, user)
    view = store.read_view()
    rows = view.list_records()
    by_ref = {r.ref: r for r in rows}
    events = sorted((r for r in rows if r.record_kind == 'event' and r.source_refs),
        key=lambda row: (row.created_at, row.ref), reverse=True)[:limit]
    replacements = {}
    for row in rows:
        channel = row.payload.get('channel_source', {})
        for key in ('sender_id', 'sender_name', 'group_id', 'bot_id'):
            value = channel.get(key)
            if isinstance(value, str) and value.strip() and value not in replacements:
                replacements[value] = f'人物或渠道{len(replacements) + 1}'
    def mask(text):
        for old in sorted(replacements, key=len, reverse=True):
            if len(old) > 1:
                text = text.replace(old, replacements[old])
        text = re.sub(r'https?://\S+', '[链接]', text)
        return re.sub(r'\b(?:wxid_[A-Za-z0-9]+|\d{7,})\b', '[账号]', text)
    result, mapping = [], {}
    for number, row in enumerate(events):
        # Use the event's original quoted human spans, not its generated summary
        # as synthetic human evidence. Skip events without a retained raw source.
        originals = [(by_ref.get(span.get('message_ref')), span) for span in row.payload.get('source_spans', [])]
        originals = [(raw, span) for raw, span in originals if raw is not None
            and raw.record_kind == 'raw_message' and raw.author_kind == 'human' and span.get('quote')]
        if not originals:
            continue
        refs, spans = [], []
        for position, (raw, span) in enumerate(originals):
            key = f'source-{number}-{position}'
            text = mask(span['quote'])
            source = Record(record_id=key, record_kind='raw_message', agent_id='eval', user_id='local-sample',
                author_kind='human', created_at=raw.created_at, body_markdown=text,
                payload={'role': 'user', 'sampled_excerpt': True, 'channel_source': {
                    'platform_id': 'fixture', 'bot_id': 'bot', 'group_id': 'room',
                    'conversation_kind': 'group', 'sender_id': 'owner', 'sender_name': '用户A',
                    'message_id': key, 'text': text}})
            result.append(source)
            refs.append(source.ref)
            spans.append(SourceSpan(message_ref=source.ref, start=0, end=len(text), quote=text))
            mapping[source.ref] = {'ref': raw.ref, 'content_hash': raw.content_hash}
        text = mask(row.body_markdown)
        event = Record(record_id=f'event-{number}', record_kind='event', agent_id='eval', user_id='local-sample',
            author_kind='system', created_at=row.created_at, source_refs=refs, body_markdown=text,
            payload=Event(fact_summary=text, occurred_at=row.payload.get('occurred_at'), participants=['owner'],
                event_fingerprint=f'local-sample-{number}', source_spans=spans).model_dump())
        result.append(event)
        mapping[event.ref] = {'ref': row.ref, 'content_hash': row.content_hash}
    dataset = {'id': 'local-masked-events-v1', 'owner_id': 'owner', 'owner_name': '用户A',
        'sampling': 'Recent events with original human quotations; single flattened audience, not an ACL benchmark.',
        'records': [r.model_dump(mode='json') for r in result], 'queries': []}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding='utf-8')
    output.with_suffix('.sources.json').write_text(json.dumps({'watermark': view.snapshot(),
        'mapping': mapping}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'events': len(mapping) - sum(r.record_kind == 'raw_message' for r in result),
        'records': len(result), 'queries': 0, 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--agent', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    export(args.root, args.agent, args.user, args.output, args.limit)
