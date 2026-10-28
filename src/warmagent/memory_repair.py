"""Bridge-only candidate isolation. Original identity/evidence validators still run."""
import asyncio
import json
import re
import time
from copy import copy, deepcopy

from myatri.cognition.association import AssociationAgent as CoreAssociation, ASSOCIATION_SCHEMA
from myatri.cognition.experience import (ExperienceOrganizer as CoreOrganizer, _group_item, _group_source,
    EXPERIENCE_SCHEMA, GROUP_ATTRIBUTION_RULES, IngestProgressPending, message_windows)
from myatri.cognition.materials import json_bytes, select_materials
from myatri.cognition.tasks import cognitive_call, proposal_for
from myatri.contracts import Edge, ModelRequest, ModelResponse, Record, digest
from myatri.prompts import load_prompt

from .short_memory import evidence_key
from .latency import run_sync


def _original_quote(original, quote):
    """Restore only an unambiguous whitespace-normalized source fragment."""
    if not isinstance(quote, str) or not evidence_key(quote):
        return None
    if quote in original:
        return quote
    pattern = re.escape(evidence_key(quote)).replace(r"\ ", r"\s+")
    matches = list(re.finditer(pattern, original))
    return matches[0].group() if len(matches) == 1 else None


class UnresolvedCandidates(ValueError):
    """No validated candidate remains; do not mark the source as organized."""


