"""首轮核心用例：P01归档读取 / P03检索缓存 / P05近期月度索引 / P06索引与维护。

只经真实入口计时；计数器在runner侧包装真实方法，不替换执行。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from myatri.memory import MemoryIndex
from myatri.storage import ArchiveStore

from warmagent.context_read_view import ContextReadView
from warmagent.group_memory import GroupRecall
from warmagent.identity import IdentityRegistry
from warmagent.memory_timeline import MemoryTimeline
from warmagent.shared_runtime import MemoryVisibility

from .fixtures import (FixtureBuilder, SCOPE_A, copy_fixture, new_revision_records)
from .instrument import Counters, timed_repeats

CONFIG_BASELINE = "baseline-current"


def build_fixture(ctx, size: str) -> Path:
    """按需生成并缓存基础夹具目录。"""
    target = ctx.fixtures_dir / size
    if not (target / "fixture.json").exists():
        FixtureBuilder(size).build(target)
    return target


# ---------------------------------------------------------------- P01

def case_p01(ctx):
    source = build_fixture(ctx, ctx.size)
    base = copy_fixture(source, ctx.work_dir / "p01")

    def make_store(fresh_copy: Path | None = None):
        return ArchiveStore(fresh_copy / "archive", agent_id="warmagent",
                            user_id="synthetic-shared")

    # snapshot: C1=新对象首次访问; W1=同对象第二次
    holder = {}

    def prepare_c1():
        holder["store"] = make_store(base)
        holder["counters"] = Counters()
        holder["counters"].wrap(holder["store"], "_load", "archive_load_calls")
        return holder["store"]

    def prepare_w1():
        store = holder.get("store")
        if store is None:
            store = prepare_c1()
            store.snapshot()  # 预热后测稳定命中
        return store

    for op, entry in (("snapshot", "archive_snapshot"), ("list_records", "list_event"),
                      ("get_record", "get_single")):
        def run_snapshot(store):
            return store.snapshot()

        def run_list(store):
            return store.list_records("event")

        single_id = "event_group_a_0"

        def run_get(store):
            return store.get_record(single_id)

        runner = {"snapshot": run_snapshot, "list_records": run_list,
                  "get_record": run_get}[op]

        def validate(outcome, _prep, entry=entry, op=op):
            assert outcome is not None

        for state, prep, warm in (("C1", prepare_c1, 0), ("W1", prepare_w1, 0)):
            if state == "W1":
                holder.clear()
                prepare_c1()
                holder["store"].snapshot()
                holder["counters"] = Counters()
                holder["counters"].wrap(holder["store"], "_load", "archive_load_calls")
                def prep():
                    return (holder["store"])
            timed_repeats(ctx.recorder, case_id="P01",
                          condition=f"{op}.{state}", fixture_id=f"{ctx.size}",
                          cache_state=state, scope_label="group_a",
                          principal_label="shared", snapshot_label="",
                          config_hash=CONFIG_BASELINE, warmup=warm,
                          prepare=prep, run=runner, validate=validate,
                          counters_from=lambda prep: holder["counters"],
                          extras={"result_count": 1 if op == "get_record" else None})

    # ContextReadView初始化 + 100个精确ref读取
    holder.clear()
    prepare_c1()

    def run_view_init(store):
        return ContextReadView(store)

    timed_repeats(ctx.recorder, case_id="P01", condition="view_init",
                  fixture_id=ctx.size, cache_state="C1", scope_label="group_a",
                  principal_label="shared", snapshot_label="",
                  config_hash=CONFIG_BASELINE, prepare=lambda: holder["store"],
                  run=run_view_init,
                  validate=lambda outcome, _p: assert_has(outcome, "snapshot"),
                  counters_from=lambda _p: holder["counters"])

    refs = json.loads((base / "fixture.json").read_text(encoding="utf-8"))["sample_event_refs"]
    holder.clear()
    prepare_c1()

    def prep_refs_c1():
        holder["store"] = make_store(base)
        holder["view"] = ContextReadView(holder["store"])
        holder["counters"] = Counters()
        holder["counters"].wrap(holder["store"], "_load", "archive_load_calls")
        return holder["view"]

    def prep_refs_w1():
        view = holder.get("view")
        if view is None:
            prep_refs_c1()
            for ref in refs:  # 预热一次读取
                holder["view"].get_record(ref)
        return holder["view"]

    def run_read_refs(view):
        return [view.get_record(ref) for ref in refs]

    for state, prep in (("C1", prep_refs_c1), ("W1", prep_refs_w1)):
        if state == "W1":
            holder.clear()
            prep_refs_c1()
            for ref in refs:
                holder["view"].get_record(ref)
            holder["counters"] = Counters()
            holder["counters"].wrap(holder["store"], "_load", "archive_load_calls")
            def prep():
                return (holder["view"])
        timed_repeats(ctx.recorder, case_id="P01", condition=f"read_100_refs.{state}",
                      fixture_id=ctx.size, cache_state=state, scope_label="group_a",
                      principal_label="shared", snapshot_label="",
                      config_hash=CONFIG_BASELINE, prepare=prep, run=run_read_refs,
                      validate=lambda outcome, _p: (assert_true(len(outcome) == 100)
                                                    if outcome else assert_true(False)),
                      counters_from=lambda _p: holder["counters"],
                      extras={"result_count": 100})


def assert_true(value, message="expected truthy"):
    assert value, message


def assert_has(obj, attr):
    assert obj is not None and hasattr(obj, attr)


# ---------------------------------------------------------------- P03

def _recall_setup(work: Path):
    """store+index只建一次；C1的"冷"指应用级缓存为空，不是重建索引。"""
    store = ArchiveStore(work / "archive", agent_id="warmagent", user_id="synthetic-shared")
    index = MemoryIndex(store)
    asyncio.run(index.rebuild())
    return store, index


def _recall_for(store, index, *, wave: bool, cache: bool, scope: str = SCOPE_A,
                identities=None):
    if identities is None:
        identities = IdentityRegistry(store.root.parent / "identities.sqlite3",
                                      owner_id="owner-1", owner_name="Owner")
    recall = GroupRecall(store, index, identities, scope,
                         retrieval_wave_enabled=wave, scope_index_cache_enabled=cache)
    recall.shared_visibility = True
    recall.current_sender = "member-1"
    return recall


def case_p03(ctx):
    source = build_fixture(ctx, ctx.size)
    questions = ctx.load_questions(source)
    for cache in (False, True):
        tag = f"cache={'on' if cache else 'off'}"
        work = copy_fixture(source, ctx.work_dir / f"p03-{tag}")
        setup = {"done": False, "store": None, "index": None}
        holder = {"recall": None, "store": None}

        def ensure_setup():
            if not setup["done"]:
                setup["store"], setup["index"] = _recall_setup(work)
                setup["done"] = True
            return setup["store"], setup["index"]

        def prep_c1():
            store, index = ensure_setup()
            holder["recall"] = _recall_for(store, index, wave=False, cache=cache)
            return holder["recall"]

        def prep_w1():
            if holder.get("recall") is None:
                prep_c1()
                # 预热一次不计分
                holder["recall"].recall("预热查询")
            return holder["recall"]

        def run_question(question_key, query):
            def run(recall):
                recall.current_sender = "member-1"
                return recall.recall(query, budget=4000)
            return run

        def validate_question(question_key, expected_ref, counter_ref, source_ref,
                              scope_private_expected=False):
            def validate(bundle, _prep):
                refs = [record.record_id for record in bundle.records]
                if question_key == "direct_fact":
                    assert expected_ref in refs[:3], f"direct fact miss: {refs[:5]}"
                if question_key == "preference_correction":
                    assert expected_ref in refs and counter_ref in refs, \
                        f"correction pair miss: {refs[:6]}"
                if question_key == "two_hop_chain":
                    assert expected_ref in refs, f"two hop miss: {refs[:6]}"
                if question_key == "private_negative":
                    assert expected_ref not in refs, "private leaked into group scope"
            return validate

        for question_key, spec in questions.items():
            run = run_question(question_key, spec["query"])
            validate = validate_question(question_key, spec["expected"],
                                         spec.get("counter"), spec.get("source"))
            for state, prep in (("C1", prep_c1), ("W1", prep_w1)):
                if state == "W1" and holder.get("recall") is None:
                    prep_w1()
                timed_repeats(ctx.recorder, case_id="P03",
                              condition=f"{tag}.{question_key}.{state}",
                              fixture_id=ctx.size, cache_state=state,
                              scope_label="group_a", principal_label="member-1",
                              snapshot_label="", config_hash=CONFIG_BASELINE,
                              prepare=prep, run=run, validate=validate)

        # I1/W2：cache=on时提交一条新记录后首查、二查；cache=off同流程对照
        holder.clear()
        ensure_setup()
        holder["recall"] = _recall_for(setup["store"], setup["index"], wave=False, cache=cache)
        holder["store"] = setup["store"]
        holder["recall"].recall("预热查询")
        def run_i1(recall):
            recall.current_sender = "member-1"
            return recall.recall("合成事实与新增修订", budget=4000)

        def validate_i1(bundle, _prep):
            assert bundle is not None

        def run_w2(recall):
            recall.current_sender = "member-1"
            return recall.recall("合成事实与新增修订", budget=4000)

        from .instrument import DEFAULT_REPEATS as _R, measure
        for repeat in range(1, _R + 1):
            holder["store"].commit(new_revision_records(600 + repeat + int(cache) * 100),
                                   idempotency_key=f"perf-i1-{int(cache)}-{repeat}")
            for state, operation in (("I1", run_i1), ("W2", run_w2)):
                with measure() as metrics:
                    outcome = operation(holder["recall"])
                validate_i1(outcome, None)
                ctx.recorder.record(case_id="P03", condition=f"{tag}.{state.lower()}_paired",
                                    fixture_id=ctx.size, cache_state=state,
                                    scope_label="group_a", principal_label="member-1",
                                    snapshot="", config_hash=CONFIG_BASELINE,
                                    repeat=repeat, **metrics)

    # owner→普通成员切换(cache=on)：ACL缓存换主体；群scope下任何主体都不得见私聊
    if getattr(ctx, 'quick', False):
        switch_store, switch_index = setup['store'], setup['index']
    else:
        work = copy_fixture(source, ctx.work_dir / "p03-switch")
        switch_store, switch_index = _recall_setup(work)
    recall = _recall_for(switch_store, switch_index, wave=False, cache=True)
    recall.shared_visibility = True
    fact = questions["direct_fact"]
    secret = questions["private_negative"]["expected"]

    def run_switch(_prep):
        recall.current_sender = "owner-1"
        owner_bundle = recall.recall(fact["query"], budget=4000)
        recall.current_sender = "member-1"
        member_bundle = recall.recall(fact["query"], budget=4000)
        return owner_bundle, member_bundle

    def validate_switch(outcome, _prep):
        owner_bundle, member_bundle = outcome
        owner_refs = {record.record_id for record in owner_bundle.records}
        member_refs = {record.record_id for record in member_bundle.records}
        assert fact["expected"] in owner_refs, "owner应召回群事实"
        assert fact["expected"] in member_refs, "成员应召回群事实"
        assert secret not in owner_refs, "群scope下owner也不得见私聊记录"
        assert secret not in member_refs, "群scope下成员不得见私聊记录"

    timed_repeats(ctx.recorder, case_id="P03", condition="cache=on.sender_switch",
                  fixture_id=ctx.size, cache_state="W1", scope_label="group_a",
                  principal_label="owner-1->member-1", snapshot_label="",
                  config_hash=CONFIG_BASELINE, warmup=1, prepare=lambda: None,
                  run=run_switch, validate=validate_switch)


# ---------------------------------------------------------------- P05

def case_p05(ctx):
    source = build_fixture(ctx, ctx.size)
    work = copy_fixture(source, ctx.work_dir / "p05")
    store = ArchiveStore(work / "archive", agent_id="warmagent", user_id="synthetic-shared")
    MemoryVisibility(store, SCOPE_A, False)
    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    holder = {"timeline": None, "store": store}

    def prep_c1():
        holder["timeline"] = MemoryTimeline(store, MemoryVisibility(store, SCOPE_A, False))
        return holder["timeline"]

    def prep_w1():
        if holder["timeline"] is None:
            prep_c1()
            holder["timeline"].view(SCOPE_A, now)  # 预热
        return holder["timeline"]

    def run_view(timeline):
        return timeline.view(SCOPE_A, now, budget_chars=4000)

    def validate_view(result, _prep):
        assert result is not None
        assert "recent" in result and result["recent"], "近期列表为空"

    for state, prep in (("C1", prep_c1), ("W1", prep_w1)):
        if state == "W1":
            holder.clear()
            holder["store"] = store
            prep_c1()
            holder["timeline"].view(SCOPE_A, now)
        timed_repeats(ctx.recorder, case_id="P05", condition=f"view.{state}",
                      fixture_id=ctx.size, cache_state=state, scope_label="group_a",
                      principal_label="member-1", snapshot_label="",
                      config_hash=CONFIG_BASELINE, warmup=0 if state == "C1" else 0,
                      prepare=prep, run=run_view, validate=validate_view)

    # 跨分钟：水位推进61秒，缓存应失效重建
    holder.clear()
    holder["timeline"] = MemoryTimeline(store, MemoryVisibility(store, SCOPE_A, False))
    holder["timeline"].view(SCOPE_A, now)
    next_minute = now + timedelta(seconds=61)

    def run_cross(timeline):
        return timeline.view(SCOPE_A, next_minute, budget_chars=4000)

    timed_repeats(ctx.recorder, case_id="P05", condition="view.cross_minute",
                  fixture_id=ctx.size, cache_state="I1", scope_label="group_a",
                  principal_label="member-1", snapshot_label="",
                  config_hash=CONFIG_BASELINE, prepare=lambda: holder["timeline"],
                  run=run_cross, validate=validate_view)

    # I1/W2：提交新记录后的首查与二查
    holder.clear()
    holder["timeline"] = MemoryTimeline(store, MemoryVisibility(store, SCOPE_A, False))
    holder["timeline"].view(SCOPE_A, now)
    def validate_i1(result, _prep):
        assert result is not None

    from .instrument import DEFAULT_REPEATS, measure
    for repeat in range(1, DEFAULT_REPEATS + 1):
        store.commit(new_revision_records(500 + repeat),
                     idempotency_key=f"perf-p05-i1-{repeat}")
        for state in ("I1", "W2"):
            with measure() as metrics:
                outcome = run_view(holder["timeline"])
            validate_i1(outcome, None)
            ctx.recorder.record(case_id="P05", condition=f"view.{state.lower()}_after_commit",
                                fixture_id=ctx.size, cache_state=state, scope_label="group_a",
                                principal_label="member-1", snapshot="", config_hash=CONFIG_BASELINE,
                                repeat=repeat, **metrics)

    # expand：展开一个月份
    month = "2026-01"

    def run_expand(timeline):
        return timeline.expand(month, scope=SCOPE_A, budget_chars=12000)

    timed_repeats(ctx.recorder, case_id="P05", condition="expand.month",
                  fixture_id=ctx.size, cache_state="W1", scope_label="group_a",
                  principal_label="member-1", snapshot_label="",
                  config_hash=CONFIG_BASELINE, warmup=1,
                  prepare=lambda: holder["timeline"], run=run_expand,
                  validate=lambda result, _p: assert_true(result is not None))


# ---------------------------------------------------------------- P06

def case_p06(ctx):
    source = build_fixture(ctx, ctx.size)
    new_revision_records(0)

    def extra_appends(count: int):
        rows = []
        for number in range(1, count):
            rows.extend(new_revision_records(number))
        return rows

    conditions = {
        "update_append_1": 1,
        "update_append_100": 100,
    }
    if getattr(ctx, 'quick', False):
        conditions = {"update_append_1": 1}
    for name, count in conditions.items():
        holder = {}

        def prepare(count=count):
            work = copy_fixture(source, ctx.work_dir / f"p06-{name}")
            store = ArchiveStore(work / "archive", agent_id="warmagent",
                                 user_id="synthetic-shared")
            index = MemoryIndex(store)
            asyncio.run(index.rebuild())  # 起始索引就绪(准备阶段，不计分)
            old_watermark = index.watermark
            rows = []
            for number in range(count):
                rows.extend(new_revision_records(number))
            store.commit(rows, idempotency_key=f"perf-p06-{name}")
            if store.snapshot() == old_watermark:
                raise AssertionError("append did not advance archive watermark")
            holder["counters"] = Counters()
            holder["counters"].wrap(index, "rebuild", "index_rebuild_calls")
            holder["index"] = index
            holder["old_watermark"] = old_watermark
            holder["appended_refs"] = [f"event_i1_{number}" for number in range(count)]
            return index

        def run_update(index):
            return asyncio.run(index.update())

        def validate_update(generation, index):
            assert generation, "update未返回generation"
            assert index.watermark != holder["old_watermark"], "update未推进索引水位"
            assert index.watermark == index.store.snapshot(), "索引水位未追上归档"

        timed_repeats(ctx.recorder, case_id="P06", condition=name,
                      fixture_id=ctx.size, cache_state="C1", scope_label="-",
                      principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                      timeout_ms=180000, prepare=prepare, run=run_update,
                      validate=validate_update,
                      counters_from=lambda prep: holder["counters"])

    if getattr(ctx, 'quick', False):
        return  # 有效append条件已实际执行rebuild，短轮不重复建同一大索引。

    # 完整rebuild
    holder = {}

    def prepare_rebuild():
        work = copy_fixture(source, ctx.work_dir / "p06-rebuild")
        store = ArchiveStore(work / "archive", agent_id="warmagent",
                             user_id="synthetic-shared")
        index = MemoryIndex(store)
        holder["counters"] = Counters()
        holder["counters"].wrap(index, "rebuild", "index_rebuild_calls")
        holder["index"] = index
        return index

    def run_rebuild(index):
        return asyncio.run(index.rebuild())

    def validate_rebuild(generation, _prep):
        assert generation, "rebuild未返回generation"

    timed_repeats(ctx.recorder, case_id="P06", condition="full_rebuild",
                  fixture_id=ctx.size, cache_state="C1", scope_label="-",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  timeout_ms=180000, prepare=prepare_rebuild, run=run_rebuild,
                  validate=validate_rebuild,
                  counters_from=lambda prep: holder["counters"])


# 小于1ms的纯函数微基准：固定100次一批，报告batch_total/100(方案§3.1)
def micro_batch(fn, per_batch: int = 100):
    def run(_prep):
        for _ in range(per_batch):
            fn()
        return fn.__name__
    return run
