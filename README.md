# WARMAGENT-Public

具有持久记忆、自然聊天、自主活动与本地工具能力的个人 Agent。微信、QQ、命令行和浏览器聊天复用同一运行内核；公开示例角色叫“小温暖”，可以在本地替换。

本仓提供通用代码和虚构角色模板，不包含任何人的聊天记录、私人角色卡、账号绑定、凭据或运行数据库。公开历史从通用快照开始。

## 快速开始

Windows 安装 Git、Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后执行：

```powershell
git clone https://github.com/Jianqu0817/WARMAGENT-Public.git
cd WARMAGENT-Public
.\Setup.cmd
.\Start-Local-Web-Offline.cmd
```

浏览器本地聊天默认在 `http://127.0.0.1:8769`。离线模式使用测试后端，不连接真实模型，也不能用它衡量真实回复质量。纯终端可运行 `Start-Local-Offline.cmd`。

接真实模型、微信或 QQ 前，按[部署说明](docs/DEPLOYMENT.md)配置自己的账号和服务。所有运行模式共用 `service.lock`，同一运行目录只开一个聊天进程。

## 能力

- 长期记忆、来源与版本、群聊与私聊范围、PAD 状态、短记忆及上下文管理。
- 微信连续消息合并、分段回复、按 scope 投递、发送回执与结果未知时停止盲重发。
- QQ 的 NapCat / OneBot 11 反向 WebSocket 适配；可与微信共用主进程，渠道上下文隔离。
- 默认 30 分钟空闲机会扫描、显式预约唤醒、后台 job 等待、用户聊天优先、暂停与恢复。
- 自主选择探索、娱乐、创作或休息；兴趣进展、私人随笔和待聊话题可持久接续。
- 同一主脑直接使用 Harness；本地命令、文件、桌面视觉、浏览器、文档、ADB 手机和视频插件按配置提供。
- 预算统计、运行监视、上下文观察页，以及可回查的工具与投递记录。
- 档案选择性复制、固定只读阶段、投递部分索引、增量索引与精确构图批计算优化。

工具与设备能力取决于本机安装和授权；默认不绑定任何真实账号或设备。自主省费时段默认保留北京时间 09—12、14—17，不影响正常聊天。

## 本地个性化

编辑 `config/bridge.local.toml` 设置自己的稳定 `owner_id` 和称呼 `owner_name`。可把个人角色卡保存为忽略的 `config/persona/persona.local.md`，并将 `persona_file` 指向该文件。名字只用于称呼，不用于认证。

运行数据在 `var/`，个人笔记在 `notes/`；本机配置、私人角色文件、数据库和凭据均不提交。详见[公开与本地边界](docs/PRIVACY.md)。

## 维护入口

- [部署、渠道和设备配置](docs/DEPLOYMENT.md)
- [模块地图与持久化契约](docs/ARCHITECTURE.md)
- [公开版本范围与验证](docs/STATUS.md)
- `Check-Integrated.cmd`：小范围离线契约验收。
- `Run-Perf.cmd`：合成数据性能对照，基线已随源码提供，不依赖私人 Git 历史。

第三方实现来源与许可证保留在源码注释及 `LICENSES/` 中。