class ExperienceOrganizer(CoreOrganizer):
    def _record_latency(self, name, started):
        timing_sink = getattr(self, "_timing_sink", None)
        if timing_sink is not None:
            elapsed = round(time.perf_counter() - started, 4)
            timing_sink(name, elapsed)

    async def _timed_sync(self, name, function, *args, **kwargs):
        started = time.perf_counter()
        try:
            return await run_sync(function, *args, **kwargs)
        finally:
            self._record_latency(name, started)

    async def _timed_await(self, name, awaitable):
        started = time.perf_counter()
        try:
            return await awaitable
        finally:
            self._record_latency(name, started)

    async def _commit(self, commit, proposal):
        return await self._timed_sync("proposal_commit", commit, proposal)

    async def _core_proposal(self, message, *, extraction):
        def build():
            # This path only builds a proposal from already prepared evidence;
            # it has no provider awaits, so its archive reads stay off the loop.
            scoped = copy(self)
            scoped.store = self.store.read_view()
            return asyncio.run(super(ExperienceOrganizer, scoped).propose(message, extraction=extraction))

        return await self._timed_sync("proposal_build", build)

    async def _draft_or_generate(self, request, identifier, enabled):
        if enabled:
            try:
                saved = await self._timed_sync("saved_draft_read", self.store.get_record, identifier)
                return ModelResponse(content=json.dumps(saved.payload["output"], ensure_ascii=False),
                    provider=saved.payload["provider"], model=saved.payload["model"])
            except KeyError:
                pass
        return await self._timed_await("model_generate", self.gateway.generate(request))

    async def organize_batch(self, messages, commit, *, max_new_chunks=8, timing_sink=None):
        """Extract many short turns in one bounded request, retaining raw roots.

        Immutable batch checkpoints survive failures/restarts. Each original
        message still gets its own A1 commit and exact source coverage.
        """
        # Keep the immutable cutoff when rebuilding batch checkpoint identities;
        # partial per-message commits must not turn paid results into a new batch.
        self._timing_sink = timing_sink
        cutoff = []
        started = time.perf_counter()
        cutoff_store = await self._timed_sync('cutoff_snapshot', self.store.read_view)
        for message in messages:
            run_id = "run_exp_" + digest({"message": message.ref, "version": "v1"})[:24]
            try:
                run = await self._timed_sync("message_run_read", cutoff_store.get_record, run_id)
            except KeyError:
                run = None
            # SummaryArchive evidence was already covered outside A1 batches;
            # its plain original text must not enter transport-envelope parsing.
            if run is None or run.payload.get("decision") != "covered_by_validated_group_summary":
                cutoff.append(message)
        self._record_latency("cutoff_filter", started)
        messages = cutoff
        cutoff_reader = copy(self)
        cutoff_reader.store = cutoff_store
        organized = await self._timed_sync("organized_check", lambda: all(
            cutoff_reader._organized(message) for message in messages))
        if organized:
            return []
        if getattr(self.gateway.provider, "name", "") == "fixture":
            for message in messages:
                if await self._timed_sync("organized_check", self._organized, message):
                    continue
                proposal = await self._timed_await("proposal_build", self.propose(message))
                await self._commit(commit, proposal)
            return [m.ref for m in messages]
        schema = deepcopy(EXPERIENCE_SCHEMA)
        schema["properties"]["events"]["maxItems"] = 12
        item = schema["properties"]["events"]["items"]
        item["required"].append("message_ref")
        item["properties"]["message_ref"] = {"type": "string"}
        groups, current = [], []
        for message in messages:
            if message.payload.get("role") != "user":
                raise ValueError("batch memory only accepts user messages")
            source = _group_source(message)
            for start, end, fragment in message_windows(message):
                row = {"ref": message.ref, "start": start, "end": end, "text": fragment, "created_at": message.created_at}
                if source:
                    row["channel_source"] = {key: value for key, value in source.items() if key not in {"text", "start", "end"}}
                if current and json_bytes([*current, row]) > 18000:
                    groups.append(current)
                    current = []
                current.append(row)
        if current:
            groups.append(current)
        checkpoints, outputs, calls = [], [], 0
        for group in groups:
            # Keep completed pre-clock checkpoints reusable after upgrading.
            identity = [{k: v for k, v in row.items() if k != "created_at"} for row in group]
            key = "run_exp_batch_" + digest([identity, "v1"])[:24]
            try:
                checkpoint = await self._timed_sync("batch_checkpoint_read", self.store.get_record, key)
            except KeyError:
                if calls >= max_new_chunks:
                    raise IngestProgressPending("batch A1 checkpoints saved; more original windows remain")
                def select_existing_materials():
                    existing = self.store.list_records("event")
                    selection = select_materials(self.store, [r.ref for r in existing],
                        query=" ".join(r["text"] for r in group), max_bytes=3500, max_records=3,
                        max_record_bytes=1000, include_counters=False)
                    return existing, selection
                existing, selection = await self._timed_sync(
                    "batch_material_selection", select_existing_materials)
                request = ModelRequest(role="experience", purpose="A1_batch", prompt_version="experience/v1",
                    budget_tokens=40000, max_tokens=4096, output_schema=schema,
                    messages=[{"role": "system", "content": load_prompt("experience") +
                        "\n合并整理一批用户原话，只提取值得持续记忆的事实、偏好、约定与变化。"
                        "每个event必须有message_ref和该原文窗口中的逐字quote。不要把闲聊或角色回答当用户事实。"
                        "连续性策略 continuity/v1：同一实体尽量沿用稳定ID；对同一事实的叙事明确标记延续、修正或终止，修正/终止必须给出可见旧事件引用；共现只记录带来源的共同出现，不推断关系或身份。"
                        "用户明确临时使用的信息不要提为长期事实。existing_event_ref只能来自已展示旧事件。" + GROUP_ATTRIBUTION_RULES},
                        {"role": "user", "content": json.dumps({"messages": group,
                            "existing_events": selection.material}, ensure_ascii=False)}])
                review_enabled = any(self._requires_review(row.get("channel_source")) for row in group)
                response = await self._timed_await(
                    "draft_response", self._draft_response(request, key + "_draft", review_enabled))
                data = json.loads(response.content)
                import jsonschema
                jsonschema.validate(data, schema)
                available = {r["ref"] for r in selection.material}
                for item in data["events"]:
                    if not any(row["ref"] == item["message_ref"] and item["quote"] in row["text"] for row in group):
                        raise ValueError("batch A1 quote outside the named original window")
                    if item.get("existing_event_ref") and item["existing_event_ref"] not in available:
                        raise ValueError("batch A1 references unshown prior event")
                await self._timed_sync("draft_commit", self._save_draft, key + "_draft", response,
                    data, [*[r["ref"] for r in group], *available], review_enabled)
                data, rejected = self._isolate_attribution(data, messages)
                data, support = await self._timed_await("support_review", self._review_support(data, messages))
                checkpoint = Record(record_id=key, record_kind="cognitive_run", agent_id=self.store.agent_id,
                    user_id=self.store.user_id, source_refs=list(dict.fromkeys([*[r["ref"] for r in group], *available])),
                    payload={"role": "A1_batch", "strategy_version": "continuity/v1", "output": data, "provider": response.provider,
                             "model": response.model, "source_windows": group, "support_review": support,
                             "rejected_candidates": rejected})
                await self._timed_sync("batch_checkpoint_commit", self.store.commit, [checkpoint], key,
                    expected={key: 0})
                calls += 1
            checkpoints.append(checkpoint)
            for item in checkpoint.payload["output"]["events"]:
                window = next(row for row in checkpoint.payload["source_windows"]
                              if row["ref"] == item["message_ref"] and item["quote"] in row["text"])
                outputs.append({**item, "_window_start": window["start"], "_window_end": window["end"],
                    "_generator": {"provider": checkpoint.payload["provider"], "model": checkpoint.payload["model"],
                                   "prompt_version": "experience/v1", "run_id": checkpoint.record_id}})
        for message in messages:
            if await self._timed_sync("organized_check", self._organized, message):
                continue
            data = {"events": [{k: v for k, v in item.items() if k != "message_ref"}
                               for item in outputs if item["message_ref"] == message.ref]}
            proposal = await self._timed_await(
                "proposal_build", self.propose(message, extraction=(data, checkpoints)))
            await self._commit(commit, proposal)
        return [m.ref for m in messages]

    def _audit(self, rejected, refs):
        identifier = "bridge_quarantine_" + digest([refs, rejected])[:24]
        try:
            return self.store.get_record(identifier)
        except KeyError:
            row = Record(record_id=identifier, record_kind="cognitive_run", agent_id=self.store.agent_id,
                user_id=self.store.user_id, source_refs=list(dict.fromkeys(refs)),
                payload={"role": "bridge_memory_validation", "decision": "quarantined",
                         "rejected_candidates": rejected})
            self.store.commit([row], identifier, expected={identifier: 0})
            return row

    def _candidate_error(self, message, item, shown=None):
        source = _group_source(message)
        quote = item.get("quote")
        original = source["text"] if source else message.body_markdown
        # Exact substring first; the normalised comparison only rescues WeChat
        # separator/whitespace artefacts, never rewording.
        restored = _original_quote(original, quote)
        if restored is None:
            return "quote_not_exact"
        item["quote"] = restored
        try:
            normalized, attribution = _group_item(message, item)
        except ValueError as error:
            return getattr(error, "code", "invalid_attribution")
        prior_ref = item.get("existing_event_ref")
        if not prior_ref:
            return None
        if shown is not None and prior_ref not in shown:
            return "unshown_prior_event"
        try:
            rid, revision = prior_ref.rsplit("@", 1)
            prior = self.store.get_record(rid, int(revision))
        except (KeyError, ValueError, AttributeError):
            return "missing_prior_event"
        if prior.record_kind != "event" or prior.payload.get("reality_scope") != item.get("reality_scope"):
            return "prior_event_scope_mismatch"
        if source:
            if sorted(prior.payload.get("participants", [])) != sorted(normalized["participants"]):
                return "merge_member_ownership"
            previous = [r.payload.get("attribution") for r in self.store.list_records("interpretation")
                        if prior.ref in r.payload.get("event_refs", [])]
            if not any(isinstance(old, dict) and all(old.get(k) == attribution.get(k)
                    for k in ("speaker_id", "subject_id", "attribution")) for old in previous):
                return "merge_attribution_ownership"
        return None

    async def _draft_response(self, request, identifier, enabled):
        if request.purpose != "A1_batch":
            request = request.model_copy(deep=True)
            request.messages[0]['content'] += (
                '\n连续性策略 continuity/v1：沿用材料中的稳定实体ID；叙事说明有证据的延续、修正或终止，'
                '后续事实另建事件，原证据不覆盖；只有展示的旧事件可引用。带来源标签的共现仅作关联，'
                '不能证明身份、因果或支持事实。')
            request.prompt_version += '+continuity/v1'
            return await self._draft_or_generate(request, identifier, enabled)
        # Keep the original request budget; allow a bounded longer JSON response.
        request = request.model_copy(update={"max_tokens": max(request.max_tokens, 8192)})
        source = json.loads(request.messages[-1]["content"])
        windows = source["messages"]
        shown = {r["ref"] for r in source.get("existing_events", [])}
        refs = list(dict.fromkeys(r["ref"] for r in windows))
        retry_id = identifier + "_candidate_retry_v1"
        saved_retry = None
        if enabled:
            try:
                saved_retry = await self._timed_sync("schema_retry_read", self.store.get_record, retry_id)
            except KeyError:
                pass
        if saved_retry is not None:
            response = ModelResponse(content=json.dumps(saved_retry.payload["output"], ensure_ascii=False),
                provider=saved_retry.payload["provider"], model=saved_retry.payload["model"])
        else:
            response = await self._draft_or_generate(request, identifier, enabled)
        for attempt in range(2):
            data = json.loads(response.content)
            accepted, rejected = [], []
            for index, item in enumerate(data["events"]):
                ref = item.get("message_ref")
                matches = [row for row in windows if row["ref"] == ref]
                code = "quote_outside_named_window" if not any(
                    evidence_key(item["quote"]) and (item["quote"] in row["text"]
                    or evidence_key(item["quote"]) in evidence_key(row["text"]))
                    for row in matches) else None
                if not code:
                    rid, rev = ref.rsplit("@", 1)
                    message = await self._timed_sync("candidate_source_validation",
                        self.store.get_record, rid, int(rev))
                    code = await self._timed_sync("candidate_validation", self._candidate_error,
                        message, item, shown)
                if code:
                    rejected.append({"index": index, "message_ref": ref if ref in refs else refs[0],
                        "status": "unconfirmed", "reason_code": code, "candidate": item})
                else:
                    accepted.append(item)
            if rejected:
                audit = await self._timed_sync("quarantine_commit", self._audit, rejected, refs)
                # Core checkpoints retain this extra provenance when committing per-message runs.
                self._bridge_audits = [*getattr(self, "_bridge_audits", []), audit]
            if accepted or not rejected:
                return response.model_copy(update={"content": json.dumps({"events": accepted}, ensure_ascii=False)})
            if attempt == 1 or saved_retry is not None:
                raise UnresolvedCandidates("all A1 candidates quarantined; sources remain pending")
            feedback = {"instruction": "上一份候选均未通过确定性证据校验。请依据原文和认证渠道字段修正，"
                        "按原schema重新生成完整JSON；不要修改来源身份，也不要保留不符合证据的合并引用。",
                        "validator_feedback": [{"index": item["index"], "reason_code": item["reason_code"]}
                                               for item in rejected]}
            retry = request.model_copy(update={"messages": [*request.messages,
                {"role": "user", "content": json.dumps(feedback, ensure_ascii=False)}]})
            response = await self._timed_await("schema_retry_generate", self.gateway.generate(retry))
            # Persist either outcome before semantic validation, so restarting
            # a failed candidate repair cannot purchase the same retry again.
            await self._timed_sync("schema_retry_commit", self._save_draft, retry_id, response,
                json.loads(response.content), [*refs, *shown], enabled)

    async def propose(self, message, *, fixture=None, extraction=None):
        if extraction is None:
            return await super().propose(message, fixture=fixture, extraction=extraction)
        data, checkpoints = extraction
        accepted, rejected = [], []
        for index, item in enumerate(data["events"]):
            code = await self._timed_sync("candidate_validation", self._candidate_error, message, item)
            if code:
                rejected.append({"index": index, "message_ref": message.ref, "status": "unconfirmed",
                    "reason_code": code, "candidate": item})
            else:
                accepted.append(item)
        audits = list(getattr(self, "_bridge_audits", []))
        # Recover audit provenance after a crash between a saved batch and per-message commits.
        audits.extend(await self._timed_sync("quarantine_recovery_read", lambda: [
            r for r in self.store.list_records("cognitive_run")
            if r.payload.get("role") == "bridge_memory_validation" and message.ref in r.source_refs]))
        if rejected:
            audits.append(await self._timed_sync("quarantine_commit", self._audit, rejected, [message.ref]))
            if not accepted:
                raise UnresolvedCandidates("all A1 candidates quarantined; sources remain pending")
        combined = {r.ref: r for r in [*audits, *checkpoints]}
        return await self._core_proposal(message,
            extraction=({"events": accepted}, list(combined.values())))


