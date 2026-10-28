import asyncio
import base64
import json
from pathlib import Path
import shlex
import struct
from types import SimpleNamespace

import pytest

from warmagent.android_device import AndroidDevice, handlers
from warmagent.bridge import Bridge
from warmagent.direct_harness import DirectHarness
from warmagent.local_browser import LocalBrowser
from warmagent.vision import tool_images


PNG = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\rIHDR' + struct.pack('>II', 720, 1280)
IME = 'com.android.adbkeyboard/.AdbIME'


class FakeADB:
    def __init__(self):
        self.calls = []
        self.text = ''
        self.ime = 'fixture/.Keyboard'
        self.fail_broadcast = False
        self.receiver_probes = 0

    async def __call__(self, arguments, timeout):
        self.calls.append(arguments)
        assert arguments[1:3] == ['-s', '127.0.0.1:16416']
        args = arguments[3:]
        if args == ['get-state']:
            return 0, b'device\n', b''
        if args == ['exec-out', 'screencap', '-p']:
            return 0, PNG, b''
        command = shlex.split(args[1]) if args[0] == 'shell' else []
        if command == ['ime', 'list', '-s']:
            return 0, (IME + '\nfixture/.Keyboard\n').encode(), b''
        if command == ['settings', 'get', 'secure', 'default_input_method']:
            return 0, self.ime.encode(), b''
        if command[:2] == ['ime', 'set']:
            self.ime = command[2]
        if command[:3] == ['dumpsys', 'activity', 'broadcasts']:
            self.receiver_probes += 1
            return 0, (b'Registered Receivers: ADB_INPUT_B64' if self.receiver_probes > 1
                else b'Historical broadcasts: ADB_INPUT_B64'), b''
        if command == ['dumpsys', 'input_method']:
            return 0, ('mCurId=' + self.ime + ' mHaveConnection=true mBoundToMethod=true\nmInputShown=true').encode(), b''
        if command[:2] == ['am', 'broadcast']:
            if self.fail_broadcast:
                raise asyncio.TimeoutError
            self.text = base64.b64decode(command[-1]).decode()
        if command and command[0] == 'cat':
            xml = '<hierarchy>' + ''.join('<node text="温暖' + str(i) + '" bounds="[0,0][20,20]" clickable="true"/>' for i in range(50)) + '</hierarchy>'
            return 0, xml.encode(), b''
        if command == ['printf', 'long']:
            return 0, ('长' * 8000).encode(), b''
        if command == ['false']:
            return 1, b'', b'fixture error'
        return 0, b'ok', b''


def device(tmp_path, runner):
    return AndroidDevice({'enabled': True, 'adb_path': 'fixture-adb',
        'serial': '127.0.0.1:16416', 'keyboard_ime': IME}, tmp_path, runner=runner)


@pytest.mark.asyncio
async def test_phone_observation_roundtrip_and_ui_pages(tmp_path):
    runner = FakeADB()
    phone = device(tmp_path, runner)
    assert runner.calls == []
    quick = await phone.observe()
    assert quick['ui_nodes'] == [] and len(runner.calls) == 1
    observation = await phone.observe(include_ui=True, limit=30)
    assert observation['width'] == 720 and observation['height'] == 1280
    assert observation['total_nodes'] == 50
    assert observation['next_offset'] == len(observation['ui_nodes'])
    assert len(json.dumps(observation, ensure_ascii=False).encode()) <= 5500
    message = {'role': 'tool', 'content': json.dumps({'host_receipt_id': 'capture-1', **observation})}
    records = tool_images(message, tmp_path / 'var/harness/vision')
    assert len(records) == 1 and records[0]['media_type'] == 'image/png'
    page = await phone.observe(include_ui=True, offset=observation['next_offset'], limit=2)
    assert page['ui_nodes'][0]['text'] == '温暖' + str(observation['next_offset'])


