"""User-selected site entrypoints and the existing Bilibili plugin route."""
import re
from urllib.parse import urlsplit


SITE_ENTRYPOINTS = {
    '小红书': 'https://www.xiaohongshu.com/explore',
    '小黑盒': 'https://xiaoheihe.cn/app/bbs/home',
    '知乎': 'https://www.zhihu.com/',
    '贴吧': 'https://tieba.baidu.com/',
}
PREFERRED_DOMAINS = ['xiaohongshu.com', 'xiaoheihe.cn', 'zhihu.com', 'tieba.baidu.com', 'youtube.com']
BILI_ROUTE_RULE = ('B站只用既有dsh-bili-humanism插件：video类别发现bili_humanism_guide/candidates/read/source/frames。'
    '不通过OpenCLI、通用浏览器、网页搜索或另起命令行访问B站；插件失败如实报告，不换这些入口。')


def is_bilibili_host(host):
    host = (host or '').lower().rstrip('.')
    return any(host == domain or host.endswith('.' + domain) for domain in ('bilibili.com', 'b23.tv'))


def targets_bilibili(text):
    if any(is_bilibili_host(urlsplit('https://' + domain).hostname)
           for domain in re.findall(r'(?i)(?:^|\s)site:([^\s]+)', text)):
        return True
    return any(is_bilibili_host(urlsplit(url).hostname)
               for url in re.findall(r'https?://[^\s"\'<>]+', text, flags=re.I))


def plugin_redirect():
    return {'error': 'bilibili_use_existing_plugin', 'not_executed': True,
            'hint': BILI_ROUTE_RULE}
