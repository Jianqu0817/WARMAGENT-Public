"""Real plugin + native host; synthetic Bili responses, no external requests."""
import asyncio
import json
from pathlib import Path

import pytest

from warmagent.direct_harness import DirectHarness
from test_direct_harness import owner_raw, settings


@pytest.mark.asyncio
async def test_bili_native_read_source_and_disabled_side_effects(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    node = root / "var/harness/node/node.exe"
    runtime = root / "var/harness/runtime"
    if not node.is_file() or not (runtime / "node_modules/dsh-bili-humanism").is_dir():
        pytest.skip("locked Bili Harness runtime is not installed")

    # Replace fetch inside this test process, including unexpected requests.
    # The real tool-host, plugin, reader, SQLite cache and receipts still run.
    fetch_log = tmp_path / "fetches.jsonl"
    fixture = tmp_path / "bili-fixture.mjs"
    fixture.write_text("""
import { appendFileSync } from 'node:fs';
const fixtures = {
  '/x/web-interface/popular': {code:0, data:{list:[{bvid:'BV1234567890', title:'Synthetic popular',
    owner:{name:'Fixture author'}, duration:20}]}},
  '/x/web-interface/view': {code:0, data:{title:'Synthetic video', owner:{name:'Fixture author'},
    pages:[{page:1,cid:123,duration:20}]}},
  '/x/player/wbi/v2': {code:0, data:{subtitle:{subtitles:[{lan:'zh-CN',
    subtitle_url:'https://aisubtitle.hdslb.com/fixture.json'}]}}},
  '/fixture.json': {body:[{from:0,to:10,content:'Synthetic transcript segment.'},
    {from:10,to:20,content:'Second transcript segment.'}]},
};
globalThis.fetch = async (url, options) => {
  appendFileSync(LOG, JSON.stringify({url:String(url),method:options.method,
    headers:options.headers})+'\\n');
  const body=fixtures[new URL(url).pathname];
  if (!body || options.method !== 'GET') throw Error('Unexpected network request');
  return new Response(JSON.stringify(body),{status:200});
};
""".replace("LOG", json.dumps(str(fetch_log))), encoding="utf-8")
    create_process = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        return await create_process(args[0], "--import", fixture.as_uri(), *args[1:], **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    config = json.loads((root / "config/harness/executor.example.json").read_text(encoding="utf-8"))
    config_file = tmp_path / "native.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")
    harness = DirectHarness({**settings(), "node_path": str(node), "runtime_root": str(runtime),
        "project_root": str(tmp_path), "native_config": str(config_file)},
        tmp_path / "receipts", "owner-1", ["wechat|private-1"], "live")

    async def call(handlers, name, args, call_id):
        receipt = await handlers["harness__bili_humanism_" + name][1].execute_call(call_id, args)
        if receipt.get("result_complete") is False:
            receipt = json.loads(Path(receipt["full_receipt"]["file"]).read_text(encoding="utf-8"))
        assert "error" not in receipt
        assert receipt["result"]["isError"] is False
        return json.loads(receipt["result"]["value"])

    try:
        handlers = await harness.handlers(owner_raw(), context={"scope": "test", "turn_id": "bili"})
        assert "harness__bili_humanism_read" in handlers
        for disabled in ("history", "favorite", "like", "coin", "personality"):
            assert "harness__bili_humanism_" + disabled not in handlers
        stranger = owner_raw()
        stranger["channel_source"]["sender_id"] = "stranger"
        assert await harness.handlers(stranger, context={"turn_id": "denied"}) == {}

        guide = await call(handlers, "guide", {}, "guide")
        assert isinstance(guide, str) and guide
        status = await call(handlers, "status", {}, "status")
        assert status["personalityLearning"]["enabled"] is False
        assert status["dailyFetching"]["enabled"] is False
        assert not fetch_log.exists()
        candidates = await call(handlers, 'candidates', {'count': 1}, 'candidates')
        assert candidates['result'][0]['bvid'] == 'BV1234567890'
        assert candidates['result'][0]['url'] == 'https://www.bilibili.com/video/BV1234567890/'
        reading = await call(handlers, "read", {"video": "BV1234567890"}, "read")
        assert reading["result"]["route"] == "A1"
        assert reading["visual"]["status"] == "unavailable"
        assert "Synthetic transcript segment." in json.dumps(reading)
        reading_id = reading["result"]["id"]
        source = await call(handlers, "source", {"readingId": reading_id}, "source")
        assert "Synthetic transcript segment." in json.dumps(source)
        tail = await call(handlers, "source", {"readingId": reading_id, "offset": 1}, "source-tail")
        assert "Second transcript segment." in json.dumps(tail)
        assert "Synthetic transcript segment." not in json.dumps(tail)
        assert await call(handlers, "read", {"video": "BV1234567890"}, "read") == reading
        # A new call uses the plugin's source cache as well as host call-ID deduplication.
        await call(handlers, "read", {"video": "BV1234567890"}, "read-cache")
        fetches = [json.loads(line) for line in fetch_log.read_text().splitlines()]
        assert len(fetches) == 4
        assert all(row["method"] == "GET" for row in fetches)
        assert "cookie" not in json.dumps(fetches).lower()
        assert (tmp_path / "var/bili-humanism").is_dir()
    finally:
        await harness.close()
