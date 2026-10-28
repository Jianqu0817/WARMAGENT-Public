"""Scoped artifact references and audience-targeted runtime notifications."""

from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .runtime_events import EventJournal


class ArtifactStore:
    def __init__(self, path: str | Path, root: str | Path, *, allow_external=False):
        self.path, self.root = Path(path), Path(root).resolve()
        self.allow_external = bool(allow_external)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS artifacts(
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, source_call_id TEXT NOT NULL,
                path TEXT NOT NULL, media_type TEXT NOT NULL, content_hash TEXT NOT NULL,
                revision INTEGER NOT NULL, UNIQUE(scope,source_call_id,path))""")
            db.execute("""CREATE TABLE IF NOT EXISTS artifact_shares(
                artifact_id TEXT NOT NULL REFERENCES artifacts(id), target_scope TEXT NOT NULL,
                PRIMARY KEY(artifact_id,target_scope))""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _path(self, path):
        resolved = Path(path).resolve()
        if not self.allow_external and not resolved.is_relative_to(self.root):
            raise ValueError("artifact path must remain under configured root")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved

    @staticmethod
    def _hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def register(self, source_call_id, scope, path, media_type):
        resolved = self._path(path)
        digest = self._hash(resolved)
        stored_path = (str(resolved) if not resolved.is_relative_to(self.root)
                       else str(resolved.relative_to(self.root)))
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,content_hash,revision FROM artifacts WHERE scope=? AND source_call_id=? AND path=?",
                             (str(scope), str(source_call_id), stored_path)).fetchone()
            if row:
                revision = row["revision"] + int(row["content_hash"] != digest)
                db.execute("UPDATE artifacts SET media_type=?,content_hash=?,revision=? WHERE id=?",
                           (str(media_type), digest, revision, row["id"]))
                artifact_id = row["id"]
            else:
                artifact_id, revision = uuid.uuid4().hex, 1
                db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                           (artifact_id, str(scope), str(source_call_id), stored_path, str(media_type), digest, revision))
        return {"id": artifact_id, "hash": digest, "revision": revision}

    def resolve(self, scope, artifact_id):
        with self._db() as db:
            row = db.execute("""SELECT a.* FROM artifacts a WHERE a.id=? AND
                (a.scope=? OR EXISTS(SELECT 1 FROM artifact_shares s WHERE s.artifact_id=a.id AND s.target_scope=?))""",
                             (str(artifact_id), str(scope), str(scope))).fetchone()
        if row is None:
            raise KeyError("artifact not found in scope")
        registered_path = Path(row["path"])
        path = self._path(registered_path if registered_path.is_absolute() else self.root / registered_path)
        current_hash = self._hash(path)
        return {"id": row["id"], "scope": row["scope"], "resolved_scope": str(scope),
                "source_call_id": row["source_call_id"],
                "path": str(path), "media_type": row["media_type"], "hash": row["content_hash"],
                "revision": row["revision"], "changed": current_hash != row["content_hash"],
                "current_hash": current_hash}

    def share(self, from_scope, artifact_id, target_scope):
        """Explicitly authorize a second scope to resolve the same source reference."""
        with self._db() as db, db:
            row = db.execute("SELECT id FROM artifacts WHERE id=? AND scope=?",
                             (str(artifact_id), str(from_scope))).fetchone()
            if row is None:
                raise KeyError("artifact not found in source scope")
            db.execute("INSERT OR IGNORE INTO artifact_shares(artifact_id,target_scope) VALUES(?,?)",
                       (str(artifact_id), str(target_scope)))
        return {"id": str(artifact_id), "scope": str(target_scope), "source_scope": str(from_scope)}


class Notifications:
    """A thin audience layer over EventJournal's durable, leased inbox."""
    AUDIENCES = {"ai", "human", "shared"}

    def __init__(self, path: str | Path):
        self.journal = EventJournal(path)
        self.path = Path(path)

    def emit(self, *, scope, audience, kind, dedup_key, payload_ref="", source="workspace"):
        if audience not in self.AUDIENCES:
            raise ValueError("unsupported audience")
        payload = json.dumps({"audience": audience, "payload_ref": payload_ref},
                             ensure_ascii=False, separators=(",", ":"))
        return self.journal.publish(kind=kind, scope=scope, source="notify:" + source,
                                    dedup_key=f"{audience}:{dedup_key}", payload_ref=payload)

    def get(self, *, scope, audience, subscriber_token):
        if audience not in self.AUDIENCES or not subscriber_token:
            raise ValueError("audience and subscriber token are required")
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT event_id,payload_ref FROM runtime_events WHERE scope=? AND source LIKE 'notify:%' ORDER BY revision",
                              (scope,)).fetchall()
        for event_id, payload_ref in rows:
            try:
                payload = json.loads(payload_ref)
            except (TypeError, ValueError):
                continue
            if payload.get("audience") != audience:
                continue
            consumer = f"notify:{audience}:{subscriber_token}"
            claim = self.journal.claim(consumer, str(scope), event_id=event_id)
            if claim:
                claim["payload"] = payload
                return claim
        return None

    def ack(self, *, scope, event_id, subscriber_token, claim_token):
        # EventJournal's CAS token prevents stale subscribers from acknowledging a re-claim.
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT payload_ref FROM runtime_events WHERE event_id=? AND scope=? AND source LIKE 'notify:%'",
                             (event_id, scope)).fetchone()
        if not row or json.loads(row[0]).get("audience") not in self.AUDIENCES:
            return False
        audience = json.loads(row[0])["audience"]
        return self.journal.ack(event_id, f"notify:{audience}:{subscriber_token}", str(claim_token))

    def acknowledge_reference(self, scope, audience, payload_ref, subscriber='main_model'):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT event_id,payload_ref FROM runtime_events WHERE scope=? AND source LIKE 'notify:%'",
                (scope,)).fetchall()
        for event_id, encoded in rows:
            payload = json.loads(encoded)
            if payload.get('audience') != audience or payload.get('payload_ref') != payload_ref:
                continue
            consumer = f'notify:{audience}:{subscriber}'
            claim = self.journal.claim(consumer, scope, event_id=event_id)
            if claim:
                self.journal.ack(event_id, consumer, claim['claim_token'])