class AssociationAgent(CoreAssociation):
    async def propose(self, refs):
        available = set(refs)
        pairs = [e for e in self.index.graph.edges if e["type"] == "similar"
                 and (e["from_ref"] in available or e["to_ref"] in available)][:30]
        candidates = list(dict.fromkeys([*refs, *[e[k] for e in pairs for k in ("from_ref", "to_ref")]]))
        selection = select_materials(self.store, candidates)
        selected = {r.ref for r in selection.sources}
        pairs = [e for e in pairs if {e["from_ref"], e["to_ref"]} <= selected]
        allowed = {frozenset((e["from_ref"], e["to_ref"])) for e in pairs}
        snapshot, sources, data, generator, run = await cognitive_call(self.store, self.gateway,
            "association", candidates, "只对以下候选对建立语义假设；相似不等于支持。不得生成similar。"
            "from_ref、to_ref必须逐字使用同一候选对；evidence_refs只能来自本次材料。无完整候选时defer。\n" +
            json.dumps([{k: e[k] for k in ("from_ref", "to_ref", "type")} for e in pairs]),
            ASSOCIATION_SCHEMA, selection=selection)
        source_refs = {r.ref for r in sources}
        accepted, rejected, seen = [], [], set()
        for raw in data["edges"] if data["decision"] == "propose" else []:
            edge = Edge.model_validate(raw)
            if (edge.type == "similar" or not edge.evidence_refs or edge.edge_id in seen
                    or frozenset((edge.from_ref, edge.to_ref)) not in allowed
                    or not {edge.from_ref, edge.to_ref, *edge.evidence_refs} <= source_refs):
                rejected.append({"reason_code": "association_outside_candidates", "candidate": raw})
                continue
            # Generated IDs must not overwrite or merge a pre-existing archive object.
            try:
                self.store.get_record(edge.edge_id)
            except KeyError:
                pass
            else:
                rejected.append({"reason_code": "association_existing_id", "candidate": raw})
                continue
            try:
                record = Record(record_id=edge.edge_id, record_kind="edge", agent_id=self.store.agent_id,
                    user_id=self.store.user_id, author_kind="model", generator=generator,
                    source_refs=list(dict.fromkeys([edge.from_ref, edge.to_ref, *edge.evidence_refs, run.ref])),
                    body_markdown=f"{edge.from_ref} {edge.type} {edge.to_ref}", payload=edge.model_dump())
            except ValueError:
                rejected.append({"reason_code": "association_invalid_record", "candidate": raw})
                continue
            seen.add(edge.edge_id)
            accepted.append(record)
        if rejected:
            value = run.model_dump()
            value["payload"].update(rejected_candidates=rejected,
                decision="propose_with_quarantine" if accepted else "defer_with_quarantine")
            value["content_hash"] = ""
            run = Record.model_validate(value)
        records = [run, *accepted]
        return proposal_for(snapshot, sources, records, "A2", run)
