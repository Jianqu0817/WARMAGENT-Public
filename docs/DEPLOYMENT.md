# 部署

## 1. 基础环境与离线检查

需要 Python 3.12、uv；命令行 Harness 另外需要兼容 Node.js 和 pnpm。先运行 `Setup.cmd`，它安装锁定依赖、只补齐缺失的本机配置并执行静态检查，不登录或调用模型。

等价命令：

```powershell
uv sync --frozen --python 3.12 --extra bili --extra documents
.\.venv\Scripts\python.exe -m warmagent prepare
.\.venv\Scripts\python.exe -m warmagent check
```

需要本地神经嵌入时加 `--extra neural`，模型文件由使用者自行准备；默认没有模型时使用现有 TF-IDF 路径。

## 2. 本地聊天与模型

离线网页：`Start-Local-Web-Offline.cmd`；离线终端：`Start-Local-Offline.cmd`。

真实模型需要在 `config/engine.toml` 配置供应商 endpoint/model。在交互终端运行 `.\.venv\Scripts\python.exe -m myatri.credentials store-deepseek`，以隐藏输入写入 Windows 凭据管理器，不把 Key 写到版本管理文件中。之后在忽略的 `config/bridge.local.toml` 设置自己的固定 owner_id，再使用 `Start-Local-Web.cmd` 或 `Start-Local.cmd`。

网页默认 8769；`--ui` 同时启用 8766 管理、8767 监视、8768 上下文页。管理端仅绑定本机，令牌在 `var/`。停止时正常退出当前入口，待锁释放后切换模式。

## 3. 微信

公开仓没有微信客户端、Hook DLL 或已登录运行区。需要自行提供兼容的外部微信运行组件，配置 `config/wechatagent.local.toml` 的 workspace、native URL、账号和接收目标；模板为 `wechatagent.example.toml`。

典型顺序：`Open-WeChat.cmd` → 登录独立机器人账号 → `Start-Reader.cmd` → 检查账号后执行 bind → `Start-WeChat.cmd`。原生后端默认 30001；组件版本不兼容会导致连接失败，仓库代码不能代替该组件。

```powershell
.\.venv\Scripts\python.exe -m warmagent versions
.\.venv\Scripts\python.exe -m warmagent bind
```

设置 `bridge.owner_id` 为真实稳定账号 ID；`allowed_groups` 使用精确的 `platform_id|group_id`。机器人账号和 owner 账号应不同，不依据昵称认定身份。

## 4. QQ

使用者自行安装 NapCat。将 OneBot 11 反向 Universal WebSocket 指向 `ws://127.0.0.1:30021/ws`，在 `config/onebot.local.toml` 配置 bot_id、owner_id、私聊/群白名单以及与 NapCat 一致的 access_token，最后显式 enabled=true。

在 bridge.allowed_groups 添加对应 `xiaowennuan-qq|private:<账号>` 或 `xiaowennuan-qq|group:<群号>` 路由。`Start-Dual.cmd` 启动双通道；QQ 模板默认关闭。每个渠道保留自己的上下文 scope，长期记忆可共享。

## 5. Harness、视觉、浏览器与手机

`Setup-ComputerUse.cmd` 安装本机工具依赖；阅读脚本帮助并在本机配置独立 owner 身份与权限。工具可用性取决于实际安装的 Harness/MCP/视觉组件；电脑控制请求仍须通过稳定身份或单独的自主授权。

`Setup-OpenCLI.cmd` / `Open-OpenCLI.cmd` 管理浏览器工具和登录。B站访问保留专用插件路径；`Configure-Bilibili.cmd` 在本机进行登录配置，不提交 Cookie。

ADB 手机需安装 Android platform-tools，在设备开启 USB 调试并确认本机指纹；`Setup-Android.cmd` 完成本机依赖，具体 serial 写在本地配置中。默认不附带已绑定设备。

## 6. 记忆、角色与验证

新部署从空库开始。需要导入已有库时先备份，使用显式迁移脚本并保留来源；不要直接把他人的 state 或角色卡放进公开仓。

编辑私人角色卡后，通过版本化人格更新机制或新部署导入；已有库不会因修改模板自动覆盖人格。首次启动会兼容补齐新表/列/索引，原档案仍保留版本与校验。

最小验证运行 `Check-Integrated.cmd`。测试使用临时合成库与假模型，不证明真实渠道、登录、视觉或设备操作已经成功；上线后根据真实回执逐项确认。
