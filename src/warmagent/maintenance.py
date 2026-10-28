"""Bridge maintenance composition, frozen core 0.2.8 run() with local agent factories.

The core stage order is retained with sequential off-loop archive work,
foreground priority and persisted stage timing in this bridge adapter.
Upstream source SHA256: 92aeb1ddc6507e0fe1feaecd377002198058238c611af4e53b85b386bf14c553
"""
import asyncio
import time

from myatri.runtime.maintenance import Maintenance
from myatri.contracts import JobSpec, Proposal, digest
from myatri.cognition.reflection import ReflectionAgent
from myatri.cognition.temporary_state import TemporaryStateAgent
from myatri.cognition.attribution import AttributionAgent
from .memory_repair import ExperienceOrganizer, AssociationAgent, IngestProgressPending
from .emotion_agent import EmotionAgent
from .theme_agent import ThemeAgent
from .latency import run_sync


class _YieldToCompaction(IngestProgressPending):
    pass


def _select_messages(users, raw, allowed, pending_refs, deferred_refs):
    users = [r for r in raw if r.payload.get("role") == "user"
             and (r.ref in pending_refs or r.ref in deferred_refs)]
    return [r for r in users if r.ref in allowed]


def _message_event_refs(store, message_refs):
    records = store.list_records("event")
    return [r.ref for r in records if message_refs.intersection(r.source_refs)]


def _planned_event_refs(store, deep_messages, persona_messages):
    records = store.list_records("event")
    return ([r.ref for r in records if set(deep_messages).intersection(r.source_refs)],
            [r.ref for r in records if set(persona_messages).intersection(r.source_refs)])


def _user_positions(raw):
    return {r.ref: i for i, r in enumerate(raw) if r.payload.get("role") == "user"}


