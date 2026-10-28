import hashlib
import json
from pathlib import Path
import sys

from PIL import Image
import pytest

from myatri.contracts import ModelRequest, ModelResponse
from myatri.providers.gateway import Gateway as CoreGateway, ProviderError, validate_protocol
from warmagent.direct_harness import DirectHarness
from warmagent.structured_gateway import Gateway
from warmagent.vision import image_tokens, project_images
from test_direct_harness import owner_raw, settings


def image_request(tmp_path):
    image = tmp_path / 'image.png'
    Image.new('RGB', (32, 24), 'red').save(image)
    records = [{'file': str(image), 'sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                'media_type': 'image/png', 'bytes': image.stat().st_size}]
    body = json.dumps(records).encode()
    manifest = tmp_path / 'manifest.json'
    manifest.write_bytes(body)
    receipt = {'host_receipt_id': 'image-call', 'image_manifest': {'file': str(manifest),
        'sha256': hashlib.sha256(body).hexdigest(), 'image_count': 1}}
    messages = [{'role': 'user', 'content': '看看截图'}, {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'image-call', 'type': 'function', 'function': {'name': 'harness__Screenshot', 'arguments': '{}'}},
        {'id': 'other', 'type': 'function', 'function': {'name': 'harness__Observe', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'image-call', 'content': json.dumps(receipt)},
        {'role': 'tool', 'tool_call_id': 'other', 'content': '{}'}]
    return ModelRequest(messages=messages, role='foreground', max_tokens=100,
                        estimated_input_tokens=1000, budget_tokens=100000), image


def test_images_follow_complete_tool_round_and_keep_checkpoint_small(tmp_path):
    request, _ = image_request(tmp_path)
    projected = project_images(request, tmp_path)
    validate_protocol(projected.messages)
    assert projected.messages[-2]['tool_call_id'] == 'other'
    observation = projected.messages[-1]
    assert observation['role'] == 'user'
    assert observation['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    assert 'data:image' not in json.dumps(request.messages)
    assert image_tokens(request, tmp_path) == 1088


def test_image_integrity_and_store_boundary(tmp_path):
    request, image = image_request(tmp_path)
    with pytest.raises(ProviderError):
        project_images(request, tmp_path / 'other-root')
    image.write_bytes(b'changed')
    with pytest.raises(ProviderError):
        project_images(request, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['foreground', 'autonomy'])
async def test_main_gateway_sends_images_without_another_model(tmp_path, role):
    request, _ = image_request(tmp_path)
    request = request.model_copy(update={'role': role})
    class Provider:
        name, model = 'deepseek', 'deepseek-flash'
        def __init__(self):
            self.requests = []
        async def generate(self, value):
            self.requests.append(value)
            return ModelResponse(content='synthetic answer', provider='fixture', model='fixture',
                                 usage={'prompt_tokens': 1500, 'completion_tokens': 10})
    provider = Provider()
    core = CoreGateway(provider, retries=0)
    gateway = Gateway.adapt(core, vision_root=tmp_path)
    gateway = Gateway.adapt(gateway, vision_root=tmp_path)  # Shared-library adaptation is idempotent.
    await gateway.generate(request)
    assert len(provider.requests) == 1
    assert isinstance(provider.requests[0].messages[-1]['content'], list)
    assert gateway.used_tokens == 1510
    assert 'data:image' not in json.dumps(request.messages)


@pytest.mark.asyncio
async def test_native_image_and_mcp_image_become_durable_observations(tmp_path):
    root = Path(__file__).resolve().parents[1]
    node, runtime = root / 'var/harness/node/node.exe', root / 'var/harness/runtime'
    if not node.is_file() or not (runtime / 'node_modules/.pnpm').is_dir():
        pytest.skip('local Harness runtime is not installed')
    image = tmp_path / 'red.png'
    Image.new('RGB', (32, 24), 'red').save(image)
    server = tmp_path / 'mcp-fixture.py'
    server.write_text('''import sys,json,base64
from pathlib import Path
for line in sys.stdin:
 request=json.loads(line)
 if 'id' not in request:continue
 method=request['method']
 if method=='initialize':
  result={'protocolVersion':request['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
 elif method=='tools/list':
  result={'tools':[{'name':'Snapshot','description':'Synthetic image fixture','inputSchema':{'type':'object','properties':{}}}]}
 elif method=='tools/call':
  result={'content':[{'type':'text','text':'synthetic snapshot'},{'type':'image','mimeType':'image/png','data':base64.b64encode(Path(sys.argv[1]).read_bytes()).decode()}]}
 else:result={}
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
''', encoding='utf-8')
    config = tmp_path / 'native.json'
    config.write_text(json.dumps({'mcpServers': [{'transport': 'stdio', 'serverName': 'fixture',
        'command': sys.executable, 'args': [str(server), str(image)], 'cwd': str(tmp_path), 'env': {}}]}))
    harness = DirectHarness({**settings(), 'node_path': str(node), 'runtime_root': str(runtime),
        'project_root': str(tmp_path), 'native_config': str(config), 'vision_enabled': True,
        'vision_model': 'deepseek-flash', 'vision_endpoint': 'https://api.deepseek.com'},
        tmp_path / 'state', 'owner-1', ['wechat|private-1'], 'live')
    try:
        handlers = await harness.handlers(owner_raw(), context={'scope': 'test', 'turn_id': 'vision'})
        for name, args in [('read_image', {'file_path': str(image)}), ('mcp__fixture__Snapshot', {})]:
            result = await handlers['harness__' + name][1].execute_call(name, args)
            assert 'image_manifest' in result, result
            request = ModelRequest(messages=[{'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]},
                {'role': 'tool', 'tool_call_id': name, 'content': json.dumps(result)}])
            projected = project_images(request, tmp_path / 'var/harness/vision')
            assert projected.messages[-1]['content'][1]['type'] == 'image_url'
            assert 'data:image' not in json.dumps(result)
            assert len(json.dumps(result).encode()) < 6000
    finally:
        await harness.close()
