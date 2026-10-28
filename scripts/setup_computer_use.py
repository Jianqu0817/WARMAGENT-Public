"""Install isolated Windows-MCP and Bili frames, then wire the existing host.

No model call, desktop action, login, scheduled task, or production restart.
"""
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import tomllib

from setup_computer_control import replace_table


ROOT = Path(__file__).resolve().parents[1]


def install():
    bridge_file = ROOT / 'config/bridge.local.toml'
    text = bridge_file.read_text(encoding='utf-8-sig')
    bridge = tomllib.loads(text)['bridge']
    computer = dict(bridge['computer'])
    engine = tomllib.loads((ROOT / bridge['engine_config']).read_text(encoding='utf-8-sig'))
    if engine['chat']['model'] != 'deepseek-flash':
        raise ValueError('当前图片通道针对官方deepseek-flash；请先核对主模型。')
    node = Path(computer['node_path'])
    runtime = Path(computer['runtime_root'])
    plugin = runtime / 'node_modules/dsh-bili-humanism/bin/cli.mjs'
    if not node.is_file() or not plugin.is_file():
        raise FileNotFoundError('请先完成Direct Harness和B站插件安装。')
    uv = shutil.which('uv')
    if not uv:
        raise FileNotFoundError('请先安装uv。')
    python = ROOT / 'var/computer-use/venv/Scripts/python.exe'
    if not python.is_file():
        subprocess.run([uv, 'venv', '--python', '3.14', str(python.parents[1])], check=True)
    subprocess.run([uv, 'pip', 'install', '--python', str(python), '-r',
        str(ROOT / 'config/computer-use/requirements.lock')], check=True)
    import os
    env = {**os.environ, 'BILI_HUMANISM_HOME': str(ROOT / 'var/bili-humanism')}
    subprocess.run([str(node), str(plugin), 'setup-media', '--python',
        str(ROOT / '.venv/Scripts/python.exe')], env=env, check=True)
    native_file = ROOT / computer.get('native_config', 'config/harness/executor.local.json')
    native_text = native_file.read_text(encoding='utf-8')
    native = json.loads(native_text)
    server = {'transport': 'stdio', 'serverName': 'windows', 'command': str(python),
        'args': ['-m', 'windows_mcp', 'serve', '--transport', 'stdio', '--config',
                 str(ROOT / 'config/computer-use/windows-mcp.toml')],
        'cwd': str(ROOT), 'env': {'ANONYMIZED_TELEMETRY': 'false', 'PYTHONIOENCODING': 'utf-8'},
        'toolCallTimeoutMs': 60000}
    native['mcpServers'] = [s for s in native.get('mcpServers', []) if s.get('serverName') != 'windows'] + [server]
    computer['vision_enabled'] = True
    updated = replace_table(text, 'bridge.computer', computer)
    before = tomllib.loads(text)
    before['bridge']['computer'] = computer
    if tomllib.loads(updated) != before:
        raise RuntimeError('配置变更超出电脑工具范围，未保存。')
    if bridge_file.read_text(encoding='utf-8-sig') != text or native_file.read_text(encoding='utf-8') != native_text:
        raise RuntimeError('安装期间配置发生变化，请核对后重试。')
    backup = ROOT / 'var/backups' / ('computer-use-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    shutil.copy2(bridge_file, backup / bridge_file.name)
    shutil.copy2(native_file, backup / native_file.name)
    native_file.write_text(json.dumps(native, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    bridge_file.write_text(updated, encoding='utf-8')
    print('Windows-MCP与图片通道已配置；角色卡及记忆未改，正常重启WARMAGENT后生效。')
    print('配置备份：' + str(backup))


if __name__ == '__main__':
    install()
