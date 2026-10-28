"""Frozen Python reference for the native-text equivalence benchmark."""
import json
import re

def terms(text):
    text = re.sub(r"\s+|[，。！？、,.!?…]|小[温溫]暖", "", text.lower())
    return {text[i:i + 2] for i in range(max(0, len(text) - 1))}


def overlap(left, right):
    a, b = terms(left), terms(right)
    return len(a & b) / max(1, min(len(a), len(b)))


def select_summaries(summaries, query, sender, *, max_chars=30000):
    """Bound relevance; carry uncertainty/corrections without inventing freshness."""
    ranked = []
    for summary in summaries:
        for item in summary["items"]:
            row = {key: item[key] for key in ("text", "speaker_id", "subject_id", "attribution", "source_message_ids")}
            row.update({key: item[key] for key in ("facet", "change", "supersedes_message_ids", "valid_time", "interaction_kind", "source_quotes") if key in item})
            row.update(window_end=summary["window_end"], created_at=summary["created_at"])
            score = overlap(query, item["text"]) * 5 + (item["subject_id"] == sender)
            score += .5 if any(word in item["text"] for word in ("更正", "不再", "取消", "改为", "约定", "答应")) else 0
            row["attention_score"] = score
            ranked.append((score, summary["window_end"], row))
    ranked.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    selected, used, seen = [], 0, set()
    for _, _, row in ranked:
        key = (row["speaker_id"], row["subject_id"], row["text"])
        size = len(json.dumps(row, ensure_ascii=False))
        if key not in seen and used + size <= max_chars:
            selected.append(row)
            used += size
            seen.add(key)
    return {"items": sorted(selected, key=lambda r: r["window_end"]), "omitted_item_count": len(ranked)-len(selected),
            "note": "按本轮人物和话题选择；未选择不代表遗忘，摘要不是原话。"}
