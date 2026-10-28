"""Durable continuation of the original chat model/tool protocol."""
from contextlib import closing
import json
import sqlite3
import time


BATCH_REPORT_SCHEMA = {'type': 'object', 'properties': {
    'state': {'type': 'string', 'enum': ['completed', 'continue', 'blocked']},
    'report': {'type': 'string', 'minLength': 1, 'maxLength': 1800},
    'next_step': {'type': 'string', 'maxLength': 500}},
    'required': ['state', 'report', 'next_step'], 'additionalProperties': False}
BATCH_REPORT_RULES = (
    '本批执行额度即将用完，或宿主请求在步骤边界让位。本轮只生成JSON阶段报告，不调用操作工具。'
    '按真实回执说明已经查到的结果和未完成项，不把工具可用或局部结果写成目标已完成。'
    'state=completed表示用户目标已完成；continue表示还有可执行步骤，必须填写next_step，宿主保存后续跑；'
    'blocked表示缺条件或结果未知，停止自动续跑并说明原因。未知效果先核对，不换ID重做。'
    'report是小温暖直接给用户看的自然中文，简短包含成果和剩余部分；不要输出思考过程。'
)


def unknown_outcome(value):
    if isinstance(value, dict):
        return (value.get('error') == 'outcome_unknown'
            or value.get('state') == 'unknown' or value.get('status') == 'unknown'
            or any(unknown_outcome(child) for child in value.values()))
    return isinstance(value, list) and any(unknown_outcome(child) for child in value)


class ChatBatches:
    def __init__(self, path):
        self.path = path
        with self._db() as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS chat_batches (
                key TEXT PRIMARY KEY, scope TEXT NOT NULL, batch_index INTEGER NOT NULL,
                state TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
                last_error TEXT, updated REAL NOT NULL)''')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return closing(db)

    def recover(self):
        # Called only when the service lock is held and the previous worker is gone.
        with self._db() as db, db:
            db.execute("UPDATE chat_batches SET state='pending' WHERE state='running'")

    def queue(self, key, scope, batch_index):
        with self._db() as db, db:
            self.queue_in(db, key, scope, batch_index)

    @staticmethod
    def queue_in(db, key, scope, batch_index):
        db.execute('''INSERT INTO chat_batches(key,scope,batch_index,state,updated)
                VALUES(?,?,?,'pending',?) ON CONFLICT(key) DO UPDATE SET
                batch_index=MAX(chat_batches.batch_index,excluded.batch_index),state=CASE WHEN chat_batches.state IN ('paused','cancelled')
                THEN chat_batches.state ELSE 'pending' END,updated=excluded.updated''',
                (key, scope, batch_index, time.time()))

    def state(self, key):
        with self._db() as db:
            row = db.execute('SELECT state FROM chat_batches WHERE key=?', (key,)).fetchone()
        return row['state'] if row else None

    def claim(self, *, local, scopes, platforms=None):
        if local and not scopes:
            return None
        with self._db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT b.key,b.scope,b.batch_index,d.envelope FROM chat_batches b JOIN deliveries d ON b.key=d.id "
                "WHERE b.state='pending' AND b.next_attempt<=? ORDER BY b.updated", (time.time(),)).fetchall()
            for row in rows:
                source = json.loads(row['envelope'])
                if platforms is not None and source.get('platform_id') not in platforms:
                    continue
                if (source.get('transport') == 'local_cli') != local or local and row['scope'] not in scopes:
                    continue
                db.execute("UPDATE chat_batches SET state='running',updated=? WHERE key=?", (time.time(), row['key']))
                return dict(row)
        return None

    def finish(self, key, state='completed', error=None):
        with self._db() as db, db:
            db.execute("UPDATE chat_batches SET state=?,last_error=?,updated=? WHERE key=? AND state!='cancelled'",
                (state, error, time.time(), key))

    def control(self, scope, action):
        state = {'pause': 'paused', 'resume': 'pending', 'cancel': 'cancelled'}[action]
        with self._db() as db, db:
            count = db.execute("UPDATE chat_batches SET state=?,updated=? WHERE scope=? "
                "AND (state IN ('pending','running','paused') OR (? AND state='blocked' AND last_error IS NOT NULL))",
                (state, time.time(), scope, action == 'resume')).rowcount
        return {'state': state, 'changed': count}

    def view(self, scope):
        with self._db() as db:
            return [dict(row) for row in db.execute('SELECT key,state,batch_index,last_error FROM chat_batches '
                'WHERE scope=? ORDER BY updated DESC LIMIT 3', (scope,))]
