"""Install the locked OpenCLI runtime and official extension; preserve owner/settings."""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
import urllib.request
import zipfile

from setup_computer_control import _pnpm_command, replace_table


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pnpm-js')
    parser.add_argument('--pnpm')
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args()
    path = ROOT / 'config/bridge.local.toml'
    original = path.read_text(encoding='utf-8-sig')
    before = tomllib.loads(original)
    config = before['bridge']
    computer = config.get('computer', {})
    if not config.get('owner_id') or computer.get('owner_id') != config['owner_id']:
        raise ValueError('请先配置稳定owner及现有电脑工具，安装不会重新绑定账号。')
    node = Path(computer['node_path'])
    if not args.pnpm_js and not args.pnpm:
        runtime_node = shutil.which('node')
        bases = [node.parent, node.parent.parent]
        if runtime_node:
            bases.extend([Path(runtime_node).parent, Path(runtime_node).parent.parent])
        candidate = next((base / 'node_modules/pnpm/bin/pnpm.cjs' for base in bases
                          if (base / 'node_modules/pnpm/bin/pnpm.cjs').is_file()), None)
        if candidate:
            args.pnpm_js = str(candidate)
        else:
            args.pnpm = shutil.which('pnpm')
    manager = _pnpm_command(args, node)
    backup = ROOT / 'var/backups' / f'{datetime.now():%Y%m%d_%H%M%S}_opencli_setup'
    backup.mkdir(parents=True)
    shutil.copy2(path, backup / path.name)
    runtime = ROOT / 'var/opencli/runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ('package.json', 'pnpm-lock.yaml'):
        shutil.copy2(ROOT / 'config/opencli' / name, runtime / name)
    env = dict(os.environ)
    env['PATH'] = str(node.parent) + os.pathsep + env.get('PATH', '')
    subprocess.run([*manager, '--dir', str(runtime), 'install', '--frozen-lockfile',
        '--ignore-scripts', '--reporter', 'append-only'], env=env, check=True, timeout=180)
    pinned = json.loads((ROOT / 'config/opencli/runtime.json').read_text(encoding='utf-8'))
    archive = ROOT / 'var/opencli/extension.zip'
    with urllib.request.urlopen(pinned['extensionUrl'], timeout=45) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != pinned['extensionSha256']:
        raise ValueError('官方扩展校验失败，未解压或修改配置。')
    archive.write_bytes(data)
    extension = ROOT / 'var/opencli/extension'
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            if not (extension / member.filename).resolve().is_relative_to(extension.resolve()):
                raise ValueError('invalid_extension_path')
        package.extractall(extension)
    installed = json.loads((runtime / 'node_modules/@jackwener/opencli/package.json').read_text(encoding='utf-8'))
    manifest = json.loads((extension / 'manifest.json').read_text(encoding='utf-8'))
    if installed['version'] != pinned['cliVersion'] or manifest['version'] != pinned['extensionVersion']:
        raise ValueError('OpenCLI或扩展版本与锁定配置不同。')
    values = {'enabled': True, 'entrypoint': 'var/opencli/runtime/node_modules/@jackwener/opencli/dist/src/main.js',
        'browser_port': 9224, 'timeout_seconds': 90,
        'preferred_domains': ['xiaohongshu.com', 'xiaoheihe.cn', 'zhihu.com', 'tieba.baidu.com', 'youtube.com'],
        'bilibili_plugin_only': True,
        **config.get('opencli', {})}
    values['enabled'] = True
    updated = replace_table(original, 'bridge.opencli', values)
    expected = tomllib.loads(original)
    expected['bridge']['opencli'] = values
    assert tomllib.loads(updated) == expected
    if path.read_text(encoding='utf-8-sig') != original:
        raise RuntimeError('安装期间配置已改变；未覆盖，请重试。')
    path.write_text(updated, encoding='utf-8')
    receipt = {'cli_version': installed['version'], 'extension_version': manifest['version'],
        'extension_sha256': pinned['extensionSha256'], 'backup': str(backup), 'model_calls': 0}
    (ROOT / 'var/opencli/installation.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print('OpenCLI及官方扩展已安装，原账号/其他配置保留；正常重启WARMAGENT后加载。')
    if args.open_browser:
        from warmagent.opencli import OpenCLI
        client = OpenCLI(values, ROOT, str(node))
        asyncio.run(client.open_browser())
        print('专用浏览器已打开；需要的站点请在此窗口登录，登录态保留在项目var/opencli。')


if __name__ == '__main__':
    main()
