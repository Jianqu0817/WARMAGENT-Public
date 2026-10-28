"""One memory library, with independent channel working windows.

Queue jobs, usage and protocols remain in the library's runtime database.
Only working-window metadata is namespaced; clearing a chat cannot clear a
different chat or erase long-term sources.
"""
from datetime import datetime, timezone
import json

from myatri.contracts import digest
from myatri.conversation.session import SessionWindow


class ScopedQueue:
    KEYS = {'context_window_boundary', 'context_token_anchor', 'group_context_boundary'}

    def __init__(self, queue, scope):
        self.queue, self.scope = queue, scope

    def __getattr__(self, name):
        return getattr(self.queue, name)

    def key(self, name):
        return f'context:{self.scope}:{name}' if name in self.KEYS else name

    def get_meta(self, name, default=None):
        return self.queue.get_meta(self.key(name), default)

    def set_meta(self, name, value):
        return self.queue.set_meta(self.key(name), value)


class ScopedSessionWindow(SessionWindow):
    def clear(self):
        state = {'snapshot': self.store.snapshot(),
                 'cleared_at': datetime.now(timezone.utc).isoformat()}
        queue = self.queue
        with queue.db() as db:
            for key, value in [('context_window_boundary', state), ('context_token_anchor', None)]:
                db.execute('INSERT OR REPLACE INTO schedule VALUES (?,?,?,?)',
                           (queue.agent_id, queue.user_id, queue.key(key), json.dumps(value)))
            rows = db.execute('SELECT turn_id,payload FROM protocols WHERE agent=? AND user=?',
                              (queue.agent_id, queue.user_id)).fetchall()
            for turn_id, encoded in rows:
                protocol = json.loads(encoded)
                if protocol.get('status') != 'active':
                    continue
                try:
                    raw = self.store.get_record(turn_id)
                except KeyError:
                    continue
                source = raw.payload.get('channel_source', {})
                scope = digest([source.get(k) for k in ('platform_id', 'bot_id', 'group_id')])[:32]
                if scope == queue.scope:
                    protocol.update(status='context_reset', reset_at=state['cleared_at'])
                    db.execute('UPDATE protocols SET payload=? WHERE agent=? AND user=? AND turn_id=?',
                               (json.dumps(protocol, ensure_ascii=False), queue.agent_id, queue.user_id, turn_id))
        return state


class MemoryVisibility:
    """Follow exact provenance, including derived claims and assistant replies."""
    def __init__(self, store, scope, private=False):
        self.store, self.scope, self.private = store, scope, private
        self.cache = {}

    def allowed(self, record, seen=None):
        if self.private:
            return True
        if record.ref in self.cache:
            return self.cache[record.ref]
        # Personality and PAD are deliberately shared. They are not a second
        # channel transcript; the original claims still have source ACLs.
        if record.record_kind in {'persona_version', 'emotion_state'}:
            return True
        seen = set() if seen is None else seen
        if record.ref in seen:
            return True
        seen = {*seen, record.ref}
        source = record.payload.get('channel_source', {})
        if source.get('conversation_kind') == 'private' or record.payload.get('private_activity'):
            self.cache[record.ref] = False
            return False
        if source.get('platform_id') and source.get('group_id'):
            scope = digest([source.get(k) for k in ('platform_id', 'bot_id', 'group_id')])[:32]
            if scope != self.scope:
                self.cache[record.ref] = False
                return False
        refs = [*record.source_refs]
        for key in ('reply_to', 'event_refs', 'source_range', 'state_refs', 'input_refs'):
            value = record.payload.get(key, [])
            refs.extend([value] if isinstance(value, str) else value if isinstance(value, list) else [])
        refs.extend(s.get('message_ref') for s in record.payload.get('source_spans', []))
        for ref in refs:
            if not isinstance(ref, str) or '@' not in ref:
                continue
            key, revision = ref.rsplit('@', 1)
            try:
                parent = self.store.get_record(key, int(revision))
            except (KeyError, ValueError):
                # A derived claim with an unavailable source cannot be safely
                # projected into a less privileged conversation.
                self.cache[record.ref] = False
                return False
            if not self.allowed(parent, seen):
                self.cache[record.ref] = False
                return False
        self.cache[record.ref] = True
        return True
