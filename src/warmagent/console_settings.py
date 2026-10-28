"""Small allowlisted TOML form; preserve comments, unknown fields and local keys."""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime
import re
import threading
import time
import tomllib

from myatri.storage.archive import _atomic_write

_LOCK = threading.Lock()


def field(file, table, key, label, kind='text', default='', **options):
    return {'id': f'{file}:{table}:{key}', 'file': file, 'table': table, 'key': key,
        'label': label, 'kind': kind, 'default': default, **options}


FIELDS = [
    field('bridge', 'bridge', 'owner_id', '你的稳定账号ID'),
    field('bridge', 'bridge', 'owner_name', '你的称呼', default='用户'),
    field('bridge', 'bridge', 'allowed_groups', '允许的渠道路由（每行一个）', 'lines', []),
    field('bridge', 'bridge', 'persona_file', '角色卡路径', default='config/persona/persona.md'),
    field('bridge', 'bridge.autonomy', 'enabled', '允许自主活动', 'bool', False),
    field('bridge', 'bridge.autonomy', 'scan_interval_seconds', '默认空闲扫描间隔（秒）', 'int', 1800, min=60, max=86400),
    field('bridge', 'bridge.autonomy', 'cost_quiet_hours', '省费时段（每行一个，例如09:00-12:00）', 'lines', ['09:00-12:00', '14:00-17:00']),
    field('bridge', 'bridge.wechat_unread', 'enabled', '微信连续消息合并', 'bool', True),
    field('bridge', 'bridge.wechat_unread', 'quiet_seconds', '停止输入后等待（秒）', 'float', 1.5, min=.1, max=30),
    field('bridge', 'bridge.wechat_unread', 'max_wait_seconds', '连续输入最长收集（秒）', 'float', 6.0, min=.1, max=60),
    field('bridge', 'bridge.memory', 'compact_index_enabled', '紧凑索引（关闭前须先回退旧代际）', 'bool', False),
    field('bridge', 'bridge.memory', 'incremental_index_enabled', '后台增量索引', 'bool', False),
    field('bridge', 'bridge.opencli', 'enabled', '允许专用浏览器工具', 'bool', False),
    field('bridge', 'bridge.android', 'enabled', '允许ADB手机工具', 'bool', False),
    field('bridge', 'bridge.android', 'adb_path', 'ADB程序路径'),
    field('bridge', 'bridge.android', 'serial', 'ADB设备serial'),
    field('engine', 'chat', 'endpoint', '模型服务地址', default='https://api.deepseek.com'),
    field('engine', 'chat', 'model', '聊天模型', default='deepseek-flash'),
    field('engine', 'chat', 'reasoning_effort', '思考档位', 'choice', 'high', choices=['low', 'high', 'max']),
    field('engine', 'chat', 'thinking_timeout', '单次模型超时（秒）', 'int', 240, min=1, max=600),
    field('engine', 'chat', 'thinking_max_tokens', '单次输出上限（含思考）', 'int', 32768, min=1024, max=393216),
    field('engine', 'budget', 'daily_tokens', '每日Token预算', 'int', 100000000, min=1, max=10000000000),
    field('wechat', 'wechatagent', 'group_enabled', '接收微信群聊', 'bool', True),
    field('wechat', 'wechatagent', 'private_enabled', '接收本人微信私聊', 'bool', True),
    field('wechat', 'wechatagent', 'group_name', '微信群完整名称'),
    field('wechat', 'wechatagent', 'bot_nickname', '机器人群昵称'),
    field('qq', 'onebot', 'enabled', '启用QQ渠道', 'bool', False),
    field('qq', 'onebot', 'bot_id', '机器人QQ号'),
    field('qq', 'onebot', 'owner_id', '你的QQ号'),
    field('qq', 'onebot', 'private_ids', 'QQ私聊白名单（每行一个）', 'lines', []),
    field('qq', 'onebot', 'group_ids', 'QQ群白名单（每行一个）', 'lines', []),
]
FILES = {'bridge': 'config/bridge.local.toml', 'engine': 'config/engine.toml',
    'wechat': 'config/wechatagent.local.toml', 'qq': 'config/onebot.local.toml'}


def at_table(data, table):
    for part in table.split('.'):
        data = data.get(part, {})
    return data


def revision(raw):
    return hashlib.sha256(raw).hexdigest()


