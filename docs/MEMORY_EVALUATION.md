# 记忆评测与紧凑索引基准

本文说明公开可复跑的合成评测，以及部署者如何在本机使用只读真实档案采样入口。真实记忆、匿名映射、逐题报告和运行数据不进入公开仓。

## 固定合成问题

benchmarks/memory_quality_dataset.py 提供 20 道固定合成问题，覆盖事件、身份与别名、日期、修正、历史快照、范围隔离和预算裁剪。夹具显式标注目标事件 expected_refs；可选 evidence_groups 可用于另外标注回答所需原文片段。事件召回与已标注原文证据召回分开统计。紧预算题刻意保留容量限制，报告应指出预算裁剪，不以放宽预算隐藏结果。

src/warmagent/memory_evaluation.py 在现有 ArchiveStore、GroupRecall、身份登记和可见性路径上比较旧、新索引。指标包含 Recall@1/3/5、MRR、nDCG@5、证据呈现、直接来源缺口及延迟分位数。宽预算顺序包含原有图扩展，代表完整召回链；它不是把固定候选池只交给新排序器的独立排序实验。

评测区分 unretrieved、rank_below_k、budget_pruned 与 answer_unused：分别表示宽预算未召回、排名靠后、被预算裁掉，以及答案已有证据却未引用。事件命中和原文证据命中分开计算，避免把已找到但未排进摘要前列的原文误算成遗忘。

评测器只运行检索和指标计算，不生成聊天回答，也不调用回答或裁判模型。可选的 answer_citations 是外部已提供的答案引用，用于标记已呈现证据是否被答案引用；它不自动验证回答是否正确。没有答案样本时，结果不能说明最终回答质量。

## 本机真实档案抽样

benchmarks/export_memory_sample.py 只读固定 ArchiveStore 快照，抽取带原始人类引用的事件，导出脱敏事件与引用片段；原始 ref/hash 映射仅保存在本机。抽样导出不是质量分数；部署者还需独立标注问题、目标版本与可选证据组，再运行检索评测。内容检索样本不用于推断真实人物身份或跨渠道访问权限。

公开仓不含个人档案、原始片段、原始 ref/hash 映射或逐题结果。部署者自行生成的脱敏导出和报告应留在本机忽略的 reports/ 下。若不希望读取或匿名导出本机记忆，只运行固定合成夹具即可。

## 紧凑索引合成短测

下表是此前完成的三次离线合成基准均值，合成档案含 240 条记录。每个后端、每个版本独立构建三次；旧版使用同一个 FastMemoryGraph，以免重复计算之前的构图优化。加载时间包含元数据和向量校验，文件系统为热缓存。512 维后端是确定性合成向量，不是 BGE 模型推理。

| 指标 | 旧格式 | 紧凑格式 | 变化 |
| --- | ---: | ---: | ---: |
| TF-IDF 代际加载 | 161.5161 ms | 25.8474 ms | 6.25× |
| 512 维稠密代际加载 | 240.4364 ms | 30.0455 ms | 8.00× |
| TF-IDF 代际文件 | 2,209,093 B | 505,113 B | 减少 77.1% |
| 512 维稠密代际文件 | 4,093,358 B | 1,159,329 B | 减少 71.7% |
| TF-IDF 加载 Python 分配峰值 | 23,074,490 B | 1,984,937 B | 减少 91.4% |
| 稠密加载 Python 分配峰值 | 27,087,792 B | 1,768,668 B | 减少 93.5% |

Python 分配峰值由 tracemalloc 测量，不是进程 RSS。构建耗时约 2.6—2.9 秒，仅小幅改善；稠密单次查询从 0.498 ms 变为 0.600 ms，没有查询加速。结果说明收益集中在代际文件体积、加载和 Python 对象分配，不代表聊天整体加速。本次公开文档同步没有重跑这组基准。

复现入口：benchmarks/compact_index_perf.py。此脚本写出的报告应指定到本地忽略目录，例如：

~~~powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.compact_index_perf --output reports/compact-perf.json --count 120
~~~

## 合成评测命令

以下命令会在本机生成合成题集和评测报告；本说明编写过程中没有运行这些功能或性能测试。

~~~powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.memory_quality_dataset reports/gold-memory.json
.venv\Scripts\python.exe -X utf8 -m warmagent.memory_evaluation --dataset reports/gold-memory.json --output reports/gold-memory-results.json --repeats 3
~~~

已有本地 BGE 时，可显式提供模型路径；脚本不会隐式下载模型或调用 API。真实样本的完整构建与查询延迟、召回和排序结果应由部署者在目标机器上自行报告，不应与本表的 240 条合成短测混为一谈。
