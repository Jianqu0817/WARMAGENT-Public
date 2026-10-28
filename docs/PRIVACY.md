# 公开源码与本地资料

公开仓只提供通用程序、配置模板、虚构角色、合成测试、基准代码和第三方归属。它不继承私人项目的 Git 历史。

本地保存：真实 owner_id/owner_name、平台账号、API Key、Cookie、设备 serial、私人角色卡、聊天、记忆、随笔、运行日志和备份。

推荐位置：

- `config/*.local.toml`、`config/harness/*.local.json`
- `config/persona/persona.local.md`，并在本地 bridge 配置中指定 persona_file
- `var/`、`notes/`、`private/`

这些路径默认忽略。提交前检查暂存区，不使用强制添加命令把本机资料提交进去。

`python scripts/check_public.py` 检查受版本管理文件的路径；`--policy <本机JSON>` 可额外检查自定义敏感字符串，策略文件保持本地。`--history` 连同当前仓库的全部可达 Git 对象一起扫描。输出只报告命中文件，不回显敏感内容。

从私人工作副本更新公开版时，只选择需要的源码和测试，替换示例身份、检查角色资源与文档，再做离线验收；不要直接推送私人分支、旧 tag、真实配置、操作交接记录或聊天素材。
