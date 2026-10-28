"""Owner-authored next-step overrides, separate from the model's checkpoints."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3
import time


PLAN_TOOL = {'type': 'function', 'function': {
    'name': 'harness__activity_plan',
    'description': '用户安排自主活动时使用。next把事情插到当前活动下一步，原目标及下一步保留；'
        'redirect调整活动目标。status查看已保存安排。必须写入成功后才能说已安排；'
        'pending表示等待聊天结束后的安全步骤边界，applied表示已接入活动，不表示完成。'
        '新安排替换尚未接入的旧安排；不会强行解除手动暂停或重做结果未知的动作。',
    'parameters': {'type': 'object', 'properties': {
        'mode': {'type': 'string', 'enum': ['next', 'redirect', 'status']},
        'instruction': {'type': 'string', 'minLength': 1, 'maxLength': 500}},
        'required': ['mode'], 'additionalProperties': False}}}


class ActivityPlans:
    def __init__(self, path):
        self.path = path
        with self._db() as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS activity_plans (
                request_id TEXT PRIMARY KEY, source_ref TEXT NOT NULL, source_scope TEXT NOT NULL,
                owner_id TEXT NOT NULL, source_text TEXT NOT NULL, mode TEXT NOT NULL,
                instruction TEXT NOT NULL, status TEXT NOT NULL, previous TEXT,
                created REAL NOT NULL, applied_at REAL)''')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return closing(db)

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ('request_id', 'source_ref', 'source_scope',
            'mode', 'instruction', 'status', 'created', 'applied_at')}

    def view(self):
        with self._db() as db:
            rows = db.execute('SELECT * FROM activity_plans ORDER BY rowid DESC LIMIT 5').fetchall()
        return [self._public(row) for row in rows]

    def pending(self):
        with self._db() as db:
            return db.execute("SELECT 1 FROM activity_plans WHERE status='pending' LIMIT 1").fetchone() is not None

    def submit(self, *, source_ref, scope, owner_id, source_text, mode, instruction):
        if mode not in {'next', 'redirect'} or not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 500:
            raise ValueError('invalid_activity_plan')
        instruction = instruction.strip()
        request_id = 'plan_' + hashlib.sha256(json.dumps(
            [source_ref, mode, instruction], ensure_ascii=False).encode()).hexdigest()[:32]
        with self._db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM activity_plans WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                return self._public(existing)
            db.execute("UPDATE activity_plans SET status='superseded' WHERE status='pending'")
            db.execute('''INSERT INTO activity_plans(request_id,source_ref,source_scope,owner_id,
                source_text,mode,instruction,status,created) VALUES(?,?,?,?,?,?,?,'pending',?)''',
                (request_id, source_ref, scope, owner_id, source_text, mode, instruction, time.time()))
            return self._public(db.execute('SELECT * FROM activity_plans WHERE request_id=?', (request_id,)).fetchone())

    def apply(self, activity, protocol, revision):
        """Apply between effects; keep the previous checkpoint and close abandoned tool plans."""
        if protocol.get('uncertain_calls'):
            return activity, protocol
        with self._db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM activity_plans WHERE status='pending' ORDER BY rowid DESC LIMIT 1").fetchone()
            current = db.execute('SELECT * FROM idle_activity WHERE id=1').fetchone()
            if not row or current['control_revision'] != revision or current['pause_requested'] or current['status'] != 'running':
                return activity, protocol
            previous = dict(current)
            previous['protocol'] = deepcopy(protocol)
            value = deepcopy(protocol)
            messages = value.setdefault('messages', [])
            answered = {m.get('tool_call_id') for m in messages if m.get('role') == 'tool'}
            receipts = {r.get('call_id', r.get('id')): r for r in json.loads(current['receipts'])}
            for call in value.get('pending_calls', [])[value.get('next_call_index', 0):]:
                receipt = receipts.get(call['id'])
                if receipt and receipt.get('state') != 'completed':
                    return activity, protocol
                if call['id'] not in answered:
                    result = receipt.get('result') if receipt else {
                        'error': 'superseded_by_owner_plan', 'not_executed': True, 'request_id': row['request_id']}
                    messages.append({'role': 'tool', 'tool_call_id': call['id'],
                        'content': json.dumps(result, ensure_ascii=False)})
            value['pending_calls'], value['next_call_index'] = [], 0
            old_instruction = value.get('owner_instruction') or {}
            if old_instruction.get('status') == 'applied':
                db.execute("UPDATE activity_plans SET status='superseded' WHERE request_id=?",
                    (old_instruction['request_id'],))
            value['owner_instruction'] = {**self._public(row), 'status': 'applied',
                'previous_goal': current['goal'], 'previous_next_step': current['next_step'],
                'resume_previous': bool(activity)}
            goal = row['instruction'] if row['mode'] == 'redirect' or not activity else current['goal']
            db.execute('''UPDATE idle_activity SET goal=?,next_step=?,protocol=?,rest_until=NULL,updated=?
                WHERE id=1''', (goal, row['instruction'], json.dumps(value, ensure_ascii=False), time.time()))
            db.execute("UPDATE activity_wakeups SET status='cancelled' WHERE status='scheduled' AND task_id=?",
                (str(value.get('activity_id', '')),))
            db.execute("UPDATE activity_plans SET status='applied',previous=?,applied_at=? WHERE request_id=?",
                (json.dumps(previous, ensure_ascii=False), time.time(), row['request_id']))
            state = dict(activity or {})
            state.update(goal=goal, next_step=row['instruction'], protocol=value, status='running')
            return state, value

    def complete(self, protocol):
        instruction = protocol.get('owner_instruction') or {}
        if instruction.get('status') != 'applied':
            return None
        with self._db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT previous FROM activity_plans WHERE request_id=?',
                (instruction['request_id'],)).fetchone()
            if not row:
                return None
            previous = json.loads(row['previous'])
            instruction = dict(instruction, status='completed')
            restored = previous['protocol'].get('owner_instruction')
            resume = instruction['mode'] == 'next' and instruction['resume_previous']
            protocol['owner_instruction'] = restored if resume and restored else instruction
            if resume and restored and restored.get('status') == 'applied':
                db.execute("UPDATE activity_plans SET status='applied' WHERE request_id=?",
                    (restored['request_id'],))
            db.execute('UPDATE idle_activity SET protocol=? WHERE id=1',
                (json.dumps(protocol, ensure_ascii=False),))
            if resume:
                db.execute('UPDATE idle_activity SET goal=?,next_step=? WHERE id=1',
                    (instruction['previous_goal'], instruction['previous_next_step']))
            db.execute("UPDATE activity_plans SET status='completed' WHERE request_id=? AND status='applied'",
                (instruction['request_id'],))
        return instruction
