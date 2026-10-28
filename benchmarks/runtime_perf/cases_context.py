"""P08工具上下文/图片投影 / P09文件与分页。"""

from __future__ import annotations

import json

from myatri.conversation.budget import TokenBudget

from warmagent.artifacts import ArtifactStore
from warmagent.background_jobs import BackgroundJobs
from warmagent.document_tools import read_document
from warmagent.runtime_events import EventJournal
from warmagent.tool_context import ToolContext
from warmagent.workspace_view import WorkspaceView

from .fixtures import text_file
from .instrument import Counters, timed_repeats

CONFIG_BASELINE = "baseline-current"


def harness_history(count: int, receipt_bytes: int, *, with_images: int = 0) -> list[dict]:
    """宿主回执格式的协议历史：assistant.tool_calls → tool(host_receipt_id)配对。"""
    receipt = ("合成的长工具回执正文。" * max(1, receipt_bytes // 33))[:max(64, receipt_bytes)]
    messages: list[dict] = []
    for index in range(count):
        if index % 3 == 2:
            call_id = f"call-{index}"
            messages.append({"role": "assistant", "content": "",
                             "tool_calls": [{"id": call_id, "type": "function",
                                             "function": {"name": "harness__fixture_read",
                                                          "arguments": json.dumps({"path": f"syn/{index}"})}}]})
            body: dict = {"host_receipt_id": call_id, "result": {"text": receipt},
                          "result_preview": receipt[:300], "status": "completed"}
            if with_images and index % 6 == 2:
                body["image_manifest"] = {"file": f"manifests/{index}.json", "sha256": "0" * 64,
                                          "image_count": 1}
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": json.dumps(body, ensure_ascii=False)})
        else:
            role = "user" if index % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"合成消息{index}"})
    return messages