def read_form(root):
    result, versions, errors = [], {}, {}
    for name, relative in FILES.items():
        path = root / relative
        if not path.is_file():
            errors[name] = '尚无配置，请先补齐缺失配置。'
            continue
        raw = path.read_bytes()
        versions[name] = revision(raw)
        try:
            data = tomllib.loads(raw.decode('utf-8-sig'))
        except (UnicodeError, tomllib.TOMLDecodeError):
            errors[name] = '配置格式错误，请从配置文件入口修正。'
            continue
        for spec in FIELDS:
            if spec['file'] == name:
                value = at_table(data, spec['table']).get(spec['key'], spec['default'])
                result.append({**spec, 'value': value})
    return {'fields': result, 'revisions': versions, 'errors': errors}


def replace_value(text, table, key, value):
    """Replace one primitive key; parse equality rejects complex ambiguous edits."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    header = re.search(r'(?m)^\[' + re.escape(table) + r'\][ \t]*(?:#.*)?$', text)
    if not header:
        return text.rstrip() + f'\n\n[{table}]\n{key} = {encoded}\n'
    start = header.end()
    following = re.search(r'(?m)^\[', text[start:])
    end = start + following.start() if following else len(text)
    block = text[start:end]
    match = re.search(r'(?m)^([ \t]*' + re.escape(key) + r'[ \t]*=[ \t]*)([^\n]*)$', block)
    if not match:
        block = '\n' + f'{key} = {encoded}\n' + block.lstrip('\r\n')
    else:
        old = match[2]
        # Comments after primitive values are retained; '#' inside strings is
        # handled by the decoder below instead of splitting on a raw '#'.
        comment = ''
        for index, char in enumerate(old):
            if char == '#':
                try:
                    tomllib.loads('v = ' + old[:index].strip())
                    comment = ' ' + old[index:]
                    break
                except tomllib.TOMLDecodeError:
                    pass
        block = block[:match.start()] + match[1] + encoded + comment + block[match.end():]
    return text[:start] + block + text[end:]


def save_form(root, body):
    name, edits = body['file'], body['changes']
    if name not in FILES or not isinstance(edits, dict) or not edits:
        raise ValueError('请选择要保存的配置项。')
    path = root / FILES[name]
    with _LOCK:
        raw = path.read_bytes()
        if body['revision'] != revision(raw):
            raise FileExistsError('配置已被其他窗口修改，请重新加载后再保存。')
        text = raw.decode('utf-8-sig').replace('\r\n', '\n')
        original = tomllib.loads(text)
        desired = deepcopy(original)
        specs = {spec['id']: spec for spec in FIELDS if spec['file'] == name}
        for key, value in edits.items():
            if key not in specs:
                raise ValueError('不支持修改此字段。')
            spec = specs[key]
            kind = spec['kind']
            if kind == 'bool' and type(value) is not bool:
                raise ValueError('开关值无效。')
            if kind in {'int', 'float'} and (type(value) not in (int, float) or not math.isfinite(value)
                    or kind == 'int' and type(value) is not int or not spec['min'] <= value <= spec['max']):
                raise ValueError('数值超出允许范围。')
            if kind == 'lines' and (not isinstance(value, list) or len(value) > 200
                    or any(not isinstance(item, str) or len(item) > 1000 for item in value)):
                raise ValueError('列表内容无效。')
            if kind in {'text', 'choice'} and (not isinstance(value, str) or len(value) > 2000
                    or kind == 'choice' and value not in spec['choices']):
                raise ValueError('文本内容无效。')
            prior = at_table(original, spec['table']).get(spec['key'], spec['default'])
            if key == 'bridge:bridge.memory:compact_index_enabled' and prior is True and value is False:
                raise ValueError('请先按紧凑索引文档回退旧JSON代际，再从配置文件关闭；此表单不直接降级。')
            table = desired
            for part in spec['table'].split('.'):
                table = table.setdefault(part, {})
            table[spec['key']] = value
            text = replace_value(text, spec['table'], spec['key'], value)
        if tomllib.loads(text) != desired:
            raise ValueError('此项使用多行或复杂TOML写法，请从配置文件入口编辑，原文件未改。')
        if name == 'bridge':
            for window in desired.get('bridge', {}).get('autonomy', {}).get('cost_quiet_hours', []):
                start, end = window.split('-')
                if datetime.strptime(start.strip(), '%H:%M').time() >= datetime.strptime(end.strip(), '%H:%M').time():
                    raise ValueError('省费时段的开始时间须早于结束时间。')
        backup = root / 'var/backups/control-config' / f'{time.time_ns()}-{path.name}'
        _atomic_write(backup, raw)
        _atomic_write(path, text.encode('utf-8'))
    return {'ok': True, 'message': '已保存并备份。运行进程保持原状；下次正常启动时加载。'}
