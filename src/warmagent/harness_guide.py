"""Small resource navigation and task recipes over the current authorized registry."""
import json
from pathlib import Path
from .site_routes import SITE_ENTRYPOINTS, BILI_ROUTE_RULE


VERSION = 'local-guide/v4'
LOCAL_RESOURCE_RULES = (
    '处理实际任务时先利用已有本地资料和已授权工具；不知道入口时查询harness_guide，再按需展开工具。'
    '资料先定位再读取，结果保存后核对；普通闲聊直接回复，不为增加调用而使用工具。'
    '兴趣与随笔是可延续的个人近况；与眼前话题相关时自然接起，不照清单汇报，想记下或查阅时再用活动类别工具。'
    '资源地图表示路径和本轮工具入口，设备在线、凭据及操作结果仍以实际返回为准。'
    '工具被中断或尚未执行不表示缺少能力；区分未执行、执行失败、入口缺失。'
    '声称做不到前先核对本轮目录；图片文件可通过files类别发现read_image，视觉传输是否启用以宿主说明为准。'
    '明确要查资料、读文件或执行操作时，在本轮发现并调用合适工具，再根据回执回答；已有授权不重复询问。'
    '一种入口失败先看具体错误，必要时换现有合适入口；需要登录或验证码就保留进度说明，不循环重试。'
    '网页与视频先区分标题、摘要、正文、字幕和画面；保留真实链接与读取范围，追问分享来源可用read_shared_source。'
    '复杂任务分步想、分步做：先确定当前可执行的一步，调用工具取得结果，再决定下一步；不在一轮里反复构思全部细节。'
    '写HTML或长文件时先落盘可运行的最小版本，再分块编辑完善并查看实际结果；不要等所有设计想完才第一次写文件。'
    '已有可用成果就简短汇报进度，剩余工作沿原工具对话和批次续做，不把局部成果说成全部完成，也不展示内部思考过程。'
    + BILI_ROUTE_RULE
)

# One vocabulary powers discovery, navigation and the on-demand guide.
CATEGORIES = {
    'files': ('文件与笔记', ('文件', '目录', '路径', '素材', '笔记', '日记', '知识', '读写', '保存', 'file', 'notes')),
    'command': ('命令行', ('命令行', '终端', '执行命令', '运行脚本', '内存', 'cpu', '磁盘', '硬盘', '系统信息', 'powershell', 'pwsh', 'command', 'shell')),
    'desktop': ('Windows桌面', ('电脑', '桌面', '屏幕', '截图', '截屏', '鼠标', '键盘', '记事本', 'computer', 'desktop', 'screen', 'screenshot')),
    'android': ('安卓手机', ('安卓', '手机', 'mumu', '模拟器', 'adb', 'android')),
    'browser': ('网页与浏览器', ('网页', '网站', '浏览器', '链接', '小红书', '知乎', '贴吧', '小黑盒', 'youtube', 'opencli', 'browser', 'chrome', 'edge')),
    'video': ('B站视频', ('b站', 'bilibili', '视频', '字幕', 'video', 'bili')),
    'document': ('文档', ('pdf', 'word', 'excel', '文档', '表格', 'document')),
    'memory': ('有来源的记忆', ('记忆', '回忆', '历史', '过去聊', 'memory', 'recall')),
    'tasks': ('活动、兴趣与后台任务', ('自主活动', '下一步', '后台任务', '任务进度', '任务报告', '安排',
        '兴趣', '爱好', '随笔', '便笺', '灵感', '休息', '睡眠', '放松', 'job', 'activity', 'interest', 'personal', 'rest')),
}

RECIPES = {
    'files': ['先查看资源地图中的notes目录，列出或搜索已有文件，不凭记忆猜保存路径。',
        '读取相关段落，增补原主题并保留来源和待核实项；已有内容不重复建副本。',
        '保存后按实际路径读回核对，再汇报文件位置；文件笔记不等同于已验证长期记忆。'],
    'command': ['读取实际工具schema，明确Windows工作目录，再执行命令。',
        '长任务取得job ID后等待完成事件；根据输出和目标核实结果，不只看退出码。'],
    'desktop': ['先观察当前桌面截图，确定目标窗口和控件，再执行有边界的一步。',
        '操作后重新观察确认；没有可用截图工具时如实说明，勿编造屏幕内容。'],
    'android': ['使用android类别操作已配置手机；手机shell与Windows命令行分别执行。',
        '先观察、再操作、再观察；设备离线时报告实际错误，目录存在不代表ADB已连接。'],
    'browser': ['按网页任务选择搜索或浏览器工具；已知链接可直接读取网页信息。',
        '有harness__browser_opencli时先args=["list","search","--site","zhihu","--limit","5"]发现，再用实际命令--help -f json查参数。',
        '自由浏览：args=["browser","open","https://www.zhihu.com"]，再["browser","state"]观察后click/fill/get；宿主自动维持任务会话，不传--session或--profile。',
        '自主选材优先小红书、小黑盒、知乎、贴吧和YouTube；有站点适配就用，没有则浏览器访问。小黑盒适配存在与否以list为准，普通全网搜索补缺。',
        '已指定的浏览入口：' + json.dumps(SITE_ENTRYPOINTS, ensure_ascii=False),
        BILI_ROUTE_RULE,
        '需要交互时读取浏览器状态再操作，按实际覆盖范围汇报。'],
    'video': [BILI_ROUTE_RULE,
        '先用插件candidates取热门候选，或用给定BV/视频URL调用read；按需source/frames分页，不虚构搜索能力。',
        '区分字幕、抽样画面与完整视频；凭据缺失按返回处理，不声称完整看完。'],
    'document': ['先读取文档页数/结构，再按页或片段取内容。',
        '保留文件路径、页码和覆盖范围；输出文件完成后实际读回或检查。'],
    'memory': ['先recall定位有来源的记录，再read_record读取需要的原文。',
        '保留说话人、scope和来源；检索命中或笔记转述不能直接变成事实证据。'],
    'tasks': ['用户安排以后做的事情时调用harness__activity_plan保存next或redirect，取得request_id再确认。',
        '自己的兴趣可用direction_add_interest记有来源的起点，再用direction_update_interest续记进展、动机和下一步，也可暂停或放下。',
        '想到的问题、创作灵感、主观看法和以后想聊的话，用personal_note保存；想法无需编造外部来源，不自动变成事实记忆。',
        '自主时间可以玩、探索、创作、发呆休息；不用次次学习或汇报。活动分享随笔时附share_note_ids，实际送达才算分享。',
        '聊天时可自然接起相关的待聊话题，没关系的便笺留着，不照单念，也不把旧笔记说成刚发生。',
        '已有后台job按ID读结果、等待事件；活动在工具边界接受调整，unknown先核对。'],
}

