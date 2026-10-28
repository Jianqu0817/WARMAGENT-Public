"""Local OpenCLI launcher using the same runtime and profile as Xiaowennuan."""
import asyncio
import json
from pathlib import Path
import sys
import tomllib

from warmagent.opencli import OpenCLI


ROOT = Path(__file__).resolve().parents[1]


async def main():
    config = tomllib.loads((ROOT / 'config/bridge.local.toml').read_text(encoding='utf-8-sig'))['bridge']
    client = OpenCLI(config.get('opencli', {}), ROOT, config.get('computer', {}).get('node_path'))
    if sys.argv[1:] == ['--open-browser']:
        await client.open_browser()
        print('专用浏览器已打开；可登录B站、知乎等站点，登录态不会复制到聊天里。')
        return
    result = await client.run(sys.argv[1:] or ['doctor'], {'scope': 'local-owner-console'})
    print(json.dumps(result, ensure_ascii=False))
    if result.get('error') or result.get('exit_code', 0):
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
