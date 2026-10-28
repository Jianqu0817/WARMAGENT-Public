# 架构与修改地图

同一主脑负责聊天与自主活动，原生调用已授权的 Harness 工具。渠道是输入/投递适配，主进程维护收件箱、活动、后台任务和 outbox；长期记忆共享时仍按渠道/说话人校验材料可见性。

| 模块 | 职责与修改入口 |
| --- | --- |
| `bridge.py` / `launch.py` | 运行装配、路由、身份、活动与投递接线 |
| `wechat.py` / `onebot.py` / `local_chat.py` / `local_web.py` | 微信、QQ、终端和网页适配 |
| `runtime_inbox.py` / `wechat_unread.py` | 持久输入、连续未读窗口与合并 |
| `reading_conversation.py` / `context.py` / `tool_context.py` | 主脑工具协议、聊天上下文和结果生命周期 |
| `activities.py` / `activity_runner.py` / `activity_planning.py` | 空闲调度、预约、聊天中断和用户安排 |
| `self_direction.py` / `personal_space.py` | 作息、兴趣进展、主观随笔与待聊话题 |
| `direct_harness.py` / `background_jobs.py` | 工具宿主、执行回执、后台 job 与完成事件 |
| `harness_guide.py` / `tool_catalog.py` | 短资源导航和按需展开 schema |
| `myatri/storage/archive.py` | 档案完整性、历史快照、CAS、选择性复制和只读阶段视图 |
| `memory_repair.py` / `index_graph.py` / `incremental_index.py` | 维护、精确构图、增量索引和后台合并 |

以实际源码文件名为准；`src/myatri` 是共享内核，`src/warmagent` 负责宿主适配。

## 持久化契约

- 原档案与 manifest 保留完整性和版本；调用方得到可变副本，不能改坏内部缓存。
- read_view 固定已验证截止点，只读，不跨模型等待沿用旧快照执行写入；写入仍经权威库 CAS。
- 回执、outbox 和归档各自权威，不假装跨数据库原子提交。
- unknown 表示效果未核对，不是失败，也不自动重放。
- 兴趣与随笔是主观状态，不自动转成事实、用户说过的话或人格修订。
- 分享按准备时的 note_id/revision 关联，整条确认 sent 才记已分享；旧回执不能覆盖新笔记版本。
- 默认 chat_first；用户安排、手动停止、暂停和省费时段继续约束调度，自己选择的目标可以调整并保留最近断点。

公开版配置与本地个人资料分离；新功能验证使用临时合成库，不借用真实运行目录。
