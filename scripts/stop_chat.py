"""Send Ctrl+C to this project's authenticated bot; leave WeChat/Reader open."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import tomllib
import urllib.error
import urllib.request

from warmagent.dsh_client import CREATION_FLAGS


ROOT = Path(__file__).resolve().parents[1]


def stop(config):
    if os.name != 'nt':
        raise RuntimeError('此脚本用于Windows；其他系统请在聊天窗口正常退出。')
    settings = tomllib.loads(config.read_text(encoding='utf-8-sig'))['bridge']
    port = int(settings.get('port', 8766))
    if not 1 <= port <= 65535:
        raise ValueError('管理端口无效。')
    token = (config.parent.parent / settings['token_file']).read_text(encoding='ascii').strip()
    request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/admin/activity',
        headers={'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if json.load(response).get('ok') is not True:
                raise RuntimeError('无法确认本项目的聊天服务，未停止其他进程。')
    except urllib.error.URLError as error:
        raise RuntimeError('未连接到本项目管理API；无UI的本地窗口请输入/exit。') from error
    powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    command = (f'$item=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort {port} -State Listen | Select-Object -First 1; '
        'Get-CimInstance Win32_Process -Filter "ProcessId=$($item.OwningProcess)" | '
        'Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress')
    result = subprocess.run([powershell, '-NoProfile', '-Command', command],
        capture_output=True, text=True, encoding='utf-8', creationflags=CREATION_FLAGS, check=True)
    process = json.loads(result.stdout)
    if not re.search(r'(?:^|\s)-m\s+warmagent\s+(?:wechat|chat|dual|web)(?:\s|$)', process.get('CommandLine', '')):
        raise RuntimeError('管理端口不是标准WARMAGENT聊天入口，未停止。')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.FreeConsole()
    if not kernel.AttachConsole(process['ProcessId']):
        kernel.AttachConsole(0xFFFFFFFF)
        raise RuntimeError('无法连接聊天控制台；请在原聊天窗口输入/exit或按Ctrl+C。')
    try:
        kernel.SetConsoleCtrlHandler(None, True)
        if not kernel.GenerateConsoleCtrlEvent(0, 0):
            raise OSError(ctypes.get_last_error(), '发送Ctrl+C失败')
        time.sleep(.5)
    finally:
        kernel.FreeConsole()
        kernel.AttachConsole(0xFFFFFFFF)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with socket.socket() as connection:
            connection.settimeout(.2)
            if connection.connect_ex(('127.0.0.1', port)) != 0:
                print('聊天服务已正常退出；微信、Reader和MuMu保持原状。')
                return
        time.sleep(.25)
    raise RuntimeError('已发送Ctrl+C，服务尚在退出；请稍后再切换，未强制杀进程。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/bridge.local.toml')
    args = parser.parse_args()
    try:
        stop(args.config.resolve())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    main()
