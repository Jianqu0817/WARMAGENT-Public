import json
from types import SimpleNamespace

import pytest

from myatri.contracts import ModelResponse
from warmagent.reading_conversation import ReadingConversation


TOOL_SCHEMA = {"type": "function", "function": {
    "name": "harness__write", "description": "write fixture",
    "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                   "required": ["value"], "additionalProperties": False},
}}


class FakeStore:
    def __init__(self):
        self.records = {}
        self.users = []

    def append_message(self, role, text, *, message_id=None, **metadata):
        identifier = message_id or f"record-{len(self.records)}"
        if identifier in self.records:
            return self.records[identifier]
        record = SimpleNamespace(record_id=identifier, ref=identifier + "@1",
            created_at="2026-10-24T00:00:00+00:00", payload={"role": role, **metadata},
            body_markdown=text)
        self.records[identifier] = record
        if role == "user":
            self.users.append(record)
        return record

    def get_record(self, identifier):
        if identifier not in self.records:
            raise KeyError(identifier)
        return self.records[identifier]

    def list_records(self, kind):
        return self.users if kind == "raw_message" else []

    def flush_projection(self):
        pass


class FakeProtocolStore:
    def __init__(self):
        self.protocols = {}
        self.meta = {}

    def get_protocol(self, turn_id):
        value = self.protocols.get(turn_id)
        return json.loads(json.dumps(value)) if value else None

    def save_protocol(self, turn_id, value):
        self.protocols[turn_id] = json.loads(json.dumps(value))

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = value


class FakeContext:
    trace_every_turns = 1

    def __init__(self):
        self.reply_policy = SimpleNamespace(maximum=0, max_tokens=128,
            prepare=lambda value: value)
        self.budget = SimpleNamespace(output_reserve=1024, limit=8000, anchor=None,
            estimate=lambda messages, tools: 100,
            require=lambda messages, tools: None,
            calibrate=lambda messages, usage, tools: None)

    async def abuild_context(self, text, **kwargs):
        return [{"role": "user", "content": text}], "snapshot-1"

    def explain_context(self):
        return {"context_stats": {}, "preparation_timings": {}}


class FakeGateway:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.on_generate = None

    async def generate(self, request):
        self.requests.append(request.model_copy(deep=True))
        if self.on_generate:
            self.on_generate(len(self.requests), request)
        return self.responses.pop(0)


class FakeHandler:
    def __init__(self):
        self.calls = []
        self.on_call = None

        async def call(**kwargs):
            raise AssertionError("direct calls must use execute_call")

        async def execute_call(call_id, args):
            self.calls.append((call_id, args))
            if self.on_call:
                self.on_call(call_id, args)
            return {"value": "tool result", "host_receipt_id": call_id}

        call.execute_call = execute_call
        self.call = call


def conversation(tmp_path, gateway, handler, protocol=None, *, async_factory=True):
    value = ReadingConversation.__new__(ReadingConversation)
    value.__dict__.update({
        "store": FakeStore(), "gateway": gateway, "recall": object(),
        "context": FakeContext(), "on_message": None, "on_answer": None,
        "max_steps": 4, "enable_tools": True, "protocol_store": protocol or FakeProtocolStore(),
        "tool_handlers": {}, "reply_guard": None, "_lock": __import__('asyncio').Lock(),
        "thinking": True, "reasoning_effort": "high", "thinking_max_tokens": 1024,
        "thinking_timeout": 120,
    })
    if async_factory:
        async def factory(_raw):
            return {"harness__write": (TOOL_SCHEMA, handler.call)}
    else:
        def factory(_raw):
            return {"harness__write": (TOOL_SCHEMA, handler.call)}
    value.turn_tool_factory = factory
    return value


def tool_response(call_id="native-call-1"):
    return ModelResponse(provider="fixture", model="fixture-v1", content="",
        tool_calls=[{"id": call_id, "type": "function", "function": {
            "name": "harness__write", "arguments": '{"value":"hello"}'}}],
        provider_fields={"reasoning_content": "reasoning before direct tool"})


