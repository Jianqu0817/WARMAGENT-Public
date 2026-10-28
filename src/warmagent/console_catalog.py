"""Fixed launch targets for the local control console; no browser-supplied shell."""
from __future__ import annotations


def action(key, label, group, description, cmd=None, *, argv=None, interactive=False, family=None, impact='local'):
    return {'id': key, 'label': label, 'group': group, 'description': description,
        'cmd': cmd, 'argv': argv, 'interactive': interactive, 'family': family, 'impact': impact}


ACTIONS = [
    action('web_offline', '本地网页 · 离线体验', '聊天模式', '使用合成后端，不连接模型或微信；保留同一套记忆与心跳。',
        'Start-Local-Web-Offline.cmd', argv=['{python}', '-u', '-m', 'warmagent', 'web', '--offline', '--ui', '--no-browser'], family='chat'),
    action('web_live', '本地网页 · 正式聊天', '聊天模式', '使用本地配置的模型；启动后从上方打开聊天页。',
        'Start-Local-Web.cmd', argv=['{python}', '-u', '-m', 'warmagent', 'web', '--live', '--ui', '--no-browser'], family='chat', impact='live'),
    action('wechat', '微信聊天', '聊天模式', '复用已登录微信，自动准备Reader、核验身份，再启动聊天及观察页。', 'Start-WeChat.cmd', family='chat', impact='live'),
    action('dual', '微信 + QQ', '聊天模式', '共用一个主进程与记忆；请先完成微信和NapCat登录。', 'Start-Dual.cmd', family='chat', impact='live'),
    action('cli_offline', '终端聊天 · 离线', '聊天模式', '在独立交互窗口聊天，保留心跳；输入 /exit 退出。', 'Start-Local-Offline.cmd', interactive=True, family='chat'),
    action('cli_live', '终端聊天 · 正式', '聊天模式', '打开原终端交互入口，使用已配置的模型。', 'Start-Local.cmd', interactive=True, family='chat', impact='live'),
    action('stop_chat', '正常停止聊天', '聊天模式', '沿原身份核验与Ctrl+C流程退出，保留微信、Reader和设备。', 'Stop-Chat.cmd'),
    action('wechat_login', '打开微信登录', '渠道与浏览器', '打开本机已配置的兼容微信客户端；登录在原客户端完成。', 'Open-WeChat.cmd'),
    action('reader_start', '启动微信Reader', '渠道与浏览器', '复用原只读消息采集入口，已有Reader时不重复启动。', 'Start-Reader.cmd', family='reader'),
    action('reader_stop', '停止微信Reader', '渠道与浏览器', '聊天退出后才可停止；原脚本核对进程归属。', 'Stop-Reader.cmd'),
    action('qq_login', '打开NapCat / QQ登录', '渠道与浏览器', '打开原NapCat入口；扫码和账号确认由你完成。', 'Open-NapCat.cmd'),
    action('browser_open', '打开专用浏览器', '渠道与浏览器', '复用OpenCLI浏览器与本机登录态，不复制Cookie。', 'Open-OpenCLI.cmd'),
    action('browser_doctor', '检查浏览器连接', '渠道与浏览器', '运行原OpenCLI doctor，结果保存在任务记录。', 'OpenCLI.cmd'),
    action('pages', '启动并打开观察页', '观察与资料', '按需启动监视、上下文页面；不会启动聊天或调用模型。', 'Open-Pages.cmd'),
    action('monitor', '运行监视与统计', '观察与资料', '打开原8767页：用量、PAD、活动、任务与动态回复设置。', 'Monitor.cmd'),
    action('context', '请求上下文观察', '观察与资料', '打开原8768页，查看模型请求与工具调用材料。', 'View-Context.cmd'),
    action('prepare', '补齐缺失配置', '配置与安装', '仅创建缺失模板和令牌，保留已有账号与配置。', argv=['{python}', '-m', 'warmagent', 'prepare']),
    action('check', '检查配置', '配置与安装', '运行已有静态配置检查，不登录、不调用模型。', argv=['{python}', '-m', 'warmagent', 'check']),
    action('versions', '查看微信版本兼容', '配置与安装', '核对已配置微信运行组件的版本信息。', argv=['{python}', '-m', 'warmagent', 'versions']),
    action('bind', '核验并绑定微信身份', '配置与安装', '读取已登录渠道身份并更新原绑定，不发送测试消息。', argv=['{python}', '-m', 'warmagent', 'bind'], family='setup'),
    action('credential', '录入模型密钥', '配置与安装', '在独立窗口隐藏输入；密钥写入Windows凭据管理器，不经过网页。',
        argv=['{python}', '-m', 'myatri.credentials', 'store-deepseek'], interactive=True, family='setup'),
    action('setup', '安装 / 更新项目依赖', '配置与安装', '运行原Setup；可能下载依赖，仅补缺失配置。运行聊天时请先正常停止。', 'Setup.cmd', family='setup', impact='install'),
    action('computer', '安装桌面与视觉工具', '配置与安装', '运行原ComputerUse安装器，可能下载依赖及更新本机配置。', 'Setup-ComputerUse.cmd', interactive=True, family='setup', impact='install'),
    action('android', '配置ADB手机', '配置与安装', '先在表单填写ADB路径与设备serial，再由原安装器配置输入法与绑定。', 'Setup-Android.cmd', interactive=True, family='setup', impact='device'),
    action('browser_setup', '安装OpenCLI', '配置与安装', '安装原锁定版本、扩展与专用浏览器连接。', 'Setup-OpenCLI.cmd', interactive=True, family='setup', impact='install'),
    action('bili_login', 'B站扫码登录', '配置与安装', '在原窗口显示二维码并确认登录；凭据仅保存在本机。', 'Configure-Bilibili.cmd', interactive=True, family='setup'),
    action('native', '构建原生加速组件', '维护与检查', '原Build-Native入口，按需下载编译依赖并构建。', 'Build-Native.cmd', family='setup', impact='install'),
    action('tests', '运行最小验收', '维护与检查', '仅在你点击后执行原合成测试；不会自动运行。', 'Check-Integrated.cmd', family='maintenance'),
    action('perf', '运行性能基准', '维护与检查', '按需执行原离线合成基准，可能需要数分钟。', 'Run-Perf.cmd', family='maintenance'),
    action('help', '命令入口说明', '维护与检查', 'Run.cmd仍保留高级参数；此处显示统一入口帮助。', 'Run.cmd', argv=['{python}', '-m', 'warmagent', '--help']),
]

# Only these files can be opened in an editor by the console.
EDIT_FILES = {
    'bridge': ('主配置', 'config/bridge.local.toml'),
    'engine': ('模型与预算', 'config/engine.toml'),
    'wechat': ('微信配置', 'config/wechatagent.local.toml'),
    'qq': ('QQ配置与令牌', 'config/onebot.local.toml'),
    'persona': ('本机私人角色卡', 'config/persona/persona.local.md'),
}

LINKS = [
    {'label': '本地聊天', 'port': 8769, 'path': '/'},
    {'label': '运行监视', 'port': 8767, 'path': '/'},
    {'label': '上下文', 'port': 8768, 'path': '/'},
    {'label': '生活小本', 'port': 8767, 'path': '/personal'},
    {'label': 'QQ登录面板', 'port': 6099, 'path': '/'},
]