@pytest.mark.asyncio
async def test_input_quoting_unicode_restore_and_shell_errors(tmp_path):
    runner = FakeADB()
    phone = device(tmp_path, runner)
    await phone.tap(12, 34)
    await phone.swipe(1, 2, 3, 4, duration_ms=500)
    await phone.key('KEYCODE_BACK')
    result = await phone.text('你好 %s; $(literal) 😺\nnext')
    assert runner.text == '你好 %s; $(literal) 😺\nnext'
    assert runner.ime == 'fixture/.Keyboard' and not result['verified']
    assert result['action_submitted']
    assert runner.receiver_probes == 2
    assert any(a[-1] == 'input tap 12 34' for a in runner.calls)
    long = await phone.shell('printf long')
    assert long['truncated'] and len(long['stdout']) == 2000
    assert json.loads(Path(long['output_file']).read_text(encoding='utf-8'))['stdout'] == '长' * 8000
    assert (await phone.shell('false'))['exit_code'] == 1
    runner.fail_broadcast = True
    with pytest.raises(asyncio.TimeoutError):
        await phone.text('不会自动重试')
    assert runner.ime == 'fixture/.Keyboard'


@pytest.mark.asyncio
async def test_editor_readback_happens_before_keyboard_restore(tmp_path):
    class EditorADB(FakeADB):
        async def __call__(self, arguments, timeout):
            command = shlex.split(arguments[-1]) if arguments[3] == 'shell' else []
            if command and command[0] == 'cat':
                assert self.ime == IME
                return 0, ('<hierarchy><node focused="true" text="' + self.text + '"/></hierarchy>').encode(), b''
            return await super().__call__(arguments, timeout)
    runner = EditorADB()
    result = await device(tmp_path, runner).text('小温暖😊')
    assert result['verified'] and runner.ime == 'fixture/.Keyboard'


@pytest.mark.asyncio
async def test_bridge_owner_offline_idempotence_and_pause(tmp_path):
    runner = FakeADB()
    direct = DirectHarness({'enabled': True, 'permission_profile': 'full', 'autonomy_granted': True},
        tmp_path / 'state', 'owner', ['fixture|room'], 'live')
    context = {'scope': 'fixture-scope', 'turn_id': 'turn-1'}
    bridge = SimpleNamespace(direct=direct, android=device(tmp_path, runner),
        browser=LocalBrowser({}, tmp_path / 'var'), vision_root=tmp_path / 'var/harness/vision')
    assert Bridge._browser_handlers(bridge, context, authorized=False) == {}
    entries = Bridge._browser_handlers(bridge, context, authorized=True)
    invoke = entries['harness__android_tap'][1].execute_call
    denied = await invoke('bad', {'x': 1, 'y': 2, 'serial': 'other-device'})
    assert denied['error'] == 'invalid_tool_arguments' and runner.calls == []
    first = await invoke('tap-1', {'x': 1, 'y': 2})
    second = await invoke('tap-1', {'x': 1, 'y': 2})
    assert first == second and len(runner.calls) == 1
    direct.activity_paused = lambda: True
    paused = Bridge._browser_handlers(bridge, {**context, 'activity_id': 'activity-1'}, authorized=True)
    result = await paused['harness__android_tap'][1].execute_call('tap-paused', {'x': 1, 'y': 2})
    assert result['error'] == 'activity_paused_before_action' and len(runner.calls) == 1
    direct.mode = 'offline'
    assert Bridge._browser_handlers(bridge, context, authorized=True) == {}
    assert handlers(AndroidDevice({}, tmp_path), None) == {}


@pytest.mark.asyncio
async def test_capture_receipt_uses_existing_vision_path_and_unknown_not_replayed(tmp_path):
    runner = FakeADB()
    phone = device(tmp_path, runner)
    direct = DirectHarness({'enabled': True, 'permission_profile': 'full'}, tmp_path / 'state', 'owner', [], 'live')
    async def execute(name, args, call_id, perform):
        return await direct.execute_local(name, args, {'turn_id': 'turn-1'}, call_id, perform, authorized=True)
    entries = handlers(phone, execute)
    receipt = await entries['harness__android_observe'][1].execute_call('observe-1', {'include_ui': False})
    assert tool_images({'role': 'tool', 'content': json.dumps(receipt)}, phone.vision_root)
    assert 'harness__android_observe' not in handlers(phone, execute, vision_enabled=False)
    runner.fail_broadcast = True
    result = await entries['harness__android_text'][1].execute_call('input-unknown', {'text': 'test'})
    assert result['error'] == 'outcome_unknown'
    count = len(runner.calls)
    again = await entries['harness__android_text'][1].execute_call('input-unknown', {'text': 'test'})
    assert again['error'] == 'outcome_unknown' and len(runner.calls) == count
