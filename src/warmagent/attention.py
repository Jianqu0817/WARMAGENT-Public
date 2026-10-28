"""Group short follow-up attention, atomic with transport deduplication."""

import re

from myatri.contracts import digest


def name_call(text):
    """A vocative at a sentence boundary; discussing a name is not a summons."""
    return bool(re.search(r'(?:^|[，,。！？!?\n])\s*(?:小温暖|小溫暖)(?![的是和跟被])'
                          r'(?:[\s，,。！？!?：:]|你|请|帮|能|在吗|$)', text))


def low_information(text):
    value = re.sub(r'\[[^\[\]]{1,12}\]', '', text).strip()
    return (not re.search(r'[\w\u3400-\u9fff]', value)
            or bool(re.fullmatch(r'(?:嗯|哦|噢|好|哈|呵|笑死|确实|是的|收到|ok|OK|[\s。！!，,])+', value)))


def followup(db, scope, key, *, wake, now, sender_id, ttl=120):
    if wake:
        rounds = 1 + int(digest(["attention-v2", key])[:8], 16) % 4
        db.execute(
            "INSERT OR REPLACE INTO attention_windows(scope,remaining,expires,wake_key,sender_id) VALUES(?,?,?,?,?)",
            (scope, rounds, now + ttl, key, sender_id),
        )
        return {"wake": True, "active": False, "remaining": rounds}
    row = db.execute(
        "SELECT remaining,expires,sender_id FROM attention_windows WHERE scope=?", (scope,)
    ).fetchone()
    if not row or row[0] <= 0 or row[1] <= now or row[2] != sender_id:
        return {"wake": False, "active": False, "remaining": 0}
    db.execute(
        "UPDATE attention_windows SET remaining=remaining-1 WHERE scope=?", (scope,)
    )
    return {"wake": False, "active": True, "remaining": row[0] - 1}
