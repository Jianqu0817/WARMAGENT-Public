import asyncio
import json

import pytest

from myatri.contracts import ModelRequest, ModelResponse
from myatri.providers import BudgetExceeded, ProviderError
from warmagent.artifacts import ArtifactStore
from warmagent.host_extensions import handlers
from warmagent.model_router import ModelRouter, load_model_router
from warmagent.workspace_view import WorkspaceView


def test_host_extensions_bind_scope_and_aggregate_tools(tmp_path):
    root = tmp_path / "attachments"
    root.mkdir()
    path = root / "summary.txt"
    path.write_text("hello artifact", encoding="utf-8")
    view = WorkspaceView(tmp_path / "view.sqlite3")
    store = ArtifactStore(tmp_path / "artifacts.sqlite3", root)
    tools = handlers(view, store, "scope-a", "chat", lambda ref: ref == "call-ok")
    assert set(("harness__workspace_list", "harness__artifact_register", "harness__artifact_read")) <= set(tools)
    register = tools["harness__artifact_register"][1]
    result = asyncio.run(register(path=str(path), media_type="text/plain", source_call_id="call-other"))
    assert result["error"] == "source_call_not_allowed"
    result = asyncio.run(register(path=str(path), media_type="text/plain", source_call_id="call-ok"))
    read = asyncio.run(tools["harness__artifact_read"][1](id=result["id"]))
    assert read["resolved_scope"] == "scope-a" and read["content"] == "hello artifact"


def test_artifact_text_read_pages_chinese_content(tmp_path):
    root = tmp_path / "attachments"
    root.mkdir()
    path = root / "chinese.txt"
    path.write_text("暖" * 1500, encoding="utf-8")
    store = ArtifactStore(tmp_path / "artifacts.sqlite3", root)
    registered = store.register("call-ok", "scope-a", path, "text/plain")
    tools = handlers(WorkspaceView(tmp_path / "view.sqlite3"), store,
                     "scope-a", "chat", lambda ref: ref == "call-ok")
    read_tool = tools["harness__artifact_read"][1]
    page1 = asyncio.run(read_tool(id=registered["id"], max_chars=1000))
    page2 = asyncio.run(read_tool(id=registered["id"], max_chars=1000,
                                  offset=page1["next_offset"]))
    assert len(page1["content"]) == 1000 and len(page1["content"].encode("utf-8")) == 3000
    assert page1["total_chars"] == 1500 and page1["next_offset"] == 1000
    assert page2["content"] == "暖" * 500 and page2["next_offset"] is None


class GatewayStub:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.seen = value, error, []

    async def generate(self, request):
        self.seen.append(request)
        if self.error:
            raise self.error
        return self.value


def test_model_router_config_routes_same_request_and_providererror_failover(tmp_path):
    fallback_response = ModelResponse(content="ok", provider="fallback-provider", model="fallback-model")
    base = GatewayStub(error=ProviderError("incompatible endpoint"))
    vision = GatewayStub(value=ModelResponse(content="vision", provider="v", model="vision-v1"))
    fallback = GatewayStub(value=fallback_response)
    router = ModelRouter(base, {"vision": vision, "fallback": fallback}, {
        "routes": {"default": {"gateway": "base", "failover": ["fallback"]},
                   "vision": "vision"}})
    request = ModelRequest(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": "x"}]}])
    response = asyncio.run(router.generate(request))
    assert response.provider == "v" and response.model == "vision-v1"
    assert vision.seen == [request]

    text_request = ModelRequest(messages=[{"role": "user", "content": "hello"}])
    response = asyncio.run(router.generate(text_request))
    assert response.provider == "fallback-provider" and response.model == "fallback-model"
    assert base.seen == [text_request] and fallback.seen == [text_request]


def test_model_router_does_not_fallback_on_non_providererror_and_missing_config_is_noop(tmp_path):
    base = GatewayStub(error=ValueError("local bug"))
    base.used_tokens = 123
    base.provider = object()
    fallback = GatewayStub(value=ModelResponse(content="should not run", provider="f", model="f"))
    router = ModelRouter(base, {"fallback": fallback}, {
        "routes": {"default": {"gateway": "base", "failover": ["fallback"]}}})
    assert router.used_tokens == 123 and router.provider is base.provider
    request = ModelRequest(messages=[{"role": "user", "content": "x"}])
    with pytest.raises(ValueError):
        asyncio.run(router.generate(request))
    assert fallback.seen == []
    assert load_model_router(base, tmp_path / "absent.toml") is base


def test_model_router_gateway_factory_is_lazy_secret_redacted_and_missing_target_fails(tmp_path):
    base = GatewayStub(value=ModelResponse(content="base", provider="base", model="base-v1"))
    config = tmp_path / "router.toml"
    config.write_text('''[model_router.routes]
default = "vision"
[model_router.gateways.vision]
model = "vision-v1"
endpoint = "http://127.0.0.1:9000"
api_key = "do-not-pass"
[model_router.gateways.vision.auth]
token = "also-do-not-pass"
''', encoding="utf-8")
    observed = []
    vision = GatewayStub(value=ModelResponse(content="vision", provider="fixture", model="vision-v1"))

    def factory(name, spec):
        observed.append((name, spec))
        return vision

    router = load_model_router(base, config, gateway_factory=factory)
    assert router.routes["default"] is vision
    assert observed == [("vision", {"model": "vision-v1", "endpoint": "http://127.0.0.1:9000", "auth": {}})]
    with pytest.raises(ValueError, match="gateway_not_configured:vision"):
        load_model_router(base, config)


def test_router_recognizes_tool_receipt_images_but_never_fails_over_budget(tmp_path):
    vision = GatewayStub(value=ModelResponse(content="vision", provider="vision", model="vision-v1"))
    base = GatewayStub(value=ModelResponse(content="base", provider="base", model="base-v1"))
    router = ModelRouter(base, {"vision": vision}, {
        "routes": {"default": "base", "vision": "vision"}})
    request = ModelRequest(messages=[{"role": "tool", "tool_call_id": "shot",
                                     "content": json.dumps({"image_manifest": {"file": "fixture"}})}])
    response = asyncio.run(router.generate(request))
    assert response.provider == "vision" and vision.seen == [request]

    for message in (
            {"role": "tool", "content": "{malformed"},
            {"role": "user", "content": 'please inspect "image_manifest"'}):
        ordinary = ModelRequest(messages=[message])
        assert asyncio.run(router.generate(ordinary)).provider == "base"

    exhausted = GatewayStub(error=BudgetExceeded("no budget"))
    fallback = GatewayStub(value=ModelResponse(content="should not run", provider="f", model="f"))
    limited = ModelRouter(exhausted, {"fallback": fallback}, {
        "routes": {"default": {"gateway": "base", "failover": ["fallback"]}}})
    with pytest.raises(BudgetExceeded):
        asyncio.run(limited.generate(ModelRequest(messages=[{"role": "user", "content": "hi"}])))
    assert fallback.seen == []

