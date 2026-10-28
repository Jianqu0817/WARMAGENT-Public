"""Cheap topic selection for subjective material, never a factual truth gate."""
import re

from .topics import overlap_many, terms


_GENERAL = set('今天 最近 喜欢 感觉 这个 那个 什么 有点 自己 一下 看看 现在 想要 想起 看到 发现 时候 可以 知道 觉得 怎么 还有 你们 我们 事情 然后'.split())
_TOPIC_BREAKS = re.compile('|'.join(sorted(_GENERAL)))
_REVISIT = re.compile(r'随笔|日记|兴趣|进展|小本|写了什么|有什么发现|最近.{0,6}(?:做|忙|看|研究|想)|在忙什么')


def select_personal(query, rows, fields, limit):
    if _REVISIT.search(query):
        return rows[:limit]
    query = query[:2000]
    texts = [' '.join(str(row.get(field, '')) for field in fields) for row in rows]
    scores = overlap_many(query, texts)
    meaningful = {term for part in _TOPIC_BREAKS.split(query) for term in terms(part)
                  if re.search(r'[\u4e00-\u9fff]', term)}
    words = set(re.findall(r'\b[a-z][a-z0-9_+-]{2,}\b', query.casefold()))
    ranked = []
    for index, (row, text, score) in enumerate(zip(rows, texts, scores)):
        topical = meaningful & terms(text) or words & set(re.findall(r'\b[a-z][a-z0-9_+-]{2,}\b', text.casefold()))
        if topical and score >= .12:
            ranked.append((score, -index, row))
    ranked.sort(key=lambda item: item[:2], reverse=True)
    return [row for _, _, row in ranked[:limit]]