class BridgeMaintenance(Maintenance):
    async def run(self, refs, *, reason="turns"):
        try:
            return await self._run_locked(refs, reason=reason)
        except _YieldToCompaction:
            self.last_schedule = None
            raise

    async def before_compact(self):
        self._compaction_waiters = getattr(self, "_compaction_waiters", 0) + 1
        try:
            if not self.turns:
                return
            if self._deferred_refs():
                raise ValueError("maintenance has deferred source batches; resolve them before compaction")
            users, _ = await run_sync(self.pending)
            if users or self.app.queue.get_meta("deep_pending_messages", []) or self.app.queue.get_meta("persona_pending_messages", []):
                while True:
                    try:
                        await self.run([r.ref for r in users], reason="before_compaction")
                        break
                    except IngestProgressPending:
                        await asyncio.sleep(0)
        finally:
            self._compaction_waiters -= 1

    async def _run_locked(self, refs, *, reason="turns"):
        async with self.lock:
            pending_started = time.perf_counter()
            users, raw = await run_sync(self.pending)
            pending_seconds = round(time.perf_counter() - pending_started, 4)
            allowed = set(refs)
            # Explicit operator retry can resume a deferred cutoff even after
            # newer batches advanced the ordinary scheduling cursor.
            deferred_refs = self._deferred_refs().intersection(allowed)
            pending_refs = {m.ref for m in users}
            messages = await run_sync(_select_messages, users, raw, allowed,
                                      pending_refs, deferred_refs)
            pending_deep = self.app.queue.get_meta("deep_pending_messages", [])
            pending_persona = self.app.queue.get_meta("persona_pending_messages", [])
            forced = reason == "before_compaction"
            if not messages and not (forced and (pending_deep or pending_persona)):
                return {"kind": "noop", "maintenance": "already covered"}
            key = "maintenance_" + digest([r.ref for r in messages] if messages else [reason, pending_deep, pending_persona])[:24]
            progress = self.app.queue.get_meta(key, {})
            if "plan" not in progress:
                number = self.app.queue.get_meta("maintenance_batch_count", 0) + bool(messages)
                progress["plan"] = {"number": number,
                    "deep_messages": list(dict.fromkeys([*pending_deep, *[r.ref for r in messages]])),
                    "persona_messages": list(dict.fromkeys([*pending_persona, *[r.ref for r in messages]])),
                    "deep_due": forced or number % self.deep_every == 0,
                    "persona_due": forced or number % self.persona_every == 0}
            if forced:
                progress["plan"].update(deep_due=True, persona_due=True)
            plan = progress["plan"]
            progress.setdefault("planning_seconds", {})["pending"] = pending_seconds
            self.app.queue.set_meta(key, progress)

            async def yield_to_foreground():
                if forced:
                    return
                if getattr(self, "_compaction_waiters", 0):
                    raise _YieldToCompaction("maintenance yielded to foreground compaction")
                foreground_pending = getattr(self.app, "foreground_pending", None)
                if callable(foreground_pending):
                    while foreground_pending():
                        if getattr(self, "_compaction_waiters", 0):
                            raise _YieldToCompaction("maintenance yielded to foreground compaction")
                        await asyncio.sleep(0.05)
                if getattr(self, "_compaction_waiters", 0):
                    raise _YieldToCompaction("maintenance yielded to foreground compaction")

            async def stage(name, operation, substage_timings=None):
                if not progress.get(name):
                    waiting_started = time.perf_counter()
                    try:
                        await yield_to_foreground()
                    finally:
                        waited = round(time.perf_counter() - waiting_started, 4)
                        if waited > 0.001:
                            progress.setdefault("foreground_wait_seconds", {})[name] = waited
                            self.app.queue.set_meta(key, progress)
                    started = time.perf_counter()
                    try:
                        await operation()
                        progress[name] = True
                    finally:
                        progress.setdefault("stage_seconds", {})[name] = round(
                            time.perf_counter() - started, 4)
                        if substage_timings is not None:
                            progress.setdefault("substage_seconds", {})[name] = dict(substage_timings)
                        self.app.queue.set_meta(key, progress)

            commit = self.app.committer.submit_proposal
            if messages:
                memory_timings = {}
                organizer = ExperienceOrganizer(self.app.store, self.app.gateway)
                def record_memory_timing(name, seconds):
                    memory_timings[name] = round(memory_timings.get(name, 0.0) + seconds, 4)
                async def organize_memory():
                    await organizer.organize_batch(messages, commit,
                        timing_sink=record_memory_timing)
                await stage("memory", organize_memory, memory_timings)
                # Project only known runs from this cutoff, never scan the
                # archive. Assign by immutable run ref so replays do not add.
                quarantine_counts = self.app.queue.get_meta("a1_quarantine_counts", {})
                for message in messages:
                    run_id = "run_exp_" + digest({"message": message.ref, "version": "v1"})[:24]
                    try:
                        run = await run_sync(self.app.store.get_record, run_id)
                    except KeyError:
                        continue
                    count = run.payload.get("source_coverage", {}).get("quarantined_candidates", 0)
                    if type(count) is int and count > 0:
                        quarantine_counts[run.ref] = count
                if quarantine_counts:
                    self.app.queue.set_meta("a1_quarantine_counts", quarantine_counts)
            message_refs = {r.ref for r in messages}
            event_started = time.perf_counter()
            event_refs = await run_sync(_message_event_refs, self.app.store, message_refs)
            deep_events, persona_events = await run_sync(_planned_event_refs, self.app.store,
                plan["deep_messages"], plan["persona_messages"])
            progress.setdefault("planning_seconds", {})["event_selection"] = round(
                time.perf_counter() - event_started, 4)
            self.app.queue.set_meta(key, progress)
            if plan["deep_due"]:
                # Association/theme generation needs a complete graph. Ordinary
                # refresh jobs may defer that work; deep maintenance is a barrier.
                index_refresh = (self.app.index.rebuild if hasattr(self.app.index, 'incremental_status')
                                 else self.app.index.update)
                await stage("initial_index", index_refresh)
            for start in range(0, len(event_refs), 6):
                async def appraise(group=event_refs[start:start + 6]):
                    proposal = await EmotionAgent(self.app.store, self.app.gateway, self.app.emotions.config).propose_batch(group)
                    if proposal:
                        await run_sync(commit, proposal)
                await stage(f"emotion_{start}", appraise)

            async def state_refs():
                rows = await run_sync(self.app.states.list)
                return [r.ref for r in rows if r.payload["status"] in {"open", "paused"}]

            if event_refs or deep_events or persona_events or await state_refs():
                async def association():
                    proposal = await AssociationAgent(self.app.store, self.app.gateway, self.app.index).propose(deep_events)
                    await run_sync(commit, proposal)
                if plan["deep_due"] and deep_events and any(e["type"] == "similar" and (e["from_ref"] in deep_events or e["to_ref"] in deep_events)
                                      for e in self.app.index.graph.edges):
                    await stage("association", association)

                themes = [(tid, theme) for tid, theme in self.app.index.themes.items() if theme["needs_summary"]][:2]
                for tid, theme in themes if plan["deep_due"] else []:
                    async def summarize_theme(theme_id=tid):
                        proposal = await ThemeAgent(self.app.store, self.app.gateway, self.app.index).propose(theme_id)
                        if proposal:
                            await run_sync(commit, proposal)
                    await stage("theme_" + digest([tid, theme["material_hash"]])[:16], summarize_theme)

                async def reflect():
                    states = await state_refs()
                    proposal = await ReflectionAgent(self.app.store, self.app.gateway).propose([*deep_events, *states])
                    await run_sync(commit, proposal)
                if plan["deep_due"]:
                    await stage("reflection", reflect)

                async def temporary():
                    relevant = deep_events if forced else event_refs
                    reflection_rows = await run_sync(self.app.store.list_records, "reflection")
                    reflections = [r.ref for r in reflection_rows
                                   if relevant and set(relevant).intersection(r.source_refs)]
                    states = await state_refs()
                    proposal = await TemporaryStateAgent(self.app.store, self.app.gateway).propose(
                        [*relevant, *reflections[-2:], *states])
                    await run_sync(commit, proposal)
                if messages or forced:
                    await stage("temporary", temporary)

                async def attribution():
                    answers = await run_sync(lambda: [r.ref for r in raw
                        if r.payload.get("role") == "assistant"
                        and r.payload.get("reply_to") in plan["deep_messages"]])
                    if answers and await state_refs():
                        states = await state_refs()
                        proposal = await AttributionAgent(self.app.store, self.app.gateway).propose(
                            [*answers, *deep_events, *states])
                        await run_sync(self.app._submit_attribution, proposal)
                if plan["deep_due"]:
                    await stage("attribution", attribution)

                async def persona():
                    result = await self.app._persona_revision(JobSpec(role="persona_revision", input_refs=persona_events))
                    if result["kind"] == "revision_candidate":
                        await run_sync(commit, Proposal.model_validate(result["proposal"]))
                        if result["data"]["decision"] == "propose":
                            patch = result["data"]["patch"]
                            reviewed = await self.app._persona_review(JobSpec(role="persona_review", payload={"patch": patch}))
                            await self.app._apply(reviewed)
                if plan["persona_due"]:
                    await stage("persona", persona)

            await stage("index", self.app.index.update)
            # The cursor advances only after memory, state, emotion and review
            # dependencies completed or explicitly deferred with durable runs.
            await run_sync(self.app.store.flush_projection)
            positions = await run_sync(_user_positions, raw)
            old_cursor = self.app.queue.get_meta("maintenance_cursor")
            cursor = max([r.ref for r in messages] + ([old_cursor] if old_cursor else []),
                         key=lambda ref: positions[ref], default=None)
            deferred = self.deferred_batches()
            for item in deferred.values():
                if item["status"] == "deferred" and set(item["input_refs"]).issubset(message_refs):
                    item["status"] = "recovered"
            def pending_after(key, planned, completed):
                current = self.app.queue.get_meta(key, [])
                return ([ref for ref in current if ref not in set(planned)] if completed
                        else list(dict.fromkeys([*current, *planned])))
            self.app.queue.set_meta_many({
                "maintenance_cursor": cursor,
                "maintenance_batch_count": max(plan["number"], self.app.queue.get_meta("maintenance_batch_count", 0)),
                "maintenance_deferred_batches": deferred,
                "deep_pending_messages": pending_after("deep_pending_messages", plan["deep_messages"], plan["deep_due"]),
                "persona_pending_messages": pending_after("persona_pending_messages", plan["persona_messages"], plan["persona_due"]),
                "last_maintenance": {"reason": reason, "message_refs": [r.ref for r in messages],
                                     "event_refs": event_refs, "stages": progress}})
            return {"kind": "maintenance_done", "message_refs": [r.ref for r in messages]}
