"""Quarantine invalid batch candidates while retaining strict appraisal evidence."""
from myatri.cognition.emotion import APPRAISAL_SCHEMA, EmotionAgent as CoreEmotionAgent
from myatri.cognition.tasks import proposal_for
from myatri.contracts import ModelRequest, Record, SourceSpan, TemporaryEntry, digest, parse_timestamp
from myatri.contracts.emotion import AppraisalMeaning, EmotionAppraisal, appraisal_id
from myatri.persona.state import PersonaState
from myatri.persona.temporary import resolve


class EmotionAgent(CoreEmotionAgent):
    async def propose_batch(self, event_refs):
        """One bounded appraisal request for several events; sources stay separate."""
        if not self.config.enabled or not self.config.llm_appraisal:
            return None
        if getattr(self.gateway.provider, "name", "") == "fixture":
            return None
        import json
        from copy import deepcopy
        from myatri.prompts import load_prompt
        snapshot = self.store.snapshot()
        persona = PersonaState(self.store).read_state(snapshot)
        run_id = "run_emotion_batch_" + digest([persona.ref, sorted(set(event_refs))])[:24]
        try:
            self.store.get_record(run_id)
            return None
        except KeyError:
            pass
        events = []
        for ref in event_refs:
            event = resolve(self.store, ref, snapshot)
            if event.record_kind != "event" or event.payload.get("reality_scope") not in {"reported", "observed"}:
                continue
            try:
                self.store.get_record(appraisal_id(ref))
            except KeyError:
                events.append(event)
        if not events:
            return None
        events = sorted(events, key=lambda e: e.ref)[:6]
        row_schema = deepcopy(APPRAISAL_SCHEMA)
        definitions = row_schema.pop("$defs", {})
        row_schema["required"].append("event_ref")
        row_schema["properties"]["event_ref"] = {"type": "string"}
        schema = {"type": "object", "additionalProperties": False, "$defs": definitions,
            "required": ["appraisals"], "properties": {"appraisals": {"type": "array", "minItems": len(events),
                                                                       "maxItems": len(events), "items": row_schema}}}
        material = [{"event_ref": e.ref, "text": e.body_markdown[:800], "evidence": [
            {"message_ref": s["message_ref"], "quote": s["quote"][:400]} for s in e.payload["source_spans"][:4]]} for e in events]
        response = await self.gateway.generate(ModelRequest(role="emotion", purpose="emotion_batch",
            output_schema=schema, budget_tokens=80000, max_tokens=4096,
            messages=[{"role": "system", "content": load_prompt("emotion") +
                "\n合并评价这些独立事件对角色的意义。每条保持event_ref，quote只能来自该事件的evidence。"
                "这是延后批量评价，不声称是事件发生当时已产生的体验。不得用同批后续事件改写较早事件的依据。"
                "不把用户情绪当角色情绪，不把二十轮计数当变化证据；可逐条defer。"},
                {"role": "user", "content": json.dumps({"persona": persona.body_markdown[:1600], "events": material}, ensure_ascii=False)}]))
        import jsonschema
        data = json.loads(response.content)
        jsonschema.validate(data, schema)
        generator = {"provider": response.provider, "model": response.model, "prompt_version": "emotion/v1", "run_id": run_id}
        refs = list(dict.fromkeys([persona.ref, *[e.ref for e in events],
            *[s["message_ref"] for e in events for s in e.payload["source_spans"]]]))
        records, seen, decisions = [], set(), []
        run_ref = run_id + "@1"
        lookup = {e.ref: e for e in events}
        for item in data["appraisals"]:
            try:
                if item["event_ref"] not in lookup or item["event_ref"] in seen:
                    raise ValueError("emotion batch cites unshown or duplicate event")
                seen.add(item["event_ref"])
                if item["decision"] != "propose":
                    decisions.append(item)
                    continue
                event = lookup[item["event_ref"]]
                meaning = AppraisalMeaning.model_validate(item["appraisal"])
                spans = []
                for evidence in item["evidence"]:
                    if not evidence["quote"].strip():
                        raise ValueError("emotion batch quote is whitespace only")
                    matched = next((s for s in event.payload["source_spans"] if s["message_ref"] == evidence["message_ref"] and evidence["quote"] in s["quote"]), None)
                    if not matched:
                        raise ValueError("emotion batch quote outside event evidence")
                    raw = resolve(self.store, evidence["message_ref"], snapshot)
                    start = raw.body_markdown.find(evidence["quote"], matched["start"], matched["end"])
                    if start < 0:
                        raise ValueError("emotion batch quote not verbatim")
                    spans.append(SourceSpan(message_ref=raw.ref, start=start, end=start+len(evidence["quote"]), quote=evidence["quote"]))
                observed = max(parse_timestamp(resolve(self.store, s["message_ref"], snapshot).created_at) for s in event.payload["source_spans"])
                appraisal = EmotionAppraisal(**meaning.model_dump(), event_ref=event.ref, persona_ref=persona.ref,
                    observed_at=observed.isoformat(), source_spans=spans, evidence_refs=[event.ref])
            except ValueError as exc:
                decisions.append({"event_ref": item["event_ref"], "decision": "quarantine",
                                  "reason": str(exc), "candidate": item})
                continue
            decisions.append(item)
            record_id = appraisal_id(event.ref)
            record = Record(record_id=record_id, record_kind="emotion_appraisal", agent_id=self.store.agent_id,
                user_id=self.store.user_id, author_kind="model", generator=generator,
                source_refs=[*refs, run_ref], body_markdown=meaning.meaning, payload=appraisal.model_dump(mode="json"))
            records.append(record)
            if meaning.concern:
                entry = TemporaryEntry(text=meaning.concern, kind_hint="emotional_echo", scope="相关经历与关系",
                    origin_refs=[event.ref, record.ref], reason=item["reason"])
                records.append(Record(record_id=record_id+"_echo", record_kind="temporary_state", agent_id=self.store.agent_id,
                    user_id=self.store.user_id, author_kind="model", generator=generator, source_refs=[event.ref, record.ref],
                    body_markdown=entry.text, payload=entry.model_dump()))
        for ref in sorted(set(lookup) - seen):
            decisions.append({"event_ref": ref, "decision": "defer",
                              "reason": "emotion batch omitted this event; no appraisal applied"})
        run = Record(record_id=run_id, record_kind="cognitive_run", agent_id=self.store.agent_id, user_id=self.store.user_id,
            source_refs=refs, payload={"role": "emotion_batch", "decision": "batch", "provider": response.provider,
                "model": response.model, "retrospective": True,
                "event_refs": [e.ref for e in events], "decisions": decisions})
        records.insert(0, run)
        proposal = proposal_for(snapshot, [resolve(self.store, ref, snapshot) for ref in refs], records, "emotion", run)
        proposal.read_versions.update({r.record_id: 0 for r in records})
        return proposal

