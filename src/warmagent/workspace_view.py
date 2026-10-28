"""Small, revisioned annotations over authoritative tool receipts.

The database stores references and editorial state only. Receipt bodies remain in
the existing host receipt store and are read through its existing tools.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from typing import Callable

from .context_lifecycle import Audience, Lifecycle


class WorkspaceConflict(RuntimeError):
    """Raised when a caller's workspace revision is stale."""


class WorkspaceView:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS workspace_views (
                scope TEXT NOT NULL, workspace TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                current_goal TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'auto',
                PRIMARY KEY(scope,workspace))""")
            view_columns = {
                row[1] for row in db.execute("PRAGMA table_info(workspace_views)")
            }
            if "mode" not in view_columns:
                db.execute(
                    "ALTER TABLE workspace_views ADD COLUMN mode TEXT NOT NULL DEFAULT 'auto'"
                )
            db.execute("""CREATE TABLE IF NOT EXISTS workspace_items (
                scope TEXT NOT NULL, workspace TEXT NOT NULL, ref TEXT NOT NULL,
                lifecycle TEXT NOT NULL, audience TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                payload_ref TEXT, observed INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
                folded INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
                has_image INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(scope,workspace,ref),
                FOREIGN KEY(scope,workspace) REFERENCES workspace_views(scope,workspace))""")
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(workspace_items)")
            }
            if "has_image" not in columns:
                db.execute(
                    "ALTER TABLE workspace_items ADD COLUMN has_image INTEGER NOT NULL DEFAULT 0"
                )
            db.execute('CREATE TABLE IF NOT EXISTS workspace_selection ('
                'scope TEXT NOT NULL, base TEXT NOT NULL, selected TEXT NOT NULL, PRIMARY KEY(scope,base))')

    def active_workspace(self, scope, base):
        with self._db() as db:
            row = db.execute('SELECT selected FROM workspace_selection WHERE scope=? AND base=?', (scope, base)).fetchone()
        return row[0] if row else base

    def select(self, scope, base, branch_id):
        with self._db() as db:
            if not db.execute('SELECT 1 FROM workspace_views WHERE scope=? AND workspace=?', (scope, branch_id)).fetchone():
                raise KeyError('workspace not found in scope')
            db.execute('INSERT OR REPLACE INTO workspace_selection VALUES(?,?,?)', (scope, base, branch_id))
        return {'workspace': branch_id}

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _ensure(db, scope, workspace):
        db.execute(
            "INSERT OR IGNORE INTO workspace_views(scope,workspace) VALUES(?,?)",
            (str(scope), str(workspace)),
        )

    def register_context(
        self,
        scope,
        workspace,
        ref,
        *,
        lifecycle="durable",
        audience="ai",
        summary="",
        payload_ref=None,
        observed=False,
        has_image=False,
    ):
        """Register a receipt reference and metadata, never its raw body."""
        lifecycle, audience = Lifecycle(lifecycle).value, Audience(audience).value
        with self._db() as db, db:
            self._ensure(db, scope, workspace)
            db.execute(
                """INSERT INTO workspace_items(scope,workspace,ref,lifecycle,audience,
                summary,payload_ref,observed,has_image) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(scope,workspace,ref) DO UPDATE SET lifecycle=excluded.lifecycle,
                audience=excluded.audience,summary=excluded.summary,payload_ref=excluded.payload_ref,
                observed=MAX(workspace_items.observed,excluded.observed),
                has_image=MAX(workspace_items.has_image,excluded.has_image)""",
                (
                    scope,
                    workspace,
                    str(ref),
                    lifecycle,
                    audience,
                    str(summary),
                    payload_ref,
                    int(bool(observed)),
                    int(bool(has_image)),
                ),
            )

    def contains(self, scope, workspace, ref):
        """Check a full ref or a call-id suffix without loading the workspace list."""
        value = str(ref or "")
        if not value:
            return False
        with self._db() as db:
            row = db.execute(
                """SELECT 1 FROM workspace_items
                WHERE scope=? AND workspace=? AND
                (ref=? OR substr(ref,-length(?))=?) LIMIT 1""",
                (str(scope), str(workspace), value, ":" + value, ":" + value),
            ).fetchone()
        return row is not None

    def projection(self, scope, workspace, turn):
        with self._db() as db:
            head = db.execute(
                "SELECT revision,current_goal,mode FROM workspace_views WHERE scope=? AND workspace=?",
                (scope, workspace),
            ).fetchone()
            rows = db.execute(
                "SELECT ref,pinned,note FROM workspace_items WHERE scope=? AND workspace=? "
                "AND (pinned=1 AND substr(ref,1,length(?))=? OR note!='') ORDER BY pinned DESC,ref LIMIT 20",
                (scope, workspace, turn + ":", turn + ":"),
            ).fetchall()
        return {
            **(
                dict(head)
                if head
                else {"revision": 0, "current_goal": "", "mode": "auto"}
            ),
            "items": [dict(row) for row in rows],
        }

    def _change(
        self, scope, workspace, expected_revision, sql, params=(), *, mode=None
    ):
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure(db, scope, workspace)
            row = db.execute(
                "SELECT revision FROM workspace_views WHERE scope=? AND workspace=?",
                (scope, workspace),
            ).fetchone()
            if expected_revision is None or int(expected_revision) != row["revision"]:
                raise WorkspaceConflict(f"workspace revision is {row['revision']}")
            db.execute(sql, (*params, scope, workspace))
            if mode is not None:
                db.execute(
                    "UPDATE workspace_views SET mode=? WHERE scope=? AND workspace=?",
                    (mode, scope, workspace),
                )
            db.execute(
                "UPDATE workspace_views SET revision=revision+1 WHERE scope=? AND workspace=?",
                (scope, workspace),
            )
            return row["revision"] + 1

    def pin(self, scope, workspace, ref, expected_revision):
        return self._change(
            scope,
            workspace,
            expected_revision,
            "UPDATE workspace_items SET pinned=1 WHERE ref=? AND scope=? AND workspace=?",
            (str(ref),),
        )

    def unpin(self, scope, workspace, ref, expected_revision):
        return self._change(
            scope,
            workspace,
            expected_revision,
            "UPDATE workspace_items SET pinned=0 WHERE ref=? AND scope=? AND workspace=?",
            (str(ref),),
        )

    def fold(self, scope, workspace, ref, expected_revision):
        with self._db() as db:
            row = db.execute(
                "SELECT lifecycle,audience,observed,summary,payload_ref,has_image FROM workspace_items "
                "WHERE scope=? AND workspace=? AND ref=?",
                (scope, workspace, str(ref)),
            ).fetchone()
        if not row:
            raise KeyError(ref)
        if not row["observed"]:
            raise ValueError("context must be observed before folding")
        # Errors/unknowns and image references always remain visible.
        marker = (row["summary"] + " " + str(row["payload_ref"] or "")).casefold()
        if row["has_image"] or any(
            word in marker for word in ("unknown", '"error"', "image_manifest")
        ):
            raise ValueError("error, unknown, or image context cannot be folded")
        return self._change(
            scope,
            workspace,
            expected_revision,
            "UPDATE workspace_items SET folded=1 WHERE ref=? AND scope=? AND workspace=?",
            (str(ref),),
            mode="fold",
        )

    def unfold(self, scope, workspace, ref, expected_revision):
        return self._change(
            scope,
            workspace,
            expected_revision,
            "UPDATE workspace_items SET folded=0 WHERE ref=? AND scope=? AND workspace=?",
            (str(ref),),
            mode="expand",
        )

    def set_mode(self, scope, workspace, mode, expected_revision):
        if mode not in {"auto", "fold", "expand"}:
            raise ValueError("mode must be auto, fold, or expand")
        revision = self._change(
            scope,
            workspace,
            expected_revision,
            "UPDATE workspace_views SET mode=? WHERE scope=? AND workspace=?",
            (mode,),
        )
        return revision

    def note(self, scope, workspace, target, text, expected_revision):
        if target == "current_goal":
            return self._change(
                scope,
                workspace,
                expected_revision,
                "UPDATE workspace_views SET current_goal=? WHERE scope=? AND workspace=?",
                (str(text),),
            )
        return self._change(
            scope,
            workspace,
            expected_revision,
            "UPDATE workspace_items SET note=? WHERE ref=? AND scope=? AND workspace=?",
            (str(text), str(target)),
        )

    def fork(self, scope, workspace, branch_id, expected_revision):
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure(db, scope, workspace)
            row = db.execute(
                "SELECT revision,current_goal,mode FROM workspace_views WHERE scope=? AND workspace=?",
                (scope, workspace),
            ).fetchone()
            if expected_revision is None or row["revision"] != int(expected_revision):
                raise WorkspaceConflict(f"workspace revision is {row['revision']}")
            db.execute(
                "INSERT INTO workspace_views(scope,workspace,revision,current_goal,mode) VALUES(?,?,0,?,?)",
                (scope, branch_id, row["current_goal"], row["mode"]),
            )
            db.execute(
                """INSERT INTO workspace_items SELECT scope,?,ref,lifecycle,audience,summary,payload_ref,
                observed,pinned,folded,note,has_image FROM workspace_items WHERE scope=? AND workspace=?""",
                (branch_id, scope, workspace),
            )
        return {"scope": scope, "workspace": branch_id, "revision": 0}

    def list(self, scope, workspace, *, max_items=10, offset=0):
        start, size = max(0, int(offset)), max(1, min(50, int(max_items)))
        with self._db() as db:
            self._ensure(db, scope, workspace)
            head = db.execute(
                "SELECT revision,current_goal,mode FROM workspace_views WHERE scope=? AND workspace=?",
                (scope, workspace),
            ).fetchone()
            total = db.execute(
                "SELECT COUNT(*) FROM workspace_items WHERE scope=? AND workspace=?",
                (scope, workspace),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT ref,lifecycle,audience,summary,payload_ref,observed,pinned,folded,note,has_image "
                "FROM workspace_items WHERE scope=? AND workspace=? ORDER BY pinned DESC,ref LIMIT ? OFFSET ?",
                (scope, workspace, size, start),
            ).fetchall()
        return {
            "scope": scope,
            "workspace": workspace,
            "revision": head["revision"],
            "mode": head["mode"],
            "current_goal": head["current_goal"],
            "items": [dict(row) for row in rows],
            "offset": start,
            "next_offset": start + size if start + size < total else None,
            "total_items": total,
        }

    def describe(self, scope, workspace):
        view = self.list(scope, workspace)
        result = {
            key: view[key]
            for key in ("scope", "workspace", "revision", "mode", "current_goal")
        }
        result['current_goal'] = result['current_goal'][:400]
        return result

    def compile(self, scope, workspace, max_chars=4000):
        view = self.list(scope, workspace)
        lines = [f"目标: {view['current_goal']}" if view["current_goal"] else ""]
        for item in view["items"]:
            # Folded items remain represented by their ref; full content is fetched via receipt tool.
            status = "已折叠" if item["folded"] else "展开"
            lines.append(
                f"[{item['ref']}] ({status}) "
                + ("" if item["folded"] else item["summary"])
                + (f"（注：{item['note']}）" if item["note"] else "")
            )
        return "\n".join(line for line in lines if line)[: max(0, int(max_chars))]

    def list_page(self, scope, workspace, *, max_items=10, offset=0):
        page = self.list(scope, workspace, max_items=max_items, offset=offset)
        page["current_goal"] = page["current_goal"][:400]
        original_count = len(page["items"])
        while (
            len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode())
            > 5500
            and page["items"]
        ):
            page["items"].pop()
        if len(page["items"]) != original_count:
            page["next_offset"] = offset + len(page["items"])
        return page

    def handlers(self, scope, workspace, allowed_ref: Callable[[str], bool]):
        """Return compact host-bound handlers; scope/workspace never come from model args."""

        base_workspace = workspace
        async def invoke(action, **args):
            workspace = self.active_workspace(scope, base_workspace)
            ref = args.get("ref") or args.get("target")
            if ref and ref != "current_goal" and not allowed_ref(str(ref)):
                return {"error": "ref_not_allowed"}
            try:
                if action == 'select':
                    return self.select(scope, base_workspace, args['branch_id'])
                if action == "describe":
                    return self.describe(scope, workspace)
                if action == "list":
                    return self.list_page(
                        scope,
                        workspace,
                        max_items=args.get("max_items", 10),
                        offset=args.get("offset", 0),
                    )
                if action == "compile":
                    return {
                        "text": self.compile(
                            scope, workspace, min(args.get("max_chars", 1000), 1000)
                        )
                    }
                if action == "pin":
                    revision = self.pin(scope, workspace, ref, args["revision"])
                elif action == "unpin":
                    revision = self.unpin(scope, workspace, ref, args["revision"])
                elif action == "fold":
                    revision = self.fold(scope, workspace, ref, args["revision"])
                elif action == "unfold":
                    revision = self.unfold(scope, workspace, ref, args["revision"])
                elif action == "mode":
                    revision = self.set_mode(
                        scope, workspace, args["mode"], args["revision"]
                    )
                elif action == "note":
                    revision = self.note(
                        scope, workspace, args["target"], args["text"], args["revision"]
                    )
                elif action == "fork":
                    return self.fork(
                        scope, workspace, args["branch_id"], args["revision"]
                    )
                else:
                    return {"error": "unknown_action"}
                return {"revision": revision}
            except (KeyError, ValueError, WorkspaceConflict) as exc:
                return {"error": type(exc).__name__, "detail": str(exc)}

        schemas = {}
        for action in (
            "describe",
            "list",
            "compile",
            "pin",
            "unpin",
            "fold",
            "unfold",
            "mode",
            "note",
            "fork",
            "select",
        ):
            properties = {"revision": {"type": "integer"}}
            required = ["revision"]
            if action in ("pin", "unpin", "fold", "unfold"):
                properties["ref"] = {"type": "string"}
                required.append("ref")
            if action == "note":
                properties.update(
                    target={"type": "string"},
                    text={"type": "string", "maxLength": 1200},
                )
                required += ["target", "text"]
            if action in {"fork", "select"}:
                properties["branch_id"] = {"type": "string", "maxLength": 160}
                required.append("branch_id")
            if action == "compile":
                properties["max_chars"] = {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1000,
                }
            if action == "list":
                properties.update(
                    max_items={"type": "integer", "minimum": 1, "maximum": 50},
                    offset={"type": "integer", "minimum": 0},
                )
            if action == "mode":
                properties["mode"] = {
                    "type": "string",
                    "enum": ["auto", "fold", "expand"],
                }
                required.append("mode")
            if action in ("describe", "list", "compile"):
                required = []
            if action == 'select':
                required = ['branch_id']
            name = "harness__workspace_" + action
            schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "读取或调整当前工作视图；原始内容须通过receipt工具读取。",
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            }

            async def handler(_action=action, **kwargs):
                return await invoke(_action, **kwargs)

            schemas[name] = (schema, handler)
        return schemas
