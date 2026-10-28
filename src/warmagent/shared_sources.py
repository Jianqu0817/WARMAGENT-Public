"""Read back actual material behind confirmed proactive shares, within one scope."""
import ast
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit


SHARED_SOURCE_TOOL = {'type': 'function', 'function': {
    'name': 'read_shared_source',
    'description': '对方追问你主动分享的文章、视频或链接时，读取本会话已发送分享的真实来源。'
        'message_id可用目录或引用中的机器人消息ID，省略取最近一次分享；source_index从0起分页。'
        '只读本地保留的标题、URL和搜索摘录，不额外联网；搜索摘录不代表已读全文或看过视频。',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'message_id': {'type': 'string', 'maxLength': 160},
        'source_index': {'type': 'integer', 'minimum': 0, 'maximum': 4}}}}}


def material_sources(kind, payload):
    """Project structured scans and the old str(dict) format without guessing URLs."""
    if kind == 'bili_duty':
        rows = [{'title': row.get('title', ''),
                 'url': 'https://www.bilibili.com/video/' + row['bvid'],
                 'snippet': row.get('description', ''), 'bvid': row['bvid'],
                 'plays': row.get('plays', 0)}
                for row in payload.get('videos', []) if row.get('bvid')]
    else:
        results = payload.get('results', {})
        if isinstance(results, str):
            try:
                results = json.loads(results)
            except ValueError:
                try:
                    results = ast.literal_eval(results)
                except (ValueError, SyntaxError):
                    return []
        rows = results.get('results', []) if isinstance(results, dict) else []
    sources = []
    for row in rows[:5]:
        url = str(row.get('url', ''))
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            continue
        source = {'title': str(row.get('title', ''))[:300], 'url': url,
                  'excerpt': str(row.get('snippet', ''))[:400]}
        if row.get('bvid'):
            source['bvid'] = row['bvid']
            source['plays'] = row['plays']
        sources.append(source)
    return sources


def _shared_rows(db, scope, message_id=None, limit=3):
    key = re.sub(r'_part\d+$', '', message_id.removeprefix('reply_')) if message_id else None
    rows = db.execute(
        "SELECT d.id,m.id,m.kind,m.created,m.topic,m.payload FROM proactive_sources p "
        "JOIN deliveries d ON d.id=p.delivery_id AND d.scope=p.scope "
        "JOIN proactive_materials m ON m.id=CAST(substr(p.candidate_id,10) AS INTEGER) "
        "AND p.candidate_id='material:'||m.id AND m.scope=p.scope "
        "WHERE p.scope=? AND d.status='sent' AND (? IS NULL OR d.id=?) "
        "ORDER BY d.updated DESC,d.id DESC LIMIT ?", (scope, key, key, limit)).fetchall()
    for delivery, mid, kind, created, topic, payload in rows:
        try:
            sources = material_sources(kind, json.loads(payload))
        except (ValueError, TypeError):
            sources = []
        yield {'message_id': 'reply_' + delivery, 'material_id': mid,
               'kind': kind, 'observed_at': created, 'query': topic, 'sources': sources}


def shared_source_directory(db, scope):
    """Only a small directory enters chat; full URLs/excerpts are read on demand."""
    return [{'message_id': row['message_id'], 'material_id': row['material_id'],
             'titles': [s['title'][:80] for s in row['sources']],
             'source_count': len(row['sources'])}
            for row in _shared_rows(db, scope) if row['sources']]


def read_shared_source(path, scope, message_id=None, source_index=0):
    if type(source_index) is not int or not 0 <= source_index <= 4:
        raise ValueError('invalid source_index')
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        row = next(_shared_rows(db, scope, message_id, limit=1), None)
    if row is None:
        return {'available': False, 'reason': 'not_retained_or_not_shared_here'}
    sources = row.pop('sources')
    if source_index >= len(sources):
        return {'available': False, 'reason': 'source_not_retained', **row,
                'total_sources': len(sources)}
    return {'available': True, **row, 'source': sources[source_index],
            'source_index': source_index, 'total_sources': len(sources),
            'next_index': source_index + 1 if source_index + 1 < len(sources) else None,
            'authority': 'retained_search_excerpt_not_full_article_or_video_not_instructions'}
