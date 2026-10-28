"""Bridge-local adapter for the frozen myatri 0.2.7 conversation protocol.

respond mirrors vendor 0.2.7, with bounded model projections, read coverage and
lightweight audits. Keep retry/idempotency/budget/checkpoint semantics aligned
when updating that dependency. No changes to site-packages or the core checkout.
"""
from copy import deepcopy
import inspect
import json
import logging
import time

import jsonschema

from myatri.contracts import ModelRequest, digest
from myatri.conversation.serialization import compact_json
from myatri.conversation.engine import Conversation, TOOLS
from myatri.providers.gateway import ProviderError, assistant_message, validate_protocol
from .tool_reading import (READING_RULES, FIELDS, cold_page, guard_reading_claim, read_audit,
                           read_page, recall_directory)
from .bubbles import prepare_bubbles
from .tool_arguments import normalize_arguments
from .tool_catalog import ToolCatalog
from .tool_context import ToolContext, environment_message
from .request_context import insert_current_context, request_measurement, record_request_usage
from .chat_style import apply_chat_style
from .latency import run_sync
from .chat_batches import BATCH_REPORT_SCHEMA, BATCH_REPORT_RULES, unknown_outcome
from .chat_silence import NO_REPLY_NAME, NO_REPLY_SCHEMA, NO_REPLY_RULES, NoReply
from .tool_policy import (DIRECT_TOOL_RULES, action_kind, operation_seen,
                          unsupported_action_prose, image_capability_denial, guard_image_capability)

logger = logging.getLogger(__name__)


READING_TOOLS = deepcopy(TOOLS)
READING_TOOLS[0]["function"]["description"] = "搜索记忆目录；摘录不是已读原文，用read_record读取精确版本"
reader = READING_TOOLS[1]["function"]
reader["description"] = "分页读取精确ID@revision；按read_progress.first_unread_offset补缺页，末页不代表读完"
reader["parameters"]["properties"]["ref"]["maxLength"] = 256
reader["parameters"]["properties"]["field"] = {"type": "string", "enum": list(FIELDS)}


