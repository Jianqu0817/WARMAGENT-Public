"""Scoped storage for self-authored notes to revisit in conversation."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time


_KINDS = {"idea", "question", "reflection", "later"}
_MAX_LIST_LIMIT = 20
_SOURCE_REF = re.compile(r"^([^@\s]+)@([0-9]+)$")


class PersonalSpace:
    def __init__(self, path, allowed_source=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.allowed_source = allowed_source
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self):
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS personal_note (
                    scope TEXT NOT NULL,
                    note_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('idea','question','reflection','later')),
                    text TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    shared_at REAL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(scope, note_id)
                );
                CREATE INDEX IF NOT EXISTS personal_note_scope_recent
                    ON personal_note(scope, archived, shared_at, updated_at DESC);
            """)

    @staticmethod
    def _scope(scope):
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 256:
            raise ValueError("scope must be a nonempty string")
        return scope

    @staticmethod
    def _note_id(note_id):
        if not isinstance(note_id, str) or not note_id.strip() or len(note_id) > 128:
            raise ValueError("note_id must contain 1..128 characters")
        return note_id

    @staticmethod
    def _kind(kind):
        if not isinstance(kind, str) or kind not in _KINDS:
            raise ValueError("kind must be idea, question, reflection, or later")
        return kind

    @staticmethod
    def _text(text):
        if not isinstance(text, str) or not text.strip() or len(text) > 1200:
            raise ValueError("text must contain 1..1200 characters")
        return text

    def _source_ref(self, scope, source_ref):
        if not isinstance(source_ref, str):
            raise ValueError("source_ref must be a string")
        if not source_ref:
            return source_ref
        if len(source_ref) > 256:
            raise ValueError("source_ref must contain at most 256 characters")
        match = _SOURCE_REF.fullmatch(source_ref)
        if not match or int(match.group(2)) < 1:
            raise ValueError("source_ref must be id@positive_revision")
        if self.allowed_source is None or not self.allowed_source(scope, source_ref):
            raise ValueError("source_ref is not visible in this scope")
        return source_ref

    @staticmethod
    def _record(row):
        return {
            "scope": row["scope"],
            "note_id": row["note_id"],
            "kind": row["kind"],
            "text": row["text"],
            "source_ref": row["source_ref"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "shared_at": row["shared_at"],
            "archived": bool(row["archived"]),
        }

    def put(self, scope, note_id, kind, text, source_ref=""):
        scope = self._scope(scope)
        note_id = self._note_id(note_id)
        kind = self._kind(kind)
        text = self._text(text)
        source_ref = self._source_ref(scope, source_ref)
        now = time.time()
        duplicate = False
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT kind,text,source_ref,revision FROM personal_note WHERE scope=? AND note_id=?",
                (scope, note_id),
            ).fetchone()
            if old is None:
                normalized = ' '.join(text.split())
                day_start = ((now + 8 * 3600) // 86400) * 86400 - 8 * 3600
                candidates = db.execute('SELECT note_id,text FROM personal_note WHERE scope=? AND kind=? '
                    "AND source_ref=? AND archived=0 AND (source_ref!='' OR updated_at>=? AND updated_at<?) "
                    'ORDER BY updated_at DESC LIMIT 100',
                    (scope, kind, source_ref, day_start, day_start + 86400)).fetchall()
                match = next((row for row in candidates
                              if ' '.join(row['text'].split()) == normalized), None)
                if match:
                    note_id, duplicate = match['note_id'], True
                    old = db.execute('SELECT kind,text,source_ref,revision FROM personal_note '
                                     'WHERE scope=? AND note_id=?', (scope, note_id)).fetchone()
            if old is None:
                db.execute(
                    "INSERT INTO personal_note(scope,note_id,kind,text,source_ref,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (scope, note_id, kind, text, source_ref, now, now),
                )
            elif not duplicate and (old["kind"], old["text"], old["source_ref"]) != (kind, text, source_ref):
                db.execute(
                    "UPDATE personal_note SET kind=?,text=?,source_ref=?,updated_at=?,shared_at=NULL,archived=0 "
                    ",revision=revision+1 WHERE scope=? AND note_id=?",
                    (kind, text, source_ref, now, scope, note_id),
                )
            db.commit()
        result = self.read(scope, note_id, include_archived=True)
        if duplicate:
            result['duplicate_of'] = note_id
        return result

    def get(self, scope, note_id):
        """Read one note including archived notes, raising when it is absent."""
        record = self.read(scope, note_id, include_archived=True)
        if record is None:
            raise KeyError((scope, note_id))
        return record

    def read(self, scope, note_id, include_archived=False):
        scope = self._scope(scope)
        note_id = self._note_id(note_id)
        query = "SELECT * FROM personal_note WHERE scope=? AND note_id=?"
        params = [scope, note_id]
        if not include_archived:
            query += " AND archived=0"
        with closing(self._connect()) as db:
            row = db.execute(query, params).fetchone()
        return None if row is None else self._record(row)

    @staticmethod
    def _limit(limit, *, allow_zero=False):
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        minimum = 0 if allow_zero else 1
        return min(_MAX_LIST_LIMIT, max(minimum, limit))

    def list(self, scope, kind=None, limit=5, include_archived=False, *, recent=False):
        scope = self._scope(scope)
        limit = self._limit(limit)
        if kind is not None:
            kind = self._kind(kind)
        query = "SELECT * FROM personal_note WHERE scope=?"
        params = [scope]
        if not include_archived:
            query += " AND archived=0"
        if kind is not None:
            query += " AND kind=?"
            params.append(kind)
        query += (' ORDER BY updated_at DESC,note_id LIMIT ?' if recent else
                  ' ORDER BY archived ASC, (shared_at IS NOT NULL) ASC, updated_at DESC, note_id LIMIT ?')
        params.append(limit)
        with closing(self._connect()) as db:
            rows = db.execute(query, params).fetchall()
        return [self._record(row) for row in rows]

    def archive(self, scope, note_id):
        scope = self._scope(scope)
        note_id = self._note_id(note_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT 1 FROM personal_note WHERE scope=? AND note_id=?", (scope, note_id)
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE personal_note SET archived=1 WHERE scope=? AND note_id=?",
                    (scope, note_id),
                )
            db.commit()
        return row is not None

    def mark_shared(self, scope, note_ids, expected_revisions=None):
        scope = self._scope(scope)
        if isinstance(note_ids, (str, bytes)):
            raise ValueError("note_ids must be an iterable of note ids")
        try:
            ids = list(dict.fromkeys(self._note_id(note_id) for note_id in note_ids))
        except TypeError as exc:
            raise ValueError("note_ids must be an iterable of note ids") from exc
        if len(ids) > 100:
            raise ValueError("at most 100 note ids may be marked at once")
        if expected_revisions is not None:
            if not isinstance(expected_revisions, dict):
                raise ValueError("expected_revisions must be a mapping")
            for note_id, revision in expected_revisions.items():
                self._note_id(note_id)
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                    raise ValueError("expected revisions must be positive integers")
        now = time.time()
        marked = []
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            for note_id in ids:
                if expected_revisions is not None:
                    revision = expected_revisions.get(note_id)
                    if revision is None:
                        continue
                    cursor = db.execute(
                        "UPDATE personal_note SET shared_at=? "
                        "WHERE scope=? AND note_id=? AND revision=? AND shared_at IS NULL",
                        (now, scope, note_id, revision),
                    )
                else:
                    cursor = db.execute(
                        "UPDATE personal_note SET shared_at=? "
                        "WHERE scope=? AND note_id=? AND shared_at IS NULL",
                        (now, scope, note_id),
                    )
                if cursor.rowcount:
                    marked.append(note_id)
            db.commit()
        return marked

    @staticmethod
    def _context_json(notes):
        return json.dumps(
            {"label": "subjective notes only; self-authored, not facts or personality",
             "notes": notes},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def context(self, scope, limit=3, max_chars=1000, *, query=None, recent=False):
        limit = self._limit(limit, allow_zero=True)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            raise ValueError("max_chars must be an integer")
        max_chars = min(1000, max(0, max_chars))
        empty = self._context_json([])
        if max_chars < len(empty):
            return ""
        if limit == 0:
            return empty
        if query is None:
            candidates = self.list(scope, limit=limit, recent=recent)
        else:
            from .personal_relevance import select_personal
            with closing(self._connect()) as db:
                rows = db.execute('SELECT * FROM personal_note WHERE scope=? AND archived=0 '
                                  'ORDER BY updated_at DESC,note_id LIMIT 100', (self._scope(scope),)).fetchall()
            candidates = select_personal(query, [self._record(row) for row in rows], ('text',), limit)
        selected = []
        for note in candidates:
            item = {
                "note_id": note["note_id"],
                "revision": note["revision"],
                "kind": note["kind"],
                "shared": note["shared_at"] is not None,
                "text": note["text"],
            }
            candidate = self._context_json([*selected, item])
            if len(candidate) <= max_chars:
                selected.append(item)
                continue

            source = note["text"]
            low, high = 0, len(source)
            while low < high:
                middle = (low + high + 1) // 2
                shortened = {**item, "text": source[:middle], "truncated": True}
                candidate = self._context_json([*selected, shortened])
                if len(candidate) <= max_chars:
                    low = middle
                else:
                    high = middle - 1
            if low:
                selected.append({**item, "text": source[:low], "truncated": low < len(source)})
            break
        return self._context_json(selected)

    def handlers(self, scope):
        """Return one model tool bound to this scope; sharing stays host-controlled."""
        scope = self._scope(scope)
        name = "harness__personal_note"
        schema = {"type": "function", "function": {
            "name": name,
            "description": (
                "记下自己的想法、疑问、反思或想留到下次聊的事；这些只是自我随笔，"
                "不代表事实或人格。若记录外部观察，先用来源工具核验，不要杜撰来源。"
                "可按需展开完整有界便笺。"
                "写前查看近期内容；同一想法有新进展时更新原note_id，无新内容允许不写。"
                "重复写入会返回原note_id和duplicate_of，后续使用返回的ID。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["write", "list", "read", "archive"]},
                    "note_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "kind": {"type": "string", "enum": sorted(_KINDS)},
                    "text": {"type": "string", "minLength": 1, "maxLength": 1200},
                    "source_ref": {"type": "string", "maxLength": 256,
                                   "anyOf": [{"const": ""},
                                             {"pattern": r"^[^@\s]+@[0-9]*[1-9][0-9]*$"}]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST_LIMIT},
                    "order": {"type": "string", "enum": ["unshared_first", "recent"],
                              "description": "写前回看用recent；unshared_first用于选择待聊内容"},
                },
                "required": ["action"],
                "additionalProperties": False,
                "anyOf": [
                    {"properties": {"action": {"const": "write"}},
                     "required": ["action", "note_id", "kind", "text"]},
                    {"properties": {"action": {"const": "list"}}, "required": ["action"]},
                    {"properties": {"action": {"const": "read"}},
                     "required": ["action", "note_id"]},
                    {"properties": {"action": {"const": "archive"}},
                     "required": ["action", "note_id"]},
                ],
            },
        }}

        async def personal_note(action, note_id=None, kind=None, text=None, source_ref="", limit=5, order='unshared_first'):
            try:
                if action == "write":
                    return self.put(scope, note_id, kind, text, source_ref)
                if action == "list":
                    return self.list(scope, kind=kind, limit=limit, recent=order == 'recent')
                if action == "read":
                    return self.read(scope, note_id)
                if action == "archive":
                    return {"archived": self.archive(scope, note_id)}
                return {"error": "unknown_action"}
            except ValueError as exc:
                return {"error": "ValueError", "detail": str(exc)}

        return {name: (schema, personal_note)}
