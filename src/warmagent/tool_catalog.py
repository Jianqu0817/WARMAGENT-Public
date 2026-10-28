"""Per-workspace schema discovery over an already authorized handler registry."""
from .harness_guide import CATEGORIES, LocalGuide, requested_categories, tool_category


def _stable_schema(value):
    if isinstance(value, dict):
        return {key: _stable_schema(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        return [_stable_schema(item) for item in value]
    return value


class ToolCatalog:
    def __init__(self, handlers, expanded=()):
        self.handlers = dict(handlers)
        self.native = sorted(name for name in handlers
            if name.startswith('harness__') and name != 'harness__activity_plan')
        self.expanded = set(expanded) & set(self.native)
        self.discovery_calls = 0
        guide = self.handlers.get('harness_guide')
        self.guide = guide[1] if guide and isinstance(guide[1], LocalGuide) else None
        if self.guide:
            self.guide.catalog = self
        self.progressive = len(self.native) > 8
        if self.progressive:
            categories = ', '.join(f'{key}({row["tools"]})' for key, row in self.category_view().items())
            schema = {'type': 'function', 'function': {
                'name': 'harness_discover_tools',
                'description': '按category、名称或任务关键词查找本轮已授权Harness工具；返回后下一轮可调用。'
                    'query留空可列举；harness_guide提供工作流。已注册类别：' + categories,
                'parameters': {'type': 'object', 'properties': {
                    'query': {'type': 'string'},
                    'category': {'type': 'string', 'enum': list(CATEGORIES)},
                    'names': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 8}},
                    'additionalProperties': False}}}
            self.handlers['harness_discover_tools'] = (schema, self.discover)

    def discover(self, query='', names=None, category=None):
        self.discovery_calls += 1
        names = names or []
        unknown = [name for name in names if name not in self.native]
        if unknown:
            return {'error': 'unknown_tool_names', 'names': unknown}
        tokens = query.casefold().split()
        if category is not None and category not in CATEGORIES:
            return {'error': 'unknown_tool_category'}
        direct = [name for name in self.native if all(
            token in (name + ' ' + self.handlers[name][0]['function'].get('description', '')).casefold()
            for token in tokens) and (category is None or tool_category(name, self.handlers[name][0]) == category)] if query else []
        categories = [category] if category else requested_categories(query)
        related = [name for key in categories for name in self.category_tools(key) if name in self.native]
        matches = names or list(dict.fromkeys([*direct, *related]))
        if not names and not query and not category:
            matches = list(self.native)
        selected = matches[:8]
        self.expanded.update(selected)
        return {'tools': [{'function': {'name': name, 'description':
                    self.handlers[name][0]['function'].get('description', '')[:160]}}
                    for name in selected],
                'matching_tools': len(matches), 'remaining': matches[8:], 'categories': categories}

    def category_tools(self, category):
        return sorted(name for name, entry in self.handlers.items()
            if name not in {'harness_guide', 'harness_discover_tools'}
            and tool_category(name, entry[0]) == category)

    def category_view(self):
        return {key: {'label': CATEGORIES[key][0], 'tools': len(names)}
            for key in sorted(CATEGORIES) if (names := self.category_tools(key))}

    def navigation(self):
        return self.guide.navigation() if self.guide else None

    def correction_hint(self, query):
        if not self.guide:
            return ''
        available = [CATEGORIES[key][0] for key in requested_categories(query) if self.category_tools(key)]
        return '可先查询harness_guide获取工作流与资源位置。' + (
            '匹配的已注册类别：' + '、'.join(available) + '。' if available else '')

    def schemas(self):
        selected = [entry[0] for name, entry in self.handlers.items()
                    if not self.progressive or name not in self.native or name in self.expanded]
        normalized = [_stable_schema(item if item.get('type') == 'function'
                                     else {'type': 'function', 'function': item}) for item in selected]
        return sorted(normalized, key=lambda item: item.get('function', {}).get('name', ''))

    def state(self):
        return {'expanded': sorted(self.expanded),
                'available_tools': len(self.handlers), 'loaded_tools': len(self.schemas()),
                'discovery_calls': self.discovery_calls, 'guide_queries': self.guide.queries if self.guide else 0}
