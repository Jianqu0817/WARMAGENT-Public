"""Bind an existing ADB device and install its optional Unicode input helper."""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import tomllib
import urllib.request

from warmagent.android_device import AndroidDevice, run_adb


ROOT = Path(__file__).resolve().parents[1]
KEYBOARD_URL = 'https://github.com/senzhk/ADBKeyBoard/releases/download/v2.4-dev/keyboardservice-debug.apk'
KEYBOARD_SHA256 = 'e0d0cf276b710cb34c46121f58720f5285a83ed410b0d45f57a0677b67dc2852'
KEYBOARD_IME = 'com.android.adbkeyboard/.AdbIME'


async def setup(args):
    config = ROOT / 'config/bridge.local.toml'
    original = config.read_text(encoding='utf-8-sig')
    before = tomllib.loads(original)
    current = before['bridge'].get('android', {})
    adb = args.adb or current.get('adb_path') or shutil.which('adb')
    serial = args.serial or current.get('serial')
    if not adb or not Path(adb).is_file() or not serial:
        raise ValueError('Specify --adb <adb.exe> --serial <host:port>, or configure [bridge.android] first.')
    settings = {'enabled': True, 'adb_path': str(Path(adb).resolve()), 'serial': serial}
    device = AndroidDevice(settings, ROOT)
    status = await device.status()
    if status.get('error'):
        raise RuntimeError('ADB device unavailable. Start MuMu and enable ADB, then retry.')
    folder = ROOT / 'var/android'
    folder.mkdir(parents=True, exist_ok=True)
    if not args.skip_keyboard:
        apk = folder / 'ADBKeyboard-v2.4-dev.apk'
        if not apk.exists():
            request = urllib.request.Request(KEYBOARD_URL, headers={'User-Agent': 'WARMAGENT-setup'})
            with urllib.request.urlopen(request, timeout=30) as response:
                apk.write_bytes(response.read())
        if hashlib.sha256(apk.read_bytes()).hexdigest() != KEYBOARD_SHA256:
            raise ValueError('ADBKeyboard release SHA256 mismatch; installation stopped.')
        code, stdout, stderr = await run_adb([settings['adb_path'], '-s', serial, 'install', '-r', str(apk)], 60)
        if code or b'Success' not in stdout:
            raise RuntimeError('ADBKeyboard install failed: ' + stderr.decode('utf-8', 'replace')[:300])
        await device._shell('ime', 'enable', KEYBOARD_IME)
        # Enable the helper without replacing the user's default input method.
        settings['keyboard_ime'] = KEYBOARD_IME
    elif current.get('keyboard_ime'):
        settings['keyboard_ime'] = current['keyboard_ime']
    if '[bridge.android]' in original:
        from warmagent.launch import replace_setting
        head, tail = original.split('[bridge.android]', 1)
        section, separator, rest = tail.partition('\n[')
        for key, value in settings.items():
            if key in current:
                section = replace_setting(section, key, value)
            else:
                section = section.rstrip() + '\n' + key + ' = ' + json.dumps(value, ensure_ascii=False) + '\n'
        changed = head + '[bridge.android]' + section + separator + rest
    else:
        changed = original.rstrip() + '\n\n[bridge.android]\n'
        changed += '\n'.join(key + ' = ' + ('true' if value is True else json.dumps(value, ensure_ascii=False))
            for key, value in settings.items()) + '\n'
    # Preserve all unrelated settings, including owner, autonomy, and memory scope.
    after = tomllib.loads(changed)
    expected = tomllib.loads(original)
    expected['bridge'].setdefault('android', {}).update(settings)
    if after != expected:
        raise ValueError('Unexpected config change; original left intact.')
    backup = ROOT / 'var/backups' / ('android-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    shutil.copy2(config, backup / config.name)
    config.write_text(changed, encoding='utf-8')
    result = await AndroidDevice(settings, ROOT).status()
    result.update(configured=True, production_restarted=False, keyboard_url=KEYBOARD_URL,
        keyboard_sha256=KEYBOARD_SHA256, original_config_backup=str(backup))
    (folder / 'setup-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb')
    parser.add_argument('--serial')
    parser.add_argument('--skip-keyboard', action='store_true')
    asyncio.run(setup(parser.parse_args()))


if __name__ == '__main__':
    main()
