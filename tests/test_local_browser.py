import asyncio
import base64
import hashlib
import json

from warmagent.local_browser import LocalBrowser, handlers


class FakeCDP:
    def __init__(self):
        self.commands = []
        self.page = {"title": "Fixture", "url": "https://example.test/", "body": "fixture body"}
        self.fail_readback = False

    async def new_target(self, url):
        self.page["url"] = url
        return {"id": "tab-1", "webSocketDebuggerUrl": "ws://fixture/devtools/page/1"}

    async def command(self, websocket_url, method, params=None):
        self.commands.append((method, params or {}))
        if method == "Runtime.evaluate":
            expression = params["expression"]
            if expression.startswith("({title:"):
                if self.fail_readback:
                    return {"exceptionDetails": {"text": "synthetic readback failure"}}
                return {"result": {"value": dict(self.page)}}
            if "const body=document.body" in expression:
                return {"result": {"value": {**self.page, "text": "暖" * 1500,
                    "total_chars": 1500, "links": [{"text": "next", "href": "https://example.test/next"}]}}}
            if "el.click()" in expression:
                return {"result": {"value": {"clicked": True}}}
            if "dispatchEvent" in expression:
                # The implementation JSON-serializes the requested text into the expression.
                self.page["value"] = "输入值"
                return {"result": {"value": {"value": "输入值", **self.page}}}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"synthetic-png-bytes").decode("ascii")}
        return {}


def test_open_read_click_fill_and_execute_call_host_wrapping(tmp_path):
    transport = FakeCDP()
    browser = LocalBrowser({}, tmp_path, transport=transport)
    assert browser.transport is transport
    assert asyncio.run(browser.open("https://example.test/"))["tab_id"] == "tab-1"
    page = asyncio.run(browser.read("tab-1", offset=0, limit=1500))
    assert page["title"] == "Fixture" and page["total_chars"] == 1500
    assert len(page["text"].encode("utf-8")) <= 3800 and page["next_offset"] == len(page["text"])
    assert page["content_kind"] == "untrusted_page_data"
    assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 5800

    async def execute_local(name, args, call_id, perform):
        assert name == "harness__browser_click" and call_id == "click-1"
        return await perform()

    toolset = handlers(browser, execute_local)
    click_result = asyncio.run(toolset["harness__browser_click"][1].execute_call(
        "click-1", {"tabId": "tab-1", "selector": "button[data-x='quoted']"}))
    assert click_result["action"] == "click" and click_result["verified"]
    assert click_result["readback"]["url"] == "https://example.test/"
    click_expr = next(params["expression"] for method, params in transport.commands
                      if method == "Runtime.evaluate" and "el.click()" in params.get("expression", ""))
    assert json.dumps("button[data-x='quoted']", ensure_ascii=False) in click_expr

    async def execute_fill(name, args, call_id, perform):
        assert name == "harness__browser_fill"
        return await perform()

    fill_tool = handlers(browser, execute_fill)["harness__browser_fill"][1]
    filled = asyncio.run(fill_tool.execute_call("fill-1", {
        "tab_id": "tab-1", "selector": "input[name=q]", "text": "输入值"}))
    assert filled["verified"] and filled["readback"] == "输入值"
    assert filled["title"] == "Fixture" and filled["url"] == "https://example.test/"

    transport.fail_readback = True
    failed_readback = asyncio.run(browser.click("tab-1", "button"))
    assert failed_readback["error"] == "click_readback_failed"


def test_close_releases_only_owned_resources_and_failed_endpoint_session(tmp_path, monkeypatch):
    transports = []
    class OwnedProcess:
        def __init__(self):
            self.terminated = False
        def poll(self): return 0 if self.terminated else None
        def terminate(self): self.terminated = True
        def wait(self, timeout=None): return 0

    class ManagedTransport:
        def __init__(self, endpoint):
            self.closed = False
            transports.append(self)
        async def wait_ready(self): raise RuntimeError("browser_not_configured")
        async def close(self): self.closed = True

    import warmagent.local_browser as module
    monkeypatch.setattr(module, "CDPTransport", ManagedTransport)
    external = LocalBrowser({"browser_endpoint": "http://127.0.0.1:9222"}, tmp_path)
    try:
        asyncio.run(external._ensure())
    except RuntimeError:
        pass
    assert external.transport is None
    assert transports[0].closed

    local = LocalBrowser({}, tmp_path, transport=ManagedTransport("fake"))
    process = OwnedProcess()
    local._process = process
    asyncio.run(local.close())
    assert process.terminated and local.transport is None


def test_screenshot_writes_vision_manifest_and_hashes(tmp_path):
    browser = LocalBrowser({}, tmp_path / "var", transport=FakeCDP())
    asyncio.run(browser.open("https://example.test/"))
    result = asyncio.run(browser.screenshot("tab-1"))
    manifest = result["image_manifest"]
    body = open(manifest["file"], "rb").read()
    assert hashlib.sha256(body).hexdigest() == manifest["sha256"]
    record = json.loads(body)[0]
    image_data = open(record["file"], "rb").read()
    assert hashlib.sha256(image_data).hexdigest() == record["sha256"]
    assert manifest["image_count"] == 1 and record["media_type"] == "image/png"
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) < 5800


def test_browser_is_lazy_and_missing_chrome_returns_config_error(tmp_path, monkeypatch):
    import warmagent.local_browser as module

    calls = []
    monkeypatch.setattr(module, "_chrome_path", lambda settings: None)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    browser = LocalBrowser({}, tmp_path)
    assert calls == [] and browser.transport is None
    result = asyncio.run(browser.open("https://example.test/"))
    assert result == {"error": "browser_not_configured"}
    assert calls == []


def test_installed_edge_path_is_detected_after_chrome_candidates(tmp_path, monkeypatch):
    import warmagent.local_browser as module

    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_bytes(b"fixture executable path")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    assert module._chrome_path({}) == edge.resolve()