GUIDE_TOOL = {'type': 'function', 'function': {
    'name': 'harness_guide',
    'description': '查询本地资源地图及任务工作流：文件笔记、命令行、桌面、安卓、网页、B站、文档、记忆、活动。'
        '只读取当前授权目录与少量路径/版本元数据，不执行操作、不读取凭据。需要参数细节再展开对应工具。',
    'parameters': {'type': 'object', 'properties': {
        'topic': {'type': 'string', 'enum': ['overview', *CATEGORIES]},
        'query': {'type': 'string', 'maxLength': 200}}, 'additionalProperties': False}}}


def requested_categories(query):
    value = query.casefold()
    scores = {key: sum(len(word) for word in aliases if word in value)
        for key, (_, aliases) in CATEGORIES.items()}
    return sorted((key for key in scores if scores[key]), key=lambda key: -scores[key])


def tool_category(name, schema):
    name = name.casefold()
    for category, words in (
        ('android', ('android_',)), ('video', ('bili_',)), ('browser', ('browser_', 'web_search')),
        ('document', ('document_',)), ('memory', ('memory_', 'recall', 'read_record', 'read_group_message')),
        ('tasks', ('activity_', 'job_', 'task_', 'workspace_', 'artifact_', 'direction_', 'personal_', 'procedure_cards')),
        ('command', ('pwsh', 'powershell', 'terminal', '__shell', '__exec')),
        ('desktop', ('windows', 'desktop', 'screenshot', 'mouse', 'keyboard', 'computer_control')),
        ('files', ('__read', '__write', '__edit', '__glob', '__grep', 'file_', 'directory')),
    ):
        if any(word in name for word in words):
            return category
    definition = schema.get('function', schema)
    matches = requested_categories(definition.get('description', ''))
    return matches[0] if matches else None


class LocalGuide:
    def __init__(self, root, *, mode, workdir=None, runtime_root=None):
        self.root = Path(root).resolve()
        self.workdir = str((self.root / (workdir or '.')).resolve())
        self.runtime_root = (self.root / runtime_root).resolve() if runtime_root else None
        self.mode = mode
        self.catalog = None  # Bound to this immutable per-turn tool catalog.
        self.queries = 0

    def navigation(self):
        return {'version': VERSION, 'mode': self.mode, 'working_directory': self.workdir,
            'notes_directory': str(self.root / 'notes'),
            'tool_categories': self.catalog.category_view(), 'guide': 'harness_guide'}

    def __call__(self, topic='overview', query=''):
        self.queries += 1
        if topic not in {'overview', *CATEGORIES}:
            raise ValueError('unknown_guide_topic')
        categories = requested_categories(query) if query else []
        if topic == 'overview' and categories:
            topic = categories[0]
        if topic == 'overview':
            return {**self.navigation(), 'capability_status': 'registered_not_probed',
                'versions': self._versions(), 'resources': self._resources(),
                'topics': {key: value[0] for key, value in CATEGORIES.items()}}
        names = self.catalog.category_tools(topic)
        if topic == 'files' and not names:
            names = self.catalog.category_tools('command')
        return {'version': VERSION, 'topic': topic, 'steps': RECIPES[topic],
            'tools': names[:8], 'remaining_tools': len(names[8:]),
            'discover': [name for name in names[:8] if self.catalog.progressive
                and name in self.catalog.native and name not in self.catalog.expanded],
            'ready_tools': [name for name in names[:8] if not self.catalog.progressive
                or name not in self.catalog.native or name in self.catalog.expanded],
            'registered': bool(names), 'capability_status': 'registered_not_probed',
            'resources': self._resources() if topic == 'files' else [],
            'next': 'discover中的名称先展开；ready_tools可直接按已有schema调用。无入口时如实说明缺项。'}

    def _resources(self):
        result = []
        for label, relative in (('当前知识笔记', 'notes/素材小本.md'),
                ('旧运行目录笔记副本', 'var/harness/runtime/notes/素材小本.md')):
            path = self.root / relative
            info = path.stat() if path.is_file() else None
            result.append({'label': label, 'path': str(path), 'exists': info is not None,
                'bytes': info.st_size if info else None, 'modified_at': info.st_mtime if info else None})
        return result

    def _versions(self):
        versions = {}
        if self.runtime_root:
            for name in ('@deepseek-ai/dsh', 'dsh-bili-humanism'):
                path = self.runtime_root / 'node_modules' / name / 'package.json'
                if path.is_file():
                    versions[name] = json.loads(path.read_text(encoding='utf-8'))['version']
        return versions
