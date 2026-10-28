# -*- coding: utf-8 -*-
"""runtime_perf 纯分析模块(仅标准库 csv/statistics)。

入口:
- load_raw(path): 读取 reports/perf/<run_id>/raw.csv 为 list[dict];
- markdown_report(rows): 组装全部统计为一份 Markdown 报告。

职责: 对 raw.csv 做纯内存统计——配对比较(pair_compare)、最慢条件(top_slowest)、
缓存效果(cache_effect)、失败汇总(failure_report)、P16前后台竞争(p16_interference)、
内存峰值(memory_peaks)。不触发任何基准执行、不写任何文件。

已验证: ast 语法检查 + StringIO 手造 CSV 纯内存自测(未调用 runner, 未跑真实基准)。
未验证: 对真实 runner 产物的全量回归(避免污染计时基准, 留待基准空闲时人工复核)。

约定:
- 仅 status=="ok" 的行参与计时统计(失败行的 wall_ms 不完整, 不纳入);
- repeat 以 int 归一化后对齐; 同一条件内重复 repeat 取首次出现;
- python_working_set_peak 原始单位为字节(psutil memory_info), 除以 1024*1024 得 MB;
- 缺失数值一律保留 None / 显示 "-", 不虚构为 0。
"""

from __future__ import annotations

import csv
import statistics

_OK = "ok"
_MIN_FULL_SUCCESS = 5        # “5次全成功”与“样本充足”的阈值
_BYTES_PER_MB = 1024.0 * 1024.0
_CACHE_OFF = "cache=off."
_CACHE_ON = "cache=on."
_C1_SUFFIX = ".C1"
_W1_SUFFIX = ".W1"
_P16_CASE = "P16"            # runner 中 P16 用例的 case_id(cases_runtime.case_p16)
_ALONE_MARK = "alone"        # P16 基线条件 chat.alone 的识别标记

_VERDICT_CN = {
    "faster": "更快",
    "slower": "更慢",
    "undistinguishable": "不可区分",
    "no_pairs": "无可对齐样本",
}


# ---------------------------------------------------------------- 基础工具

