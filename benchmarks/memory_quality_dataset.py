"""Small explicit gold corpus, independent of retrieval implementation."""
from myatri.contracts import Event, Record, SourceSpan


def gold_dataset():
    records, queries = [], []
    def fact(key, text, *, sender='owner', name='星河', room='room', private=False, day=20):
        stamp = f'2026-10-{day:02d}T10:00:00+00:00'
        raw = Record(record_id='raw-' + key, record_kind='raw_message', agent_id='eval', user_id='synthetic',
            author_kind='human', created_at=stamp, body_markdown=text,
            payload={'role': 'user', 'channel_source': {'platform_id': 'fixture', 'bot_id': 'bot',
                'group_id': room, 'conversation_kind': 'private' if private else 'group',
                'sender_id': sender, 'sender_name': name, 'message_id': key, 'text': text}})
        event = Record(record_id=key, record_kind='event', agent_id='eval', user_id='synthetic',
            author_kind='system', created_at=stamp, body_markdown=text, source_refs=[raw.ref],
            payload=Event(occurred_at=stamp, participants=[sender], fact_summary=text,
                event_fingerprint=key, source_spans=[SourceSpan(message_ref=raw.ref, start=0, end=len(text), quote=text)]).model_dump())
        records.extend([raw, event])
        return event
    fact('key', '备用钥匙放在蓝色铁盒，铁盒收在玄关柜子。')
    fact('umbrella', '雨伞放在办公室门后，不在家里的鞋柜。', sender='member', name='阿青')
    fact('meeting', '读书会定在周五下午三点，地点是图书馆二楼。', day=23)
    fact('walk', '星河散步喜欢走河边，因为那里安静，能看落日。')
    fact('alias', '星河也叫阿星，这是同一个人的昵称。', name='阿星')
    fact('tea-old', '星河以前偏爱绿茶。', day=21)
    fact('tea-new', '星河更正饮品偏好：现在更喜欢红茶，旧的绿茶偏好已不适用。', day=24)
    fact('trip', '周末去植物园，决定带相机拍银杏。', day=25)
    fact('camera', '相机充电器放在书房抽屉，出门拍银杏前要带上。', day=25)
    fact('secret', '私聊约定的暗号是琥珀月亮。', private=True, room='private-owner')
    fact('other-room', '另一个群的聚会地点是北门咖啡馆。', room='other-room')
    records.append(Record(record_id='tea-edge', record_kind='edge', agent_id='eval', user_id='synthetic',
        author_kind='system', source_refs=['tea-old@1', 'tea-new@1'], body_markdown='有原文的偏好更正',
        payload={'edge_id': 'tea-edge', 'from_ref': 'tea-old@1', 'to_ref': 'tea-new@1',
            'type': 'contradicts', 'evidence_refs': ['tea-old@1', 'tea-new@1']}))
    for revision, text in ((1, '备用钥匙可能在书架。'), (2, '更正推测：备用钥匙在玄关的蓝色铁盒。')):
        records.append(Record(record_id='claim', revision=revision,
            supersedes_revision=revision - 1 if revision > 1 else None,
            record_kind='interpretation', agent_id='eval', user_id='synthetic', author_kind='system',
            source_refs=['raw-key@1'], body_markdown=text,
            payload={'status': 'corrected' if revision == 2 else 'provisional', 'evaluation_phase': revision - 1}))
    def query(key, text, expected, **kwargs):
        queries.append({'id': key, 'query': text, 'expected_refs': [ref + '@1' for ref in expected],
            'category': kwargs.pop('category', 'direct'), **kwargs})
    query('key-direct', '备用钥匙放在哪', ['key'])
    query('key-location', '玄关的蓝色铁盒里装了什么', ['key'])
    query('self-speaker', '我把备用钥匙放哪了', ['key'], category='identity')
    query('alias-name', '阿星的备用钥匙在哪里', ['key'], category='alias')
    query('other-speaker', '阿青把雨伞放在哪里', ['umbrella'], sender_id='member', category='identity')
    query('speaker-negative', '我把雨伞放在哪里', [], forbidden_refs=['umbrella@1'], category='identity')
    query('meeting-time', '读书会几点开始', ['meeting'])
    query('meeting-date', '2026年10月23日约了什么', ['meeting'], category='date')
    query('walk-motive', '为什么喜欢去河边散步', ['walk'], category='paraphrase')
    query('walk-place', '想找个安静的地方看落日，之前去哪里', ['walk'], category='paraphrase')
    query('tea-correction', '现在喜欢红茶还是绿茶，之前如何更正的', ['tea-new', 'tea-old'], category='correction')
    query('trip-preparation', '去植物园拍银杏要准备什么，东西在哪里', ['trip', 'camera'], category='cross_event')
    query('camera-location', '相机充电器在哪里', ['camera'])
    query('private-visible', '私聊暗号是什么', ['secret'], private=True, category='scope')
    query('private-hidden', '私聊暗号是什么', [], forbidden_refs=['secret@1', 'raw-secret@1'], category='scope')
    query('other-group-hidden', '另一个群在哪里聚会', [], forbidden_refs=['other-room@1', 'raw-other-room@1'], category='scope')
    query('other-group-visible', '北门咖啡馆的聚会', ['other-room'], scope=['fixture', 'bot', 'other-room'], category='scope')
    query('historical', '备用钥匙可能在书架', [], phase=0, category='snapshot')
    queries[-1]['expected_refs'] = ['claim@1']
    queries[-1]['forbidden_refs'] = ['claim@2']
    query('current-revision', '备用钥匙位置更正', [], category='revision')
    queries[-1]['expected_refs'] = ['claim@2']
    queries[-1]['forbidden_refs'] = ['claim@1']
    query('tight-budget', '备用钥匙放哪', ['key'], budget=40, category='budget')
    return {'id': 'memory-gold-v1', 'owner_id': 'owner', 'owner_name': '星河',
        'records': [r.model_dump(mode='json') for r in records], 'queries': queries}


if __name__ == '__main__':
    import json
    import sys
    from pathlib import Path
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(gold_dataset(), ensure_ascii=False, indent=2), encoding='utf-8')