@pytest.mark.asyncio
async def test_async_factory_direct_tool_result_and_final_answer(tmp_path):
    handler = FakeHandler()
    gateway = FakeGateway(tool_response(), ModelResponse(provider="fixture", model="fixture-v1",
        content="完成。", provider_fields={"reasoning_content": "final reasoning"}))
    turn = conversation(tmp_path, gateway, handler)
    protocol = turn.protocol_store

    def check_pending(call_id, args):
        saved = protocol.get_protocol("turn-direct-1")
        assert saved["status"] == "active"
        assert saved["pending_tool"] == {"call_id": call_id, "name": "harness__write",
                                          "arguments": args, "direct": True}
        assert "tool" not in [message["role"] for message in saved["messages"]]
        assert saved["reasoning_content"] == "reasoning before direct tool"

    def check_saved_result(request_count, request):
        if request_count == 2:
            saved = protocol.get_protocol("turn-direct-1")
            assert saved["status"] == "active"
            assert not _missing_results(saved["messages"])
            assert json.loads(next(m["content"] for m in saved["messages"]
                                   if m.get("role") == "tool"))["host_receipt_id"] == "native-call-1"

    handler.on_call = check_pending
    gateway.on_generate = check_saved_result

    answer = await turn.respond("请写入临时内容", message_id="turn-direct-1")

    assert answer.body_markdown == "完成。"
    assert handler.calls == [("native-call-1", {"value": "hello"})]
    request_messages = gateway.requests[1].messages
    call = next(item for item in request_messages if item.get("tool_calls"))
    result = next(item for item in request_messages if item.get("role") == "tool")
    assert call["reasoning_content"] == "reasoning before direct tool"
    assert result["tool_call_id"] == "native-call-1"
    assert json.loads(result["content"])["value"] == "tool result"
    assert turn.protocol_store.get_protocol("turn-direct-1")["reasoning_content"] == "final reasoning"


@pytest.mark.asyncio
async def test_restart_reuses_unmatched_call_id_before_model_request(tmp_path):
    handler = FakeHandler()
    gateway = FakeGateway(ModelResponse(provider="fixture", model="fixture-v1",
        content="已恢复并完成。", provider_fields={"reasoning_content": "after recovery"}))
    protocol = FakeProtocolStore()
    turn = conversation(tmp_path, gateway, handler, protocol)
    raw = turn.store.append_message("user", "继续原请求", message_id="turn-recover-1")
    protocol.save_protocol(raw.record_id, {"status": "active", "snapshot": "snapshot-1", "steps": 1,
        "context_stats": {}, "reasoning_content": "reasoning before restart", "messages": [
            {"role": "user", "content": "继续原请求"},
            {"role": "assistant", "content": "", "reasoning_content": "reasoning before restart",
             "tool_calls": [{"id": "stable-native-call", "type": "function", "function": {
                 "name": "harness__write", "arguments": '{"value":"hello"}'}}]},
        ]})

    answer = await turn.respond("继续原请求", message_id="turn-recover-1")

    assert answer.body_markdown == "已恢复并完成。"
    assert handler.calls == [("stable-native-call", {"value": "hello"})]
    request_messages = gateway.requests[0].messages
    assert not _missing_results(request_messages)
    call = next(item for item in request_messages if item.get("tool_calls"))
    result = next(item for item in request_messages if item.get("role") == "tool")
    assert call["reasoning_content"] == "reasoning before restart"
    assert result["tool_call_id"] == "stable-native-call"
    assert json.loads(result["content"])["host_receipt_id"] == "stable-native-call"


def _missing_results(messages):
    returned = {message.get("tool_call_id") for message in messages if message.get("role") == "tool"}
    return [call for message in messages if message.get("role") == "assistant"
            for call in message.get("tool_calls", []) if call.get("id") not in returned]