class ReadingConversation(Conversation):
    @classmethod
    def adapt(cls, conversation, chat_options=None):
        # Transfer the already constructed hooks, context, lock and shared
        # budgets without reinitializing runtime state or changing its class.
        adapted = cls.__new__(cls)
        adapted.__dict__.update(conversation.__dict__)
        options = chat_options or {}
        adapted.thinking = options.get("thinking", False)
        adapted.reasoning_effort = options.get("reasoning_effort", "high")
        adapted.thinking_max_tokens = options.get("thinking_max_tokens", 32768)
        adapted.thinking_timeout = options.get("thinking_timeout", 240)
        adapted.max_steps = options.get('max_tool_steps', adapted.max_steps)
        if type(adapted.max_steps) is not int or not 1 <= adapted.max_steps <= 64:
            raise ValueError('max_tool_steps must be between 1 and 64')
        if (type(adapted.thinking) is not bool
                or not isinstance(adapted.reasoning_effort, str)
                or adapted.reasoning_effort not in {"low", "high", "max"}
                or type(adapted.thinking_max_tokens) is not int
                or not 1024 <= adapted.thinking_max_tokens <= 393216
                or type(adapted.thinking_timeout) not in {int, float}
                or not 0 < adapted.thinking_timeout <= 600):
            raise ValueError("invalid conversation thinking settings")
        if adapted.thinking:
            adapted.context.budget.output_reserve = max(
                adapted.context.budget.output_reserve, adapted.thinking_max_tokens)
        return adapted

    async def respond(self, text, *, message_id=None, metadata=None, resume_batch=False):
        started = time.perf_counter()
        async with self._lock:
            acquired = time.perf_counter()
            timings = {"lock_wait_seconds": acquired-started, "model_seconds": 0.,
                       "guard_seconds": 0., "tool_seconds": 0.}
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("message metadata must be a dict")
            if metadata and set(metadata) & {"role", "content", "message_id"}:
                raise ValueError("message metadata cannot override the conversation identity")
            raw = await run_sync(self.store.append_message, "user", text,
                message_id=message_id, **(metadata or {}))
            existing = self.protocol_store.get_protocol(raw.record_id) if self.protocol_store else None
            unread = bool(getattr(self, 'is_unread_batch', lambda _: False)(raw.record_id))
            seen = list((existing or {}).get('context_stats', {}).get('unread_keys', [raw.record_id]))
            additions = await self.collect_unread(raw.record_id, seen) if unread else []
            try:
                answer = await run_sync(self.store.get_record,
                    existing['reply_ref'].rsplit('@', 1)[0] if unread and existing and existing.get('reply_ref') else "answer_" + raw.record_id)
            except KeyError:
                pass
            else:
                if not additions:
                    if self.on_answer:
                        await run_sync(self.on_answer, answer)
                    return answer
            if existing and existing.get('decision') == 'no_reply':
                return NoReply(existing['reason'])
            if existing and existing.get('status') == 'batch_waiting' and not resume_batch:
                return self._batch_answer(raw, existing)
            # Queue ingestion before API wait; user text remains safe if the provider fails.
            if self.on_message and not (metadata or {}).get('host_event'):
                await run_sync(self.on_message, raw)
            # Build an immutable per-turn view; shared group handler state must
            # never carry an owner's authority into another member's request.
            handlers = dict(self.tool_handlers)
            if getattr(self, "turn_tool_factory", None):
                turn_handlers = self.turn_tool_factory(raw)
                if inspect.isawaitable(turn_handlers):
                    turn_handlers = await turn_handlers
                handlers.update(turn_handlers)
            tools = [*READING_TOOLS, *[entry[0] for entry in handlers.values()]] if self.enable_tools else []
            completed = await run_sync(lambda: sum(1 for r in self.store.list_records("raw_message")
                            if r.payload.get("role") == "assistant" and r.payload.get("reply_to")))
            checkpoint = (completed + 1) % self.context.trace_every_turns == 0
            timings["input_seconds"] = time.perf_counter()-acquired
            context_started = time.perf_counter()
            saved = self.protocol_store.get_protocol(raw.record_id) if self.protocol_store else None
            catalog = ToolCatalog(handlers, ((saved or {}).get('context_stats', {})
                .get('tool_catalog', {}).get('expanded', [])))
            handlers = catalog.handlers
            all_tools = [*READING_TOOLS, *[entry[0] for entry in handlers.values()]]
            tools = [*READING_TOOLS, *catalog.schemas()] if self.enable_tools else []
            if saved and saved["status"] == "context_reset":
                raise ProviderError("turn belongs to a cleared context; use a new message ID")
            delegated = ('computer_control' in handlers and 'instructions' in
                         handlers['computer_control'][0]['function']['parameters'].get('properties', {}))
            from .computer_control import command_body, explicit_computer_query, ComputerTaskPending
            source_text = raw.payload.get('channel_source', {}).get('text', '')
            if unread:
                source_text = '\n'.join([source_text, *[a['source']['text'] for a in additions]])
            source = raw.payload.get('channel_source', {})
            direct_available = any(getattr(handler[1], 'execute_call', None) for handler in handlers.values())
            requested_action = (source.get('owner_action_kind') or action_kind(source_text)) if direct_available else None
            owner_action = ((saved or {}).get('owner_action') or {
                'kind': requested_action, 'goal': source.get('action_followup_goal') or source_text,
                'tool_attempted': False}) if requested_action else None
            action_corrected = bool((saved or {}).get('action_corrected'))
            command = command_body(source_text)
            delegation_required = delegated and ((command is not None and command.startswith('任务 '))
                                                 or explicit_computer_query(source_text))
            image_available = (self.enable_tools and 'harness__read_image' in handlers
                and getattr(self.gateway, 'vision_root', None) is not None)
            optional_reply = (source.get('trigger_kind') in {'random', 'attention'}
                and source.get('conversation_kind', 'group') == 'group'
                and source.get('transport') != 'local_cli' and not owner_action
                and not delegation_required and not (metadata or {}).get('host_event')
                and self.protocol_store is not None)
            if optional_reply:
                all_tools.append(NO_REPLY_SCHEMA)
            if self.enable_tools and "computer_control" in handlers and not delegated:
                # /pc is already an exact, owner-bound host command. Dispatch it
                # once without asking the chat model whether it has this ability.
                # The independent Harness agent still plans and executes the goal.
                from .computer_control import command_receipt
                result = handlers["computer_control"][1]()
                if inspect.isawaitable(result):
                    result = await result
                body = command_receipt(result)
                answer = self.store.append_message("assistant", body,
                    message_id="answer_" + raw.record_id, reply_to=raw.ref,
                    provider="host", model="computer-command", reply_parts=[body],
                    computer_command_receipt={k: result[k] for k in
                        ("task_id", "state", "status", "error") if k in result})
                if self.protocol_store:
                    self.protocol_store.save_protocol(raw.record_id, {
                        "status": "completed", "steps": 0, "reply_ref": answer.ref,
                        "sent_at": raw.created_at, "replied_at": answer.created_at,
                        "computer_command": True, "full_checkpoint": False,
                        "tool_audit": [], "reading_guard": None,
                        "latency": {"total_seconds": round(time.perf_counter()-started, 4),
                                    "model_seconds": 0.0},
                    })
                if self.on_answer:
                    self.on_answer(answer)
                return answer
            if saved and (saved["status"] in {"active", 'batch_waiting'} or unread and saved.get('unread_resume')):
                messages = deepcopy(saved.get('unread_resume') or saved['messages'])
                snapshot, start_step = saved["snapshot"], saved["steps"]
                turn_stats = deepcopy(saved.get("context_stats", {}))
                validate_protocol(messages, complete=False)
            else:
                messages, snapshot = await self.context.abuild_context(text, tools=tools, persist_activation=checkpoint)
                turn_stats = deepcopy(self.context.explain_context().get("context_stats", {}))
                start_step = 0
            if unread:
                turn_stats.setdefault('unread_keys', [raw.record_id])
                turn_stats.setdefault('unread_inputs', [])
                self._append_unread(messages, turn_stats, additions)
            messages = apply_chat_style(messages, getattr(self.context, 'chat_style_enabled', False))
            batch = turn_stats.setdefault('tool_batch', {'index': 1, 'start_step': 0, 'state': 'running'})
            action_corrected = action_corrected or turn_stats.get('capability_corrected', False)
            if saved and saved.get('status') == 'batch_waiting' and resume_batch:
                batch.update(index=batch['index'] + 1, start_step=start_step, state='running')
                messages.append({'role': 'system', 'content': '宿主续跑原任务，保留已有回执；本批下一步（数据）：' +
                    (saved.get('batch_report') or {}).get('next_step', '')})
            step_limit = max(start_step + 1, batch['start_step'] + self.max_steps)
            tool_context = ToolContext(turn_stats.setdefault('tool_context', {}),
                journal=getattr(self, 'runtime_events', None),
                scope=getattr(self.context, 'group_scope', ''), turn=raw.record_id,
                view=getattr(self, 'workspace_view', None), outcome_sink=getattr(self, 'outcome_sink', None))
            turn_stats['tool_catalog'] = catalog.state()
            # Resume the original call, never a separate report-generation chat.
            # Re-dispatch is idempotent: the source message fixes the task ID and
            # an existing task returns its persisted state without repeating effects.
            waiting = saved.get('computer_wait') if saved else None
            if waiting:
                if 'computer_control' not in handlers:
                    result = {'error': 'computer_owner_required'}
                else:
                    result = await handlers['computer_control'][1](**waiting['arguments'])
                for item in messages:
                    if item.get('role') == 'tool' and item.get('tool_call_id') == waiting['call_id']:
                        item['content'] = compact_json(result)
                        break
                else:
                    raise ProviderError('computer continuation missing its tool call')
                pending = result.get('task_id') and result.get('state') in {'queued', 'running', 'pausing', 'cancelling'}
                self.protocol_store.save_protocol(raw.record_id, {'status': 'active', 'snapshot': snapshot,
                    'steps': start_step, 'messages': messages, 'context_stats': turn_stats,
                    **({'computer_wait': waiting} if pending else {})})
                if pending:
                    raise ComputerTaskPending(result['task_id'])
            # An interrupted tool round must be closed before it is sent back
            # to the model. Direct Harness calls reuse the original call ID so
            # its durable ledger returns a receipt or an unknown result.
            for call in _unmatched_tool_calls(messages):
                call_id = call.get('id')
                function = call.get('function', {})
                name = function.get('name')
                try:
                    args = json.loads(function.get('arguments', '{}'))
                    definition = next(t['function'] for t in all_tools
                                      if t['function']['name'] == name)
                    jsonschema.validate(normalize_arguments(args, definition['parameters']), definition['parameters'])
                    entry = handlers.get(name)
                    execute_call = getattr(entry[1], 'execute_call', None) if entry else None
                    if execute_call:
                        result = await execute_call(call_id, args)
                    else:
                        result = {'error': 'interrupted_tool_call_not_replayed'}
                except Exception:
                    result = {'error': 'interrupted_tool_call_outcome_unknown'}
                messages.append({'role': 'tool', 'tool_call_id': call_id,
                                 'content': compact_json(result)})
                if isinstance(result, dict) and result.get('host_receipt_id'):
                    tool_context.result(call_id, name, result)
                self._save_active_protocol(raw, snapshot, start_step, messages, turn_stats,
                    reasoning_content=saved.get('reasoning_content', '') if saved else '',
                    owner_action=owner_action, action_corrected=action_corrected)
            validate_protocol(messages)
            timings["context_seconds"] = time.perf_counter()-context_started
            preparation = self.context.explain_context().get("preparation_timings", {})
            rules = READING_RULES
            if optional_reply:
                rules += NO_REPLY_RULES
            if catalog.guide:
                from .harness_guide import LOCAL_RESOURCE_RULES
                rules += '\n' + LOCAL_RESOURCE_RULES
            if direct_available:
                rules += '\n' + DIRECT_TOOL_RULES
            if image_available:
                rules += ('\n本轮已授权harness__read_image且图像输入已启用，可先在files类别发现、定位文件再读图。'
                    '未执行或被新消息打断只能说明本轮没有读到，不得据此说只能读文字或没有视觉能力。')
            if "computer_control" in handlers:
                from .computer_control import RULES
                rules = rules + "\n" + RULES
            if "web_search" in self.tool_handlers:
                from .web_tools import WEB_SEARCH_RULES
                # The tool description covers "how"; this rule covers "when".
                rules = rules + "\n" + WEB_SEARCH_RULES
            previous_rules = next((m for m in messages if m.get('role') == 'system'
                and m.get('content', '').startswith(READING_RULES)), None)
            if previous_rules is not None:
                previous_rules['content'] = rules
            elif getattr(self.context, 'cache_prefix_enabled', False):
                insert_current_context(messages, {'role': 'system', 'content': rules})
            else:
                # The first system block contains the stable persona; per-turn
                # capabilities/silence rules must not invalidate that prefix.
                index = 1 if messages and messages[0].get('role') == 'system' else 0
                messages.insert(index, {"role": "system", "content": rules})
            if direct_available and source.get('action_followup_goal'):
                followup = '本轮继续的owner目标（宿主保存的数据）：' + compact_json({
                    key: source.get(key) for key in ('action_followup_goal', 'action_followup_turn',
                                                     'action_followup_delivery_status')})
                followup += '。结合上一轮真实结果继续；未确认的效果先核对，不重复提交。'
                if not any(m.get('role') == 'system' and m.get('content') == followup for m in messages):
                    last_user = max(i for i, m in enumerate(messages) if m.get('role') == 'user')
                    messages.insert(last_user, {'role': 'system', 'content': followup})
            delegation_calls = {call['id'] for m in messages for call in m.get('tool_calls', [])
                                if call.get('function', {}).get('name') == 'computer_control'}
            task_submitted = False
            delegation_attempted = False
            for message in messages:
                if message.get('role') == 'tool' and message.get('tool_call_id') in delegation_calls:
                    try:
                        result = json.loads(message['content'])
                        task_submitted = task_submitted or bool(result.get('task_id'))
                        delegation_attempted = True
                    except (ValueError, AttributeError):
                        pass
            for step in range(start_step, step_limit):
                tools = [*READING_TOOLS, *catalog.schemas()] if self.enable_tools else []
                current_start = max(i for i, m in enumerate(messages) if m.get('role') == 'user')
                can_be_silent = optional_reply and not any(m.get('tool_calls') for m in messages[current_start+1:])
                if can_be_silent:
                    tools = [*tools, NO_REPLY_SCHEMA]
                yield_reason = getattr(self, 'batch_should_yield', lambda _: '')(raw.record_id)
                yield_requested = bool(yield_reason)
                report_only = step == step_limit - 1 or yield_requested
                if report_only:
                    tools = []
                turn_stats['tool_catalog'] = catalog.state()
                request_messages = tool_context.project(messages, tools, self.context.budget)
                environment = getattr(self, 'environment_getter', None)
                snapshot_state = environment() if environment else None
                personal = {}
                personal_getter = getattr(self, 'personal_context_getter', None)
                if personal_getter:
                    snapshot_state = dict(snapshot_state or {})
                    snapshot_state.pop('personal', None)
                    personal = await run_sync(personal_getter, source_text)
                    if personal:
                        snapshot_state['personal'] = personal
                insert_current_context(request_messages, environment_message(loaded_tools=len(tools),
                    snapshot=snapshot_state,
                    navigation=catalog.navigation() if self.enable_tools else None))
                if report_only:
                    request_messages.append({'role': 'system', 'content': BATCH_REPORT_RULES})
                    if yield_requested:
                        request_messages.append({'role': 'system', 'content':
                            '本轮让位原因：' + yield_reason + '。这是调度暂停，不是能力缺失或工具故障；'
                            '只报告实际完成部分与尚未执行的步骤，新消息处理后可沿断点继续。'})
                reply_policy = self.context.reply_policy
                output_tokens = self.context.budget.output_reserve
                if self.thinking:
                    output_tokens = self.thinking_max_tokens
                elif reply_policy.maximum:
                    output_tokens = min(output_tokens, reply_policy.max_tokens)
                request = ModelRequest(messages=request_messages, tools=tools, role="foreground", purpose="conversation",
                                       output_schema=BATCH_REPORT_SCHEMA if report_only else None,
                                       budget_tokens=self.context.budget.limit, max_tokens=output_tokens,
                                       thinking=self.thinking, reasoning_effort=self.reasoning_effort,
                                       timeout=self.thinking_timeout if self.thinking else 45,
                                       estimated_input_tokens=self.context.budget.estimate(request_messages, tools))
                self.context.budget.require(request_messages, tools)
                measurement, encoded = request_measurement(request_messages, tools,
                    getattr(self, '_previous_request_bytes', None))
                self._previous_request_bytes = encoded
                model_started = time.perf_counter()
                response = await self.gateway.generate(request)
                timings["model_seconds"] += time.perf_counter()-model_started
                record_request_usage(turn_stats, measurement, response.usage)
                self.context.budget.calibrate(request_messages, response.usage, tools)
                if unread:
                    additions = await self.collect_unread(raw.record_id, turn_stats['unread_keys'])
                    if additions:
                        # A stale draft is not sent. Pair any unexecuted calls before
                        # continuing the same session so earlier receipts remain valid.
                        if response.tool_calls:
                            messages.append(assistant_message(response))
                            messages.extend({'role': 'tool', 'tool_call_id': call['id'],
                                'content': compact_json({'not_executed': True, 'reason': 'new_unread_messages'})}
                                for call in response.tool_calls)
                        self._append_unread(messages, turn_stats, additions)
                        source_text += '\n' + '\n'.join(a['source']['text'] for a in additions)
                        requested_action = action_kind(source_text) if direct_available else None
                        if requested_action and not owner_action:
                            owner_action = {'kind': requested_action, 'goal': source_text, 'tool_attempted': False}
                        self._save_active_protocol(raw, snapshot, step+1, messages, turn_stats,
                            owner_action=owner_action, action_corrected=action_corrected)
                        continue
                if any(call.get('function', {}).get('name') == NO_REPLY_NAME for call in response.tool_calls):
                    if not can_be_silent or report_only or len(response.tool_calls) != 1:
                        raise ProviderError('group_no_reply must be a standalone decision before tools')
                    call = response.tool_calls[0]
                    args = json.loads(call['function']['arguments'])
                    jsonschema.validate(args, NO_REPLY_SCHEMA['function']['parameters'])
                    messages.extend([assistant_message(response), {'role': 'tool', 'tool_call_id': call['id'],
                        'content': compact_json({'status': 'skipped', 'reason': args['reason']})}])
                    validate_protocol(messages)
                    self.protocol_store.save_protocol(raw.record_id, {
                        'status': 'completed', 'decision': 'no_reply', 'reason': args['reason'],
                        'steps': step+1, 'snapshot': snapshot, 'messages': messages,
                        'context_stats': turn_stats, 'full_checkpoint': False,
                        'latency': {**timings, 'total_seconds': round(time.perf_counter()-started, 4)}})
                    return NoReply(args['reason'])
                tool_context.consume(messages)
                if report_only:
                    report = json.loads(response.content)
                    jsonschema.validate(report, BATCH_REPORT_SCHEMA)
                    if response.tool_calls:
                        raise ProviderError('batch report must not execute tools')
                    if yield_reason in {'paused', 'cancelled', 'owner_message'} and report['state'] != 'completed':
                        report.update(state='continue', next_step=report['next_step'] or '核对原任务断点后继续。')
                    if report['state'] == 'continue' and not report['next_step'].strip():
                        raise ProviderError('continuation requires a concrete next step')
                    if report['state'] in {'continue', 'completed'} and any(unknown_outcome(json.loads(m['content']))
                            for m in messages if m.get('role') == 'tool'):
                        report.update(state='blocked', next_step='')
                        report['report'] += '\n有操作结果尚未确认，已停止自动续跑，保留回执待核对。'
                    report['report'], _ = guard_reading_claim(report['report'], read_audit(messages))
                    if image_available:
                        report['report'] = guard_image_capability(report['report'])
                    response = response.model_copy(update={'content': report['report']})
                    batch.update(state=report['state'], steps=step+1)
                    if report['state'] == 'continue':
                        messages.append(assistant_message(response))
                        payload = {'status': 'batch_waiting', 'snapshot': snapshot, 'steps': step+1,
                            'messages': messages, 'context_stats': turn_stats, 'tool_audit': read_audit(messages),
                            'batch_report': {**report, 'provider': response.provider, 'model': response.model}}
                        if owner_action:
                            payload.update(owner_action=owner_action, action_corrected=action_corrected)
                        self.protocol_store.save_protocol(raw.record_id, payload)
                        return self._batch_answer(raw, payload)
                if self.protocol_store and checkpoint:
                    self.protocol_store.set_meta("context_token_anchor", self.context.budget.anchor)
                if (image_available and not report_only and not response.tool_calls
                        and image_capability_denial(response.content) and not action_corrected
                        and step + 1 < step_limit):
                    messages.append(assistant_message(response))
                    messages.append({'role': 'system', 'content':
                        '当前目录提供harness__read_image且图像输入已启用。上一条把本轮未读到误说成没有读图能力。'
                        '先发现files类别并按上下文定位图片再读取；若失败，只说明实际路径/格式/回执问题，不扩大为能力缺失。'})
                    action_corrected = True
                    turn_stats['capability_corrected'] = True
                    self._save_active_protocol(raw, snapshot, step+1, messages, turn_stats,
                        owner_action=owner_action, action_corrected=action_corrected)
                    continue
                if not report_only and delegation_required and not delegation_attempted and not task_submitted and not response.tool_calls:
                    # One explicit correction, preserving the main model's role
                    # as delegator instead of silently substituting host prose.
                    if step > start_step:
                        raise ProviderError('computer delegation missing; task was not submitted')
                    messages.append(assistant_message(response))
                    messages.append({'role': 'system', 'content':
                        '当前已核验用户明确提交电脑任务。请生成instructions并调用computer_control委派Harness，'
                        '不能只回复做不到或声称已接单。工具返回实际执行报告后，再回答用户。'})
                    continue
                if owner_action and not operation_seen(messages, requested_action) and not response.tool_calls:
                    if not report_only and not action_corrected and step + 1 < step_limit:
                        messages.append(assistant_message(response))
                        messages.append({'role': 'system', 'content':
                            '本轮尚无对应操作工具调用。先理解对方是否在认真交代事情；明显玩笑或无意义机械重复可简短说明不接，不必启动工具。'
                            '认真且决定承接的任务，请直接使用已授权工具执行可行步骤，必要时先查记忆定位路径；不要再索要相同授权或只许诺稍后做。'
                            '若确有无法补齐的条件，准确指出缺项；不具备视觉不妨碍尝试可用的信息提取。'
                            + catalog.correction_hint(source_text)})
                        action_corrected = True
                        self._save_active_protocol(raw, snapshot, step+1, messages, turn_stats,
                            owner_action=owner_action, action_corrected=action_corrected)
                        continue
                    if unsupported_action_prose(response.content):
                        response = response.model_copy(update={'content':
                            '这次尚未执行，当前也没有后台任务记录。'})
                if not response.tool_calls:
                    if self.reply_guard is not None:
                        guard_started = time.perf_counter()
                        checked = await self.reply_guard(response.content, raw, messages)
                        timings["guard_seconds"] += time.perf_counter()-guard_started
                        response = response.model_copy(update={"content": checked})
                    if owner_action and not operation_seen(messages, requested_action) and unsupported_action_prose(response.content):
                        response = response.model_copy(update={'content':
                            '这次尚未执行，当前也没有后台任务记录。'})
                    if image_available:
                        response = response.model_copy(update={'content': guard_image_capability(response.content)})
                    guarded, reading_guard = guard_reading_claim(response.content, read_audit(messages))
                    body, reply_parts = prepare_bubbles(guarded, reply_policy)
                    response = response.model_copy(update={"content": body})
                    if not response.content:
                        raise ProviderError("empty reply after metadata removal; raw user message preserved")
                unread_resume = deepcopy(messages) if unread and not response.tool_calls else None
                messages.append(assistant_message(response))
                if not response.tool_calls:
                    validate_protocol(messages)
                    if not report_only:
                        batch.update(state='completed', steps=step+1)
                    answer = await run_sync(self.store.append_message, "assistant", response.content,
                        message_id=("answer_" + raw.record_id + ('_unread_' + digest(turn_stats['unread_keys'])[:12] if unread else '')), reply_to=raw.ref,
                        provider=response.provider, model=response.model, context_snapshot=snapshot,
                        reading_guard=reading_guard, reply_parts=reply_parts,
                        personal_note_versions={note['note_id']: note['revision']
                            for note in personal.get('notebook', {}).get('notes', [])
                            if not note.get('truncated') and len(note['text']) >= 6
                            and note['text'] in response.content},
                        **({"quality_review": self.reply_guard.last_check} if self.reply_guard is not None
                           and hasattr(self.reply_guard, "last_check") else {}))
                    timings["total_seconds"] = time.perf_counter()-started
                    timings["local_other_seconds"] = max(0., timings["total_seconds"] - sum(
                        value for key, value in timings.items() if key != "total_seconds"))
                    latency = {key: round(value, 4) for key, value in timings.items()}
                    latency["context_preparation"] = preparation
                    logger.info("MYATRI reply generation latency: %s", compact_json(latency))
                    if self.protocol_store:
                        protocol = {"status": "completed", "snapshot": snapshot,
                            "steps": step+1, "reply_ref": answer.ref, "sent_at": raw.created_at,
                            "replied_at": answer.created_at, "full_checkpoint": checkpoint,
                            "tool_audit": read_audit(messages), "reading_guard": reading_guard,
                            "context_stats": turn_stats, "latency": latency}
                        if unread:
                            protocol['unread_resume'] = unread_resume
                            protocol['messages'] = messages
                        if owner_action:
                            owner_action['tool_attempted'] = operation_seen(messages, requested_action)
                            protocol.update(owner_action=owner_action, action_corrected=action_corrected)
                        if self.thinking:
                            # Provider protocol data, never character memory. Needed
                            # when a later tools-enabled request replays this answer.
                            protocol["reasoning_content"] = response.provider_fields.get("reasoning_content", "")
                        direct_calls = any(call.get('function', {}).get('name', '').startswith('harness__')
                            for m in messages for call in m.get('tool_calls', []))
                        if checkpoint or delegation_calls or task_submitted or direct_calls or owner_action:
                            protocol["messages"] = messages
                            protocol["context"] = self.context.explain_context()
                        self.protocol_store.save_protocol(raw.record_id, protocol)
                    if checkpoint:
                        self.store.flush_projection()
                    if self.on_answer:
                        await run_sync(self.on_answer, answer)
                    tool_context.event('report_generated', key='report', payload_ref=answer.ref)
                    return answer
                tools_started = time.perf_counter()
                computer_wait = None
                for call in response.tool_calls:
                    execute_call = None
                    try:
                        name = call["function"]["name"]
                        if unread and getattr(self, 'has_unread', lambda *_: False)(raw.record_id, turn_stats['unread_keys']):
                            raise ValueError('tool_not_executed_owner_interrupt')
                        if getattr(self, 'batch_should_yield', lambda _: '')(raw.record_id):
                            raise ValueError('tool_not_executed_owner_interrupt')
                        definition = next(t["function"] for t in all_tools if t["function"]["name"] == name)
                        original_args = json.loads(call["function"]["arguments"])
                        args = normalize_arguments(original_args, definition['parameters'])
                        jsonschema.validate(args, definition["parameters"])
                        entry = handlers.get(name)
                        execute_call = getattr(entry[1], 'execute_call', None) if entry else None
                        if execute_call:
                            self._save_active_protocol(raw, snapshot, step+1, messages, turn_stats,
                                reasoning_content=response.provider_fields.get('reasoning_content', ''),
                                pending_tool={'call_id': call['id'], 'name': name,
                                              'arguments': original_args, 'direct': True},
                                owner_action=owner_action, action_corrected=action_corrected)
                        if name == "recall":
                            bundle = self.recall.recall(args["query"], snapshot=snapshot, budget=1800)
                            result = recall_directory(bundle, self.store,
                                record_allowed=getattr(self.recall, "record_allowed", None))
                        elif name in handlers:
                            if name == 'computer_control' and delegated:
                                if len(response.tool_calls) != 1:
                                    raise ValueError('computer delegation must be a single tool call')
                                if step+1 >= step_limit or not self.protocol_store:
                                    raise ValueError('computer delegation requires a resumable final step')
                                computer_wait = {'call_id': call['id'], 'arguments': args}
                                # Save the call BEFORE submitting an execution task.
                                # A crash between submission and dispatch completion
                                # can resume this exact call without another LLM plan.
                                if self.protocol_store:
                                    self.protocol_store.save_protocol(raw.record_id, {
                                        'status': 'active', 'snapshot': snapshot, 'steps': step+1,
                                        'messages': [*messages, {'role': 'tool', 'tool_call_id': call['id'],
                                            'content': compact_json({'state': 'awaiting_dispatch'})}],
                                        'context_stats': turn_stats, 'computer_wait': computer_wait})
                            if execute_call:
                                result = await execute_call(call['id'], original_args)
                            else:
                                result = handlers[name][1](**args)
                                # A blocking backend (e.g. an out-of-process search) must
                                # not stall the loop shared by every group, so async
                                # handlers are awaited and run in their own worker thread.
                                if inspect.isawaitable(result):
                                    result = await result
                            if name == 'computer_control':
                                delegation_attempted = True
                                task_submitted = bool(result.get('task_id'))
                            if name == "read_group_message":
                                result = cold_page(result, args["message_id"], read_audit(messages))
                        else:
                            result = read_page(self.store, args["ref"], snapshot, read_audit(messages),
                                offset=args.get("offset", 0), limit=args.get("limit", 800), field=args.get("field"),
                                record_allowed=getattr(self.recall, "record_allowed", None))
                        # Bounded result, without truncating JSON/protocol fields.
                        encoded = compact_json(result)
                        if len(encoded.encode("utf-8")) > 6000:
                            result = {"error": "record exceeds tool result budget", "ref": args.get("ref"), "suggestion": "use recall with narrower query"}
                    except (ValueError, KeyError, StopIteration, jsonschema.ValidationError) as exc:
                        result = ({'error': 'owner_interrupted', 'not_executed': True}
                            if str(exc) == 'tool_not_executed_owner_interrupt' else
                            {"error": type(exc).__name__, "message": "tool arguments or source invalid"})
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": compact_json(result)})
                    if isinstance(result, dict) and result.get('host_receipt_id'):
                        tool_context.result(call['id'], name, result)
                    turn_stats['tool_catalog'] = catalog.state()
                    if execute_call:
                        self._save_active_protocol(raw, snapshot, step+1, messages, turn_stats,
                            reasoning_content=response.provider_fields.get('reasoning_content', ''),
                            owner_action=owner_action, action_corrected=action_corrected)
                timings["tool_seconds"] += time.perf_counter()-tools_started
                if self.protocol_store:
                    pending = (computer_wait and result.get('task_id') and
                               result.get('state') in {'queued', 'running', 'pausing', 'cancelling'})
                    self.protocol_store.save_protocol(raw.record_id, {"status": "active", "snapshot": snapshot,
                        "steps": step+1, "messages": messages, "tool_audit": read_audit(messages),
                        "context_stats": turn_stats,
                        **({'owner_action': owner_action, 'action_corrected': action_corrected} if owner_action else {}),
                        **({'computer_wait': computer_wait} if pending else {})})
                    if pending:
                        raise ComputerTaskPending(result['task_id'])
            raise ProviderError("tool step limit reached; raw user message preserved, no fictitious reply committed")

    @staticmethod
    def _append_unread(messages, stats, additions):
        if not additions:
            return
        stats['unread_keys'].extend(a['key'] for a in additions)
        stats['unread_inputs'].extend(a['source'] for a in additions)
        messages.append({'role': 'user', 'content': compact_json({'unread_messages': [a['source'] for a in additions]})})
        if not any(m.get('role') == 'system' and m.get('content', '').startswith('本轮按一批未读消息统一回应') for m in messages):
            messages.append({'role': 'system', 'content': '本轮按一批未读消息统一回应：按时间顺序读完全部未读消息，后文可补充或修正前文；整体回复一次，不逐条机械答复。已有工具回执仍有效，不重复执行。'})

    def _batch_answer(self, raw, protocol):
        report = protocol['batch_report']
        index = protocol['context_stats']['tool_batch']['index']
        body, parts = prepare_bubbles(report['report'], self.context.reply_policy)
        return self.store.append_message('assistant', body,
            message_id=f'answer_{raw.record_id}_batch_{index}', reply_to=raw.ref,
            provider=report['provider'], model=report['model'], reply_parts=parts,
            partial=True, task_batch=index)

    def _save_active_protocol(self, raw, snapshot, steps, messages, context_stats, *,
                               reasoning_content='', pending_tool=None, owner_action=None, action_corrected=False):
        if not self.protocol_store:
            return
        payload = {'status': 'active', 'snapshot': snapshot, 'steps': steps,
                   'messages': messages, 'tool_audit': read_audit(messages),
                   'context_stats': context_stats}
        if reasoning_content:
            payload['reasoning_content'] = reasoning_content
        if pending_tool:
            payload['pending_tool'] = pending_tool
        if owner_action:
            payload.update(owner_action=owner_action, action_corrected=action_corrected)
        self.protocol_store.save_protocol(raw.record_id, payload)


def _unmatched_tool_calls(messages):
    completed = {message.get('tool_call_id') for message in messages
                 if message.get('role') == 'tool'}
    return [call for message in messages if message.get('role') == 'assistant'
            for call in message.get('tool_calls', []) if call.get('id') not in completed]
