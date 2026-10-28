"""Host-bound tools for the working view and scoped generated artifacts."""

from __future__ import annotations


def handlers(view, artifacts, scope, workspace, allowed_ref):
    """Aggregate extension tools without exposing scope/workspace to the model."""
    result = view.handlers(scope, workspace, allowed_ref)

    register_schema = {"type": "function", "function": {
        "name": "harness__artifact_register",
        "description": "将当前scope中已存在的产物登记为可追溯附件引用。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "media_type": {"type": "string"},
            "source_call_id": {"type": "string"}},
            "required": ["path", "media_type", "source_call_id"], "additionalProperties": False}}}
    read_schema = {"type": "function", "function": {
        "name": "harness__artifact_read",
        "description": "按当前scope读取已登记附件；文本内容有长度上限。",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 0, "maximum": 1000},
            "offset": {"type": "integer", "minimum": 0}},
            "required": ["id"], "additionalProperties": False}}}

    async def register(*, path, media_type, source_call_id):
        if not allowed_ref(str(source_call_id)):
            return {"error": "source_call_not_allowed"}
        try:
            return artifacts.register(source_call_id, scope, path, media_type)
        except (OSError, ValueError) as exc:
            return {"error": type(exc).__name__, "detail": str(exc)}

    async def read(*, id, max_chars=1000, offset=0):
        try:
            metadata = artifacts.resolve(scope, id)
        except (KeyError, OSError, ValueError) as exc:
            return {"error": type(exc).__name__, "detail": str(exc)}
        if metadata["media_type"].startswith("text/") or metadata["media_type"] in {
                "application/json", "application/xml", "application/yaml"}:
            try:
                size, start = max(0, min(1000, int(max_chars))), max(0, int(offset))
                with open(metadata["path"], "r", encoding="utf-8") as stream:
                    stream.seek(0)
                    content = stream.read()
                metadata["total_chars"] = len(content)
                metadata["content"] = content[start:start + size]
                metadata["offset"] = start
                metadata["next_offset"] = start + size if start + size < len(content) else None
            except (UnicodeError, OSError) as exc:
                metadata["content_error"] = type(exc).__name__
        return metadata

    result["harness__artifact_register"] = (register_schema, register)
    result["harness__artifact_read"] = (read_schema, read)
    return result