def _receipt_body(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "tool":
            return json.loads(message["content"])["result"]["text"]
    raise AssertionError("fixture must contain a tool receipt")


# ---------------------------------------------------------------- P08

def case_p08(ctx):
    budget = TokenBudget(limit=24000, output_reserve=512, safety=64)
    journal_path = ctx.work_dir / "p08-events.sqlite3"
    holder = {}

    def setup_state(messages, *, journal: bool, consume: bool, view: WorkspaceView | None):
        state = {"items": {}, "observed_call_ids": []}
        tool_context = ToolContext(state, journal=EventJournal(journal_path) if journal else None,
                                   scope="synthetic-warmagent-runtime/group", turn="turn-perf",
                                   view=view)
        for message in messages:
            if message.get("role") != "tool":
                continue
            call_id = message["tool_call_id"]
            receipt = json.loads(message["content"])
            tool_context.result(call_id, "harness__fixture_read", receipt["result"])
        if consume:
            tool_context.consume(messages)
        return tool_context

    # 主条件矩阵：20/100/300条 × 64KiB/1MiB × 未消费/已消费
    for count in (20, 100, 300):
        for size_label, receipt_bytes in (("64k", 64 * 1024), ("1m", 1024 * 1024)):
            if count == 300 and size_label == "1m":
                continue  # 300×1MiB与300×64k同构，控制总时长
            messages = harness_history(count, receipt_bytes)
            full_body = _receipt_body(messages)
            for consumed in (False, True):
                state_label = "consumed" if consumed else "unconsumed"
                holder["messages"] = messages
                holder["consumed"] = consumed

                def prepare(consumed=consumed, messages=messages):
                    holder["tc"] = setup_state(messages, journal=False,
                                               consume=consumed, view=None)
                    return holder["tc"]

                def run(tc):
                    return tc.project(holder["messages"], [], budget)

                def validate(projected, _p, consumed=consumed, full_body=full_body):
                    stats = holder["tc"].state
                    assert stats["folded_results"] == (1 if consumed else 0) * (
                        sum(1 for m in holder["messages"] if m.get("role") == "tool")), \
                        f"fold计数不符: {stats['folded_results']}"
                    if not consumed:
                        assert any(isinstance(m.get("content"), str) and full_body[:64]
                                   in m["content"] for m in projected
                                   if m.get("role") == "tool"), "未消费回执不得折叠"
                    else:
                        assert stats["projected_estimated_tokens"] < stats["original_estimated_tokens"], \
                            "折叠后token应下降"

                timed_repeats(ctx.recorder, case_id="P08",
                              condition=f"project.{count}x{size_label}.{state_label}",
                              fixture_id=ctx.size, cache_state="W1" if consumed else "C1",
                              scope_label="group", principal_label="-", snapshot_label="",
                              config_hash=CONFIG_BASELINE, prepare=prepare, run=run,
                              validate=validate,
                              extras={"result_count": len(messages)})

    # EventJournal落库对照(100×64k)
    messages = harness_history(100, 64 * 1024)
    holder["messages"] = messages

    def prepare_journal():
        holder["tc"] = setup_state(messages, journal=True, consume=False, view=None)
        return holder["tc"]

    def run_journal(tc):
        tc.project(messages, [], budget)
        tc.consume(messages)  # 事件claim/ack路径
        return tc.project(messages, [], budget)

    timed_repeats(ctx.recorder, case_id="P08", condition="project.100x64k.journal_consume",
                  fixture_id=ctx.size, cache_state="W1", scope_label="group",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  prepare=prepare_journal, run=run_journal,
                  validate=lambda outcome, _p: assert_true(outcome is not None))

    # 图片折叠：旧图已观察+新图未观察 → 旧折叠新保留
    images_messages = harness_history(30, 64 * 1024, with_images=8)
    holder["messages"] = images_messages
    image_calls = [m["tool_call_id"] for m in images_messages
                   if m.get("role") == "tool" and "image_manifest"
                   in json.loads(m["content"])]
    older_image_call = image_calls[-2]  # 消费到它：更早图片折叠，它成为最新已观察
    latest_image_call = image_calls[-1]  # 未观察，必须保留

    def prepare_images():
        state = {"items": {}, "observed_call_ids": []}
        tool_context = ToolContext(state, scope="synthetic-warmagent-runtime/group",
                                   turn="turn-perf")
        for message in images_messages:
            if message.get("role") == "tool":
                receipt = json.loads(message["content"])
                tool_context.result(message["tool_call_id"], "harness__fixture_read",
                                    receipt["result"])
        # 仅消费到倒数第二个图片观察之前：旧图已看，最新图未看
        seen_messages = []
        for message in images_messages:
            seen_messages.append(message)
            if message.get("role") == "tool" and message["tool_call_id"] == older_image_call:
                break
        tool_context.consume(seen_messages)
        holder["tc"] = tool_context
        return tool_context

    def run_images(tc):
        return tc.project(images_messages, [], budget)

    def validate_images(projected, _p):
        stats = holder["tc"].state
        assert stats["folded_results"] >= 1, "旧图回执应折叠"
        kept = [m for m in projected if m.get("role") == "tool"
                and m.get("tool_call_id") == latest_image_call]
        assert kept and "image_manifest" in json.loads(kept[0]["content"]), "最新图片观察必须保留"

    timed_repeats(ctx.recorder, case_id="P08", condition="project.images_old_fold_new_kept",
                  fixture_id=ctx.size, cache_state="W1", scope_label="group",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  prepare=prepare_images, run=run_images, validate=validate_images)

    # TokenBudget.estimate 纯函数微基准(方案§3.1：100次/批)
    messages_100 = harness_history(100, 64 * 1024)

    def run_estimate(_prep):
        for _ in range(100):
            budget.estimate(messages_100, [])
        return "estimated"

    timed_repeats(ctx.recorder, case_id="P08", condition="budget_estimate.x100_batch",
                  fixture_id=ctx.size, cache_state="W1", scope_label="-",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  prepare=lambda: None, run=run_estimate,
                  validate=lambda outcome, _p: assert_true(outcome == "estimated"))


def assert_true(value, message="expected truthy"):
    assert value, message


# ---------------------------------------------------------------- P09

def case_p09(ctx):
    work = ctx.work_dir / "p09"
    work.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(work / "artifacts.sqlite3", work, allow_external=False)

    # register/resolve 1MiB文本
    text_1m = text_file(work / "doc-1m.txt", 1024 * 1024)
    holder = {}

    def prepare_register():
        holder["counters"] = Counters()
        holder["counters"].wrap(store, "_hash", "bytes_read")
        return text_1m

    def run_register(path):
        return store.register("call-perf", "scope-a", path, "text/plain")

    def validate_register(artifact, _p):
        assert artifact and artifact.get("id")

    timed_repeats(ctx.recorder, case_id="P09", condition="artifact.register_1mib",
                  fixture_id=ctx.size, cache_state="C1", scope_label="-",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  prepare=prepare_register, run=run_register, validate=validate_register,
                  counters_from=lambda _p: holder["counters"])

    artifact_id = store.register("call-perf", "scope-a", text_1m, "text/plain")["id"]

    def run_resolve(_prep):
        return store.resolve("scope-a", artifact_id)

    timed_repeats(ctx.recorder, case_id="P09", condition="artifact.resolve_1mib",
                  fixture_id=ctx.size, cache_state="W1", scope_label="-",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  warmup=1, prepare=lambda: None, run=run_resolve,
                  validate=lambda outcome, _p: assert_true(outcome is not None))

    # 文档分页：1MiB/50MiB 首页/中页/末页；read_document全文件读取在报告中注明
    for size_label, size_bytes in (("1m", 1024 * 1024), ("50m", 50 * 1024 * 1024)):
        path = text_file(work / f"doc-{size_label}.txt", size_bytes)
        probe = read_document(str(path), offset=0, limit=1200)
        total = probe["total_chars"]
        pages = {"first": 0, "middle": total // 2, "last": max(0, total - 2000)}
        for page_label, offset in pages.items():
            def run_page(_prep, offset=offset):
                return read_document(str(path), offset=offset, limit=1200)

            def validate_page(result, _p):
                assert result.get("text"), "分页不得返回空文本"
                assert result.get("sha256")

            timed_repeats(ctx.recorder, case_id="P09",
                          condition=f"read_document.{size_label}.{page_label}",
                          fixture_id=ctx.size, cache_state="C1", scope_label="-",
                          principal_label="-", snapshot_label="",
                          config_hash=CONFIG_BASELINE, prepare=lambda: None,
                          run=run_page, validate=validate_page,
                          extras={"bytes_read": size_bytes,
                                  "note": "read_document按实现读取全文后切片，bytes_read=文件大小"})

    # BackgroundJobs.page：1MiB spill输出 首页/中页/末页
    jobs = BackgroundJobs(work / "background-jobs.sqlite3")
    spill = text_file(work / "job-output.txt", 1024 * 1024)
    jobs.record("scope-a", "task-perf", "job-perf", turn="turn-perf", session_id="s1")
    assert jobs.observe_completed("scope-a", "task-perf", {
        "event_id": "job-perf", "job_id": "job-perf", "scope": "scope-a",
        "task": "task-perf", "turn": "turn-perf", "session_id": "s1",
        "status": "completed",
        "output_ref": {"spill_paths": [str(spill)]},
        "output_preview": "", "output_complete": True}), "observe_completed被拒"
    total_chars = len(spill.read_text(encoding="utf-8"))
    for page_label, offset in (("first", 0), ("middle", total_chars // 2),
                               ("last", max(0, total_chars - 3000))):
        def run_job_page(_prep, offset=offset):
            return jobs.page("scope-a", "job-perf", offset=offset, limit=4000)

        def validate_job_page(result, _p):
            assert result and result.get("text"), "job分页不得为空"

        timed_repeats(ctx.recorder, case_id="P09",
                      condition=f"background_page.1mib.{page_label}",
                      fixture_id=ctx.size, cache_state="C1", scope_label="-",
                      principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                      prepare=lambda: None, run=run_job_page, validate=validate_job_page,
                      extras={"bytes_read": total_chars,
                              "note": "page按实现读取全部spill后切片"})