def _to_float(value):
    """CSV 字段安全转 float; 空串/缺失/非数值返回 None(不虚构为 0)。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _repeat_key(value):
    """repeat 归一化: 能转 int 就转 int, 保证 '1' 与 1 可对齐。"""
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return text


def _sd(values):
    """样本标准差; 少于 2 个样本时无法估计, 返回 0.0(视为无波动)。"""
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _fmt(value, digits=2):
    """数值格式化; None(缺失)显示 '-'。"""
    return "-" if value is None else ("%.*f" % (digits, value))


def _fmt_pct(value):
    """带符号百分比; None 显示 '-'。"""
    return "-" if value is None else ("%+.1f%%" % value)


# ---------------------------------------------------------------- 1. 载入与分组

def load_raw(path):
    """读取 raw.csv 为 list[dict]; DictReader 天然兼容列缺失(如旧版无 note 列)。"""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def by_condition(rows):
    """按 condition 字段分组, 保持条件首次出现顺序。"""
    grouped = {}
    for row in rows:
        grouped.setdefault(row.get("condition", ""), []).append(row)
    return grouped


# ---------------------------------------------------------------- 2. 配对比较

def _ok_walls(rows, condition):
    """某条件 ok 行的 {repeat_key: wall_ms}; 重复 repeat 保留首次出现。"""
    walls = {}
    for row in by_condition(rows).get(condition, []):
        if row.get("status") != _OK:
            continue
        wall = _to_float(row.get("wall_ms"))
        if wall is None:
            continue
        walls.setdefault(_repeat_key(row.get("repeat", "")), wall)
    return walls


def pair_compare(rows, base_condition, feature_condition):
    """按 repeat 对齐 wall_ms 的配对比较(feature 相对 base)。

    返回字段: pairs(可对齐样本数) / 两组均值 / mean_diff_ms(逐对差值的均值) /
    relative_pct(均值差相对 base 均值) / sd_base、sd_feature(对齐样本的样本标准差) /
    verdict:
    - "no_pairs": 两条件没有公共 repeat;
    - "undistinguishable": |均值差| <= max(两组sd), 差异在波动范围内(含完全相等);
    - "faster" / "slower": feature 显著快于 / 慢于 base。
    均值与 sd 均基于对齐后的同一段 repeat 集合, 保证两组可比。
    """
    base_walls = _ok_walls(rows, base_condition)
    feat_walls = _ok_walls(rows, feature_condition)
    shared = sorted((k for k in base_walls if k in feat_walls), key=repr)
    base_vals = [base_walls[k] for k in shared]
    feat_vals = [feat_walls[k] for k in shared]
    result = {
        "base": base_condition,
        "feature": feature_condition,
        "pairs": len(shared),
        "base_mean_ms": statistics.mean(base_vals) if base_vals else None,
        "feature_mean_ms": statistics.mean(feat_vals) if feat_vals else None,
        "mean_diff_ms": None,
        "relative_pct": None,
        "sd_base": _sd(base_vals),
        "sd_feature": _sd(feat_vals),
        "verdict": "no_pairs",
    }
    if not shared:
        return result
    diffs = [f - b for b, f in zip(base_vals, feat_vals)]
    mean_diff = statistics.mean(diffs)
    result["mean_diff_ms"] = mean_diff
    # 相对百分比以 base 均值为分母; 分母缺失或为 0 时留 None(不虚构)
    base_mean = result["base_mean_ms"]
    if base_mean:
        result["relative_pct"] = mean_diff / base_mean * 100.0
    if abs(mean_diff) <= max(result["sd_base"], result["sd_feature"]):
        result["verdict"] = "undistinguishable"
    else:
        result["verdict"] = "faster" if mean_diff < 0 else "slower"
    return result


# ---------------------------------------------------------------- 3. 最慢条件

def top_slowest(rows, n=5):
    """最慢条件排行: 仅统计“5 次重复全部成功”的条件, 按 mean wall_ms 降序取前 n。

    全成功判定: 该条件行数 >= 5 且全部 status==ok(重跑多写的全 ok 行也接受,
    出现过任何失败/缺计时的条件即排除, 避免用残缺样本冒充完整均值)。
    """
    ranked = []
    for condition, cond_rows in by_condition(rows).items():
        if not (len(cond_rows) >= _MIN_FULL_SUCCESS
                and all(r.get("status") == _OK for r in cond_rows)):
            continue
        walls = [w for w in (_to_float(r.get("wall_ms")) for r in cond_rows)
                 if w is not None]
        if walls:
            ranked.append({"condition": condition, "n": len(walls),
                           "mean_ms": statistics.mean(walls),
                           "sd_ms": _sd(walls)})
    ranked.sort(key=lambda item: item["mean_ms"], reverse=True)
    return ranked[:n]


# ---------------------------------------------------------------- 4. 缓存效果

def cache_effect(rows):
    """自动发现两类缓存对比条件并逐一 pair_compare:

    1. cache 对: “cache=off.X” ↔ “cache=on.X”(去掉 cache 前缀后同名), off 为 base;
    2. 预热对: “Y.C1” ↔ “Y.W1”(去掉 .C1/.W1 后缀后同名), 冷启 C1 为 base。
    只报告双方都真实存在的条件对; 两类配对角度不同(前缀 vs 后缀), 各自独立成立。
    """
    conditions = set(by_condition(rows))
    pairs = []
    for condition in sorted(conditions):
        if condition.startswith(_CACHE_OFF):
            partner = _CACHE_ON + condition[len(_CACHE_OFF):]
            if partner in conditions:
                pairs.append(("cache", condition, partner))
    for condition in sorted(conditions):
        if condition.endswith(_C1_SUFFIX):
            partner = condition[: -len(_C1_SUFFIX)] + _W1_SUFFIX
            if partner in conditions:
                pairs.append(("warmup", condition, partner))
    seen = set()
    results = []
    for kind, base, feature in pairs:
        if (base, feature) in seen:  # 理论上不会重复, 双保险
            continue
        seen.add((base, feature))
        entry = {"kind": kind}
        entry.update(pair_compare(rows, base, feature))
        results.append(entry)
    return results


# ---------------------------------------------------------------- 5. 失败汇总

def failure_report(rows):
    """status != ok 的行汇总: 按 (condition, error_kind) 计数, 保留 repeat 与 note 原文。

    status 缺失/为空也视为失败(保守口径); error_kind 缺失记为 unknown。
    """
    grouped = {}
    for row in rows:
        if row.get("status") == _OK:
            continue
        key = (row.get("condition", ""), row.get("error_kind") or "unknown")
        bucket = grouped.setdefault(
            key, {"condition": key[0], "error_kind": key[1],
                  "count": 0, "repeats": [], "note": ""})
        bucket["count"] += 1
        bucket["repeats"].append(_repeat_key(row.get("repeat", "")))
        if not bucket["note"] and row.get("note"):
            bucket["note"] = str(row.get("note"))
    groups = sorted(grouped.values(),
                    key=lambda g: (-g["count"], g["condition"], g["error_kind"]))
    for group in groups:  # repeat 可能 int/str 混合, 用 repr 排序避免比较报错
        group["repeats"] = sorted(group["repeats"], key=repr)
    return {"total_failed": sum(g["count"] for g in groups), "groups": groups}


# ---------------------------------------------------------------- 6. P16 竞争

def p16_interference(rows):
    """P16 前后台竞争: 三条件(chat.alone / chat.with_monitor / chat.with_index_update)
    的 mean wall_ms, 以及各自相对 alone 的增幅; 任一条件样本 < 5 标注样本不足。"""
    p16_rows = [r for r in rows if r.get("case_id") == _P16_CASE]
    stats = {}
    for condition, cond_rows in by_condition(p16_rows).items():
        walls = [w for w in (_to_float(r.get("wall_ms")) for r in cond_rows
                             if r.get("status") == _OK) if w is not None]
        if walls:
            stats[condition] = {"condition": condition, "n": len(walls),
                                "mean_ms": statistics.mean(walls),
                                "sd_ms": _sd(walls)}
    baseline = None
    for condition in stats:
        if _ALONE_MARK in condition:
            baseline = condition
            break
    baseline_mean = stats[baseline]["mean_ms"] if baseline else None
    for item in stats.values():
        if baseline_mean:  # 无 alone 基线或分母为 0 时留 None, 不虚构
            item["vs_alone_pct"] = (item["mean_ms"] - baseline_mean) \
                / baseline_mean * 100.0
        else:
            item["vs_alone_pct"] = None
        item["sample_insufficient"] = item["n"] < _MIN_FULL_SUCCESS
    ordered = sorted(stats.values(), key=lambda i: i["condition"])
    if baseline in stats:  # 基线条件排最前, 便于阅读
        ordered.sort(key=lambda i: (i["condition"] != baseline, i["condition"]))
    return {"baseline": baseline, "conditions": ordered}


# ---------------------------------------------------------------- 7. 内存峰值

def memory_peaks(rows):
    """每条件 python_working_set_peak 的最大值(字节 → MB)。

    只要字段是有效数值就计入(含失败行——峰值若被记录到仍是真实测量);
    没有任何有效样本的条件不列出。
    """
    peaks = {}
    for row in rows:
        value = _to_float(row.get("python_working_set_peak"))
        if value is None:
            continue
        condition = row.get("condition", "")
        bucket = peaks.setdefault(condition, {"condition": condition,
                                              "peak_bytes": 0.0, "n": 0})
        bucket["peak_bytes"] = max(bucket["peak_bytes"], value)
        bucket["n"] += 1
    result = [{"condition": b["condition"],
               "peak_mb": round(b["peak_bytes"] / _BYTES_PER_MB, 2),
               "n": b["n"]} for b in peaks.values()]
    result.sort(key=lambda item: item["peak_mb"], reverse=True)
    return result


# ---------------------------------------------------------------- 8. Markdown 组装

def markdown_report(rows):
    """组装上述全部统计为 Markdown; 无数据的章节输出“无数据”, 缺失数值以 - 占位。"""
    grouped = by_condition(rows)
    ok_count = sum(1 for r in rows if r.get("status") == _OK)
    failed = failure_report(rows)
    lines = ["# runtime_perf 分析报告", ""]
    run_id = rows[0].get("run_id", "") if rows else ""
    lines.append("- 运行: %s" % (run_id or "-"))
    lines.append("- 记录: %d 条(ok %d / 失败 %d), 条件数 %d"
                 % (len(rows), ok_count, failed["total_failed"], len(grouped)))
    lines.append("")

    lines.append("## 最慢条件(仅 5 次全成功)")
    slowest = top_slowest(rows)
    if slowest:
        lines.append("| 条件 | mean wall_ms | sd | 样本 |")
        lines.append("|---|---:|---:|---:|")
        for item in slowest:
            lines.append("| %s | %s | %s | %d |"
                         % (item["condition"], _fmt(item["mean_ms"], 3),
                            _fmt(item["sd_ms"], 3), item["n"]))
    else:
        lines.append("无数据")
    lines.append("")

    lines.append("## 缓存效果(cache=off/on 与 C1/W1 自动配对)")
    effects = cache_effect(rows)
    if effects:
        lines.append("| 类型 | base | feature | 配对 | 均值差ms | 相对% | 结论 |")
        lines.append("|---|---|---|---:|---:|---:|---|")
        for item in effects:
            lines.append("| %s | %s | %s | %d | %s | %s | %s |"
                         % ("cache开关" if item["kind"] == "cache" else "冷/暖启",
                            item["base"], item["feature"], item["pairs"],
                            _fmt(item["mean_diff_ms"], 3),
                            _fmt_pct(item["relative_pct"]),
                            _VERDICT_CN.get(item["verdict"], item["verdict"])))
    else:
        lines.append("无数据")
    lines.append("")

    lines.append("## P16 前后台竞争")
    p16 = p16_interference(rows)
    if p16["conditions"]:
        lines.append("| 条件 | 样本 | mean ms | 相对 alone | 备注 |")
        lines.append("|---|---:|---:|---:|---|")
        for item in p16["conditions"]:
            note = "样本不足5" if item["sample_insufficient"] else ""
            if item["condition"] == p16["baseline"]:
                note = ("基线; " + note).rstrip("; ") or "基线"
            lines.append("| %s | %d | %s | %s | %s |"
                         % (item["condition"], item["n"],
                            _fmt(item["mean_ms"], 3),
                            _fmt_pct(item["vs_alone_pct"]), note or "-"))
        if p16["baseline"] is None:
            lines.append("")
            lines.append("注: 未找到含 alone 的基线条件, 相对增幅不可计算。")
    else:
        lines.append("无数据")
    lines.append("")

    lines.append("## 失败汇总")
    if failed["groups"]:
        lines.append("| 条件 | 错误 | 次数 | repeat | note |")
        lines.append("|---|---|---:|---|---|")
        for group in failed["groups"]:
            lines.append("| %s | %s | %d | %s | %s |"
                         % (group["condition"], group["error_kind"],
                            group["count"],
                            ",".join(str(r) for r in group["repeats"]) or "-",
                            group["note"] or "-"))
    else:
        lines.append("无数据")
    lines.append("")

    lines.append("## 内存峰值(python_working_set_peak)")
    peaks = memory_peaks(rows)
    if peaks:
        lines.append("| 条件 | peak MB | 样本 |")
        lines.append("|---|---:|---:|")
        for item in peaks:
            lines.append("| %s | %.2f | %d |"
                         % (item["condition"], item["peak_mb"], item["n"]))
    else:
        lines.append("无数据")
    lines.append("")
    return "\n".join(lines)
