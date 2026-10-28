# 架构与修改地图

## 2026-10-29 新增实现入口

| 模块 | 职责 |
| --- | --- |
| [recent_chat.py](../src/warmagent/recent_chat.py) | 按消息条数读取本轮冻结的同群近期原文并分页 |
| [personal_view.py](../src/warmagent/personal_view.py) / [personal_relevance.py](../src/warmagent/personal_relevance.py) | 私人生活小本只读页面与聊天相关材料选择 |
| [compact_index.py](../src/warmagent/compact_index.py) / [compact_vectors.py](../src/warmagent/compact_vectors.py) | 紧凑代际格式、稀疏与稠密向量持久化 |
| [memory_evaluation.py](../src/warmagent/memory_evaluation.py) | 检索评测，不生成回答 |
| [app.py](../src/myatri/app.py) | 共享 Application 新增可选 index_factory 接缝 |

除 Application 的可选 index_factory 外，CompactMemoryIndex、向量格式、评测路径与其余装配均在 WARMAGENT 适配层；缺省 Application 仍构造旧 MemoryIndex。

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

群聊近期原文读取只使用当前轮投递时冻结的同群窗口，按消息条数分页；不搜索全部历史、不跨群补足，也不授予 Harness 或电脑控制权限。

生活小本页面沿用本人私聊 scope 的只读投影；查看和下载不会领取投递、写入分享回执或唤醒模型。聊天只注入有话题关联的有界材料。

紧凑索引把 JSON 行元数据与向量 sidecar 分开：float64 .npy 以只读 mmap 加载，TF-IDF 使用 CSR .npz。发布仍校验文件摘要、空间身份与水位，并沿用原单调 current 发布；构建失败保留原 current。紧凑读写器读取旧格式；旧读写器不读取紧凑格式，切回旧代码前须先由紧凑读写器将 current 回退到已验证的旧代际。

公开版配置与本地个人资料分离；新功能验证使用临时合成库，不借用真实运行目录。功能细节见[近期功能说明](FEATURES_2026-10-29.md)，评测口径见[记忆评测说明](MEMORY_EVALUATION.md)。

## 独立操作台

`Start.cmd → __main__.py console → control_console.py` 提供独立8770界面与control-console.lock，不创建Bridge或新模型。console_catalog固定原入口和交互方式，console_settings白名单TOML/CAS/备份；html/css/js实现总览、渠道、配置、任务及文档五页。原监视/上下文/生活小本继续使用已有服务。[操作台契约和CMD对照](操作台与入口地图.md)。
