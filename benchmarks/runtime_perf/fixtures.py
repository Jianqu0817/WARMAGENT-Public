"""合成夹具：统一种子20261025。规模=最新逻辑记录数；正文约512汉字。

三域共用一个归档：群A/群B经channel_source digest定scope，owner私聊用
conversation_kind=private隔离。生成遵循方案§4；生成耗时与复制不计入目标操作。
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import struct
import zipfile
from pathlib import Path

from myatri.contracts import Record
from myatri.storage import ArchiveStore

from warmagent.shared_runtime import digest

SEED = 20261025
SIZES = {"S": 100, "M": 1000, "L": 10000}
_COMMIT_BATCH = {"ten": 10, "hundred": 100}

PLATFORM, BOT = "fixture-platform", "fixture-bot"
GROUP_A, GROUP_B = "fixture-room-a", "fixture-room-b"
SCOPE_A = digest([PLATFORM, BOT, GROUP_A])[:32]
SCOPE_B = digest([PLATFORM, BOT, GROUP_B])[:32]
PRIVATE_SCOPE = "owner-private-fixture"


def _channel(group_id: str, *, platform_id: str = PLATFORM, bot_id: str = BOT) -> dict:
    return {"channel_source": {"platform_id": platform_id, "bot_id": bot_id,
                               "group_id": group_id, "conversation_kind": "group"}}


def _private_channel() -> dict:
    return {"channel_source": {"platform_id": PLATFORM, "bot_id": BOT,
                               "conversation_kind": "private"}}


# 填充正文用句子池拼接：自然文本的词句重复度，避免逐字随机采样导致
# TF-IDF词表膨胀(乱码文本曾把词表推到38万，稠密矩阵分配5.9GiB)。
# 句子内容刻意避开检索题关键词(读书会/茶/周五/场地/暗号等)。
_SENTENCES = (
    "夜里的灯还亮着。", "每周的纪要整理成档案。", "窗台的绿植浇了水。",
    "楼下的猫又来了。", "讨论从周一延续到月末。", "细节以原文为准。",
    "他先去了仓库清点物料。", "她把旧笔记翻出来核对。", "午饭在楼下小店解决的。",
    "会议记录放在共享目录里。", "窗外的雨下了一整天。", "下午的例会改到三点。",
    "账单已经核对过两遍。", "快递放在了门卫那里。", "晚风把窗帘吹起来。",
    "他回复说下周再确认。", "她提醒大家带好工牌。", "打印机的纸又用完了。",
    "地铁口新开了一家书店。", "储物间的钥匙挂在墙上。", "月底前要交一份总结。",
    "他把桌面收拾干净了。", "她说进度比预期慢一些。", "值班表贴在公告栏上。",
    "早高峰的路况不太好。", "小区里的桂花开得正盛。", "备用电池放在抽屉里。",
    "下午的阳光晒进走廊。", "他把邮件转发给了同组。", "日程表上还有两个空档。",
)


def body_text(seed: int, length: int = 512) -> str:
    rng = random.Random(seed)
    parts = []
    while sum(len(part) for part in parts) < length:
        parts.append(rng.choice(_SENTENCES))
    return "".join(parts)[:length]


def fact_summary(seed: int) -> str:
    return f"合成事实{seed}：" + body_text(seed, 48)


class FixtureBuilder:
    """按固定配方生成raw_message+event对，10%带一条旧修订。"""

    def __init__(self, size_label: str, commit_form: str = "hundred", *,
                 agent_id: str = "warmagent", user_id: str = "synthetic-shared",
                 platform_id: str = PLATFORM, bot_id: str = BOT,
                 group_a_id: str = GROUP_A, group_b_id: str = GROUP_B):
        self.size = SIZES[size_label]
        self.batch = _COMMIT_BATCH[commit_form]
        self.size_label, self.commit_form = size_label, commit_form
        self.agent_id, self.user_id = agent_id, user_id
        self.platform_id, self.bot_id = platform_id, bot_id
        self.group_a_id, self.group_b_id = group_a_id, group_b_id
        self.scope_a = digest([platform_id, bot_id, group_a_id])[:32]
        self.scope_b = digest([platform_id, bot_id, group_b_id])[:32]
        self.rng = random.Random(SEED + self.size)
        self.questions: dict[str, dict] = {}
        self._planted = set()

    def _record_pair(self, index: int, scope_kind: str,
                     summary_override: str | None = None) -> list[Record]:
        occurred = f"2026-{1 + index % 10:02d}-{1 + index % 28:02d}T{index % 24:02d}:00:00+00:00"
        {"group_a": "group", "group_b": "group", "private": "private"}[scope_kind]
        user_id = self.user_id
        channel = _channel(self.group_a_id if scope_kind == "group_a"
                           else self.group_b_id if scope_kind == "group_b" else "",
                           platform_id=self.platform_id, bot_id=self.bot_id).get("channel_source")
        payload_channel = {"channel_source": channel}
        if scope_kind == "private":
            payload_channel = _private_channel()
        raw_id = f"raw_{scope_kind}_{index}"
        raw = Record(record_id=raw_id, record_kind="raw_message", agent_id=self.agent_id,
                     user_id=user_id, author_kind="human", created_at=occurred,
                     body_markdown=body_text(SEED + index),
                     payload={"role": "user", "sent_at": occurred, **payload_channel})
        event_id = f"event_{scope_kind}_{index}"
        summary = summary_override or fact_summary(SEED + index)
        # 事件payload遵循Event契约且不可修订；scope隔离经source_refs追溯raw的channel_source
        event = Record(record_id=event_id, record_kind="event", agent_id=self.agent_id,
                       user_id=user_id, author_kind="system", created_at=occurred,
                       source_refs=[raw.ref], body_markdown=summary,
                       payload={"occurred_at": occurred, "fact_summary": summary,
                                "source_spans": [{"message_ref": raw.ref, "start": 0,
                                    "end": 64, "quote": body_text(SEED + index)[:64]}],
                                "event_fingerprint": event_id, "reality_scope": "reported"})
        return raw, event

    def _interpretation(self, index: int, scope_kind: str, revision: int) -> Record:
        """10%记录带一个可修订解读：revision1旧说法→revision2修正(方案§4旧修订)。"""
        occurred = f"2026-{1 + index % 10:02d}-{1 + index % 28:02d}T{index % 24:02d}:30:00+00:00"
        interp_id = f"interp_{scope_kind}_{index}"
        body = (f"旧解读{index}：先按绿茶记录。" if revision == 1
                else f"修正解读{index}：应为红茶，理由见原文。")
        return Record(record_id=interp_id, record_kind="interpretation",
                      agent_id=self.agent_id, user_id=self.user_id, author_kind="model",
                      created_at=occurred, revision=revision,
                      supersedes_revision=revision - 1 if revision > 1 else None,
                      source_refs=[f"event_{scope_kind}_{index}@1"],
                      body_markdown=body,
                      generator={"provider": "fixture", "model": "fixture-v1",
                                 "prompt_version": "perf", "run_id": f"run-{index}"},
                      payload={"note": "synthetic interpretation for revision fixture"})

    def _plant_questions(self):
        """植入四个检索题的ground truth(只植入一次,使用尾部固定索引)。"""
        self.questions = {
            "direct_fact": {
                "query": "群里约定周五下午几点开读书会？",
            "scope": self.scope_a, "expected": "event_group_a_0",
                "source": "raw_group_a_0"},
            "preference_correction": {
                "query": "后来明确纠正后，他更喜欢红茶还是绿茶？",
                "scope": SCOPE_A, "expected": "event_group_a_4",
                "counter": "event_group_a_1", "source": "raw_group_a_4"},
            "two_hop_chain": {
                "query": "读书会改期之后场地又换到了哪里？",
                "scope": SCOPE_A, "expected": "event_group_a_16",
                "chain": "event_group_a_6", "source": "raw_group_a_16"},
            "private_negative": {
                "query": "秋日暗号是什么？",
                "scope": SCOPE_A, "expected": "event_private_5",
                "source": "raw_private_5"},
        }

    def build(self, target: Path):
        target.mkdir(parents=True, exist_ok=True)
        store = ArchiveStore(target / "archive", agent_id=self.agent_id,
                             user_id=self.user_id)
        self._plant_questions()
        # 植入检索题固定语料：索引7/10/11/13群A + 5私聊，内容定制
        # 索引按plan规则选取：0/1/4/6/16落群A，5落私聊；3/13等%10==3项是修订项不用于植入
        specials = {
            ("group_a", 0): ("群A约定周五下午三点在老地方开读书会", None),
            ("group_a", 1): ("最初记录他更喜欢绿茶", None),
            ("group_a", 4): ("后来在群里明确纠正：他更喜欢红茶，绿茶记录作废", None),
            ("group_a", 6): ("读书会第一次改期到周六上午", None),
            ("group_a", 16): ("读书会改期之后，场地从老地方换到了社区二楼", None),
            ("private", 5): ("秋日暗号是枫叶", None),
        }
        written, commit_index = 0, 0
        total = self.size
        plan: list[tuple[int, str]] = []
        # 私聊约8%，群B约20%，其余群A
        for index in range(total):
            kind = "private" if index % 12 == 5 else ("group_b" if index % 5 == 2 else "group_a")
            plan.append((index, kind))

        def flush(batch: list[Record]):
            nonlocal commit_index
            commit_index += 1
            store.commit(batch,
                         idempotency_key=f"perf-fixture-{self.size_label}-{commit_index}")

        # 第一遍：全部记录以revision 1落库(修订项先写"旧说法")
        pending: list[Record] = []
        for (index, kind) in plan:
            special = specials.get((kind, index))
            override = special[0] if special else None
            raw, event = self._record_pair(index, kind, summary_override=override)
            pending.append(raw)
            pending.append(event)
            if index % 10 == 3:  # 10%旧修订：解读rev1先入库，rev2在第二遍提交
                pending.append(self._interpretation(index, kind, 1))
            written += 1
            if len(pending) >= self.batch * 3 or written == total:
                flush(pending)
                pending = []
        # 第二遍：为10%修订项提交解读revision 2；同批双修订被归档拒绝，分commit写入
        pending = []
        for (index, kind) in plan:
            if index % 10 != 3:
                continue
            pending.append(self._interpretation(index, kind, 2))
            if len(pending) >= self.batch:
                flush(pending)
                pending = []
        if pending:
            flush(pending)
        sample_event_refs = []
        for (index, kind) in plan:
            if len(sample_event_refs) >= 100:
                break
            sample_event_refs.append(f"event_{kind}_{index}")
        logical_records = total * 2 + sum(1 for index, _kind in plan if index % 10 == 3)
        versions = logical_records + sum(1 for index, _kind in plan if index % 10 == 3)
        manifest = {"size_label": self.size_label, "commit_form": self.commit_form,
                    "latest_records": logical_records, "input_pairs": total,
                    "version_records": versions, "commit_count": commit_index,
                    "seed": SEED, "scope_a": self.scope_a, "scope_b": self.scope_b,
                    "questions": self.questions, "sample_event_refs": sample_event_refs}
        (target / "fixture.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                             encoding="utf-8")
        return store, manifest


def new_revision_records(seed_index: int) -> list[Record]:
    """I1条件用：追加一条新raw+event。"""
    occurred = "2026-10-25T23:00:00+00:00"
    raw = Record(record_id=f"raw_i1_{seed_index}", record_kind="raw_message",
                 agent_id="warmagent", user_id="synthetic-shared", author_kind="human",
                 created_at=occurred, body_markdown=body_text(SEED + 90000 + seed_index, 200),
                 payload={"role": "user", "sent_at": occurred, **_channel(GROUP_A)["channel_source"]})
    event = Record(record_id=f"event_i1_{seed_index}", record_kind="event",
                   agent_id="warmagent", user_id="synthetic-shared", author_kind="system",
                   created_at=occurred, source_refs=[raw.ref],
                   body_markdown=fact_summary(SEED + 90000 + seed_index),
                   payload={"occurred_at": occurr_for(seed_index),
                            "fact_summary": fact_summary(SEED + 90000 + seed_index),
                            "source_spans": [{"message_ref": raw.ref, "start": 0, "end": 64,
                                "quote": raw.body_markdown[:64]}],
                            "event_fingerprint": f"event_i1_{seed_index}",
                            "reality_scope": "reported"})
    return [raw, event]


def occurr_for(index: int) -> str:
    return f"2026-10-25T23:{index % 60:02d}:00+00:00"


def copy_fixture(source: Path, target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return target


# ---------- 工作历史/文件/图片夹具 ----------

def work_history_messages(count: int, receipt_bytes: int, *, with_images: int = 0,
                          seed: int = 7) -> list[dict]:
    """协议消息数组：一半user/assistant交替，穿插tool轮(assistant.tool_calls→tool配对)。"""
    rng = random.Random(seed + count + receipt_bytes)
    receipt = ("合成的长工具回执。" * max(1, receipt_bytes // 30))[:max(64, receipt_bytes)]
    messages: list[dict] = []
    for index in range(count):
        if index % 3 == 2:
            call_id = f"call-{index}"
            messages.append({"role": "assistant", "content": "",
                             "tool_calls": [{"id": call_id, "type": "function",
                                             "function": {"name": "harness__fixture_read",
                                                          "arguments": json.dumps({"path": f"syn/{index}"})}}]})
            result: dict = {"result": {"text": receipt}}
            if with_images and index % 6 == 2:
                result["image_manifest"] = [{"path": f"synthetic/image-{index}.png",
                                             "hash": hashlib.sha256(str(index).encode()).hexdigest()}]
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": json.dumps(result, ensure_ascii=False)})
        else:
            role = "user" if index % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"合成消息{index}，" + body_text(seed + index, 60)})
    rng.randrange(1)  # 保持rng确定性引用
    return messages


def text_file(path: Path, size_bytes: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    block = ("小温暖文档分页性能验证。" * 40) + "\n"
    data = (block.encode("utf-8") * (size_bytes // len(block.encode("utf-8")) + 1))[:size_bytes]
    path.write_bytes(data)
    return path


def docx_file(path: Path, paragraphs: int = 800) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "<w:p><w:r><w:t>合成DOCX段落，用于分页读取验证。</w:t></w:r></w:p>"
    content = ("<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
               "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
               f"<w:body>{text * paragraphs}</w:body></w:document>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml",
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
            "<Default Extension='xml' ContentType='application/xml'/></Types>")
        archive.writestr("word/document.xml", content)
    return path


def pdf_file(path: Path, pages: int = 50) -> Path:
    """手工最小PDF：每页文本层若干行Helvetica，可被pypdf提取。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    font_id, contents_start = 1, 2
    pages_start = contents_start + pages
    catalog_id, pages_tree_id = pages_start + pages, pages_start + pages + 1
    objects: dict[int, bytes] = {}
    objects[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for page in range(pages):
        text_lines = [f"BT /F1 10 Tf 40 {760 - row * 15} Td "
                      f"(PDF page {page + 1} line {row + 1} synthetic) Tj ET"
                      for row in range(40)]
        stream = "\n".join(text_lines).encode("latin-1")
        objects[contents_start + page] = (b"<< /Length " + str(len(stream)).encode()
                                          + b" >>\nstream\n" + stream + b"\nendstream")
    for page in range(pages):
        objects[pages_start + page] = (
            f"<< /Type /Page /Parent {pages_tree_id} 0 R /MediaBox [0 0 595 842] "
            f"/Contents {contents_start + page} 0 R /Resources << /Font "
            f"<< /F1 {font_id} 0 R >> >> >>").encode()
    objects[catalog_id] = f"<< /Type /Catalog /Pages {pages_tree_id} 0 R >>".encode()
    kids = " ".join(f"{pages_start + page} 0 R" for page in range(pages))
    objects[pages_tree_id] = f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()

    body = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(body)
        body += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref_at = len(body)
    body += f"xref\n0 {pages_tree_id + 1}\n".encode()
    body += b"0000000000 65535 f \n"
    for number in range(1, pages_tree_id + 1):
        body += f"{offsets[number]:010d} 00000 n \n".encode()
    body += (f"trailer\n<< /Size {pages_tree_id + 1} /Root {catalog_id} 0 R >>\n"
             f"startxref\n{xref_at}\n%%EOF").encode()
    path.write_bytes(bytes(body))
    return path


def png_file(path: Path, width: int = 320, height: int = 240) -> Path:
    """无依赖PNG：确定性RGB噪声图。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    rows = []
    for _ in range(height):
        row = b"\x00" + bytes(rng.randrange(256) for _ in range(width * 3))
        rows.append(row)
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib_crc32(tag + data)))

    import zlib

    def zlib_crc32(data: bytes) -> int:
        return zlib.crc32(data) & 0xFFFFFFFF

    payload = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw, 6))
               + chunk(b"IEND", b""))
    path.write_bytes(payload)
    return path
