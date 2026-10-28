"""Content-free request measurements and placement of transient host context."""
import json

from myatri.conversation.window import composition


def insert_current_context(messages, message):
    """Keep changing host data near this turn, outside the stable prefix."""
    index = next((i for i in range(len(messages)-1, -1, -1)
                  if messages[i].get('role') == 'user'), len(messages))
    messages.insert(index, message)


def request_measurement(messages, tools, previous=None):
    """Return local byte counts, never inferred provider token counts."""
    components = {}
    for message in messages:
        content = message.get('content') or ''
        if message.get('role') == 'tool':
            category = 'tool_results'
        elif isinstance(content, str) and content.startswith('宿主环境快照'):
            category = 'environment'
        elif isinstance(content, str) and content.startswith('当前工作视图便笺'):
            category = 'workspace'
        elif isinstance(content, str) and content.startswith('[近期群聊背景与当前消息'):
            category = 'timeline_and_current'
        elif isinstance(content, str) and content.startswith('当前是用户的私聊；'):
            category = 'group_policy_and_summary'
        else:
            category = next(iter(composition([message])))
        row = components.setdefault(category, {'messages': 0, 'utf8_bytes': 0})
        row['messages'] += 1
        row['utf8_bytes'] += len(json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode())
    encoded_tools = json.dumps(tools, ensure_ascii=False, separators=(',', ':')).encode()
    components['tool_schemas'] = {'messages': len(tools), 'utf8_bytes': len(encoded_tools)}
    encoded = json.dumps({'tools': tools, 'messages': messages}, ensure_ascii=False,
                         separators=(',', ':')).encode()
    common = None
    if previous is not None:
        common = next((i for i, (a, b) in enumerate(zip(encoded, previous)) if a != b),
                      min(len(encoded), len(previous)))
    return {'measurement': 'local_serialized_utf8_bytes_not_api_tokens',
            'serialized_bytes': len(encoded), 'shared_prefix_bytes': common,
            'components': components}, encoded


def record_request_usage(stats, measurement, usage):
    """Retain the latest composition and per-turn reported totals, without text."""
    actual = {}
    for key in ('prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'):
        value = usage.get(key)
        if type(value) is int and value >= 0:
            actual[key] = value
    if 'prompt_cache_hit_tokens' not in actual:
        details = usage.get('prompt_tokens_details')
        details = details if isinstance(details, dict) else {}
        value = details.get('cached_tokens')
        if type(value) is int and value >= 0:
            actual['prompt_cache_hit_tokens'] = value
    stats['request_context'] = {**measurement, 'reported_usage': actual}
    totals = stats.setdefault('request_usage_totals', {'received_model_calls': 0})
    totals['received_model_calls'] += 1
    totals['reported_usage_calls'] = totals.get('reported_usage_calls', 0) + int(
        'prompt_tokens' in actual and 'completion_tokens' in actual)
    totals['cache_reported_calls'] = totals.get('cache_reported_calls', 0) + int(
        'prompt_cache_hit_tokens' in actual)
    for key, value in actual.items():
        totals[key] = totals.get(key, 0) + value
