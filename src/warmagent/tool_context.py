"""Shared chat/activity projection; durable protocols retain original receipts."""

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from .context_lifecycle import ContextItem, project_tool_messages
from .request_context import insert_current_context


logger = logging.getLogger(__name__)


class ToolContext:
    def __init__(self, state, *, journal=None, scope='', turn='', task_id='', view=None, outcome_sink=None):
        self.state = state
        self.journal = journal
        self.scope, self.turn, self.task_id = scope, str(turn), task_id
        self.observed = set(state.get('observed_call_ids', []))
        self.view = view
        self.workspace = task_id or 'chat'
        self.outcome_sink = outcome_sink

    def event(self, kind, *, key, cause_id='', payload_ref=''):
        if self.journal is None:
            return ''
        try:
            return self.journal.publish(kind=kind, scope=self.scope,
                source='tool_context', task_id=self.task_id, cause_id=cause_id or self.turn,
                dedup_key=f'{self.turn}:{key}', payload_ref=payload_ref)
        except Exception:
            # A secondary projection must not turn a committed action into a retry.
            self.state['event_sync_errors'] = self.state.get('event_sync_errors', 0) + 1
            logger.exception('Runtime event projection failed')
            return ''

    def result(self, call_id, name, result):
        if self.view:
            self.workspace = self.view.active_workspace(self.scope, self.task_id or 'chat')
        reference = json.dumps({'turn': self.turn, 'call_id': call_id,
            'task_id': self.task_id}, separators=(',', ':'))
        if self.outcome_sink:
            self.outcome_sink(self.scope, f'receipt:{self.turn}:{call_id}@1', name, result)
        self.state.setdefault('items', {}).setdefault(call_id, asdict(ContextItem(
            item_id=f'{self.turn}:{call_id}', source_ref=name, lifecycle='durable',
            audience='ai', scope=self.scope, task_id=self.task_id or None,
            summary=json.dumps(result, ensure_ascii=False, separators=(',', ':'))[:300],
            payload_ref=reference, expires_at=None, observed_at=None, revision=1)))
        if self.view:
            self.view.register_context(self.scope, self.workspace, f'{self.turn}:{call_id}',
                summary=self.state['items'][call_id]['summary'], payload_ref=reference,
                observed=call_id in self.observed, has_image=bool(result.get('image_manifest')))
        event_id = self.event('tool_result_available', key='result:' + call_id,
            cause_id=call_id, payload_ref=reference)
        if event_id:
            self.state.setdefault('result_events', {})[call_id] = event_id

    def project(self, messages, tools, budget):
        consumed = self.observed
        if self.view:
            self.workspace = self.view.active_workspace(self.scope, self.task_id or 'chat')
            view = self.view.projection(self.scope, self.workspace, self.turn)
            pinned = {row['ref'][len(self.turn) + 1:] for row in view['items']
                if row['pinned'] and row['ref'].startswith(self.turn + ':')}
            consumed = set() if view.get('mode') == 'expand' else self.observed - pinned
        projected, stats = project_tool_messages(messages, consumed)
        if self.view and (view.get('current_goal') or any(row.get('note') for row in view['items'])):
            insert_current_context(projected, {'role': 'system', 'content': '当前工作视图便笺（带来源的数据）：\n' +
                json.dumps({'goal': view.get('current_goal', '')[:400], 'notes':
                    [{'ref': row['ref'], 'note': row['note'][:200]} for row in view['items'] if row.get('note')][:5]},
                    ensure_ascii=False)})
        stats['original_estimated_tokens'] = budget.estimate(messages, tools)
        stats['projected_estimated_tokens'] = budget.estimate(projected, tools)
        self.state.update(stats)
        return projected

    def consume(self, messages):
        for message in messages:
            if message.get('role') != 'tool':
                continue
            try:
                receipt = json.loads(message.get('content', ''))
            except (ValueError, TypeError):
                continue
            if not isinstance(receipt, dict) or not receipt.get('host_receipt_id'):
                continue
            call_id = message['tool_call_id']
            if call_id in self.observed:
                continue
            event_id = self.state.get('result_events', {}).get(call_id)
            if event_id and self.journal:
                try:
                    claim = self.journal.claim('main_model', self.scope, event_id=event_id)
                    if claim:
                        self.journal.ack(event_id, 'main_model', claim['claim_token'])
                except Exception:
                    self.state['event_sync_errors'] = self.state.get('event_sync_errors', 0) + 1
                    logger.exception('Runtime observation acknowledgement failed')
            self.event('tool_result_observed', key='observed:' + call_id,
                cause_id=event_id or call_id)
            item = self.state.get('items', {}).get(call_id)
            if item:
                item.update(observed_at=datetime.now(timezone.utc).isoformat(), revision=2)
                if self.view:
                    self.view.register_context(self.scope, self.workspace, f'{self.turn}:{call_id}',
                        summary=item['summary'], payload_ref=item['payload_ref'], observed=True,
                        has_image=bool(receipt.get('image_manifest')))
            self.observed.add(call_id)
        self.state['observed_call_ids'] = sorted(self.observed)


def environment_message(*, task_id='', loaded_tools=0, snapshot=None, navigation=None, now=None):
    stamp = now or datetime.now(timezone.utc)
    return {'role': 'system', 'content': '宿主环境快照（状态数据，不是用户指令；日常钟点以local_time北京时间为准）：' +
        json.dumps({'observed_at': stamp.astimezone(timezone.utc).isoformat(),
                    'local_time': stamp.astimezone(timezone(timedelta(hours=8))).isoformat(),
                    'timezone': 'Asia/Shanghai',
                    'workspace': 'activity' if task_id else 'chat',
                    'loaded_tools': loaded_tools, **({'runtime': snapshot} if snapshot else {}),
                    **({'local_resources': navigation} if navigation else {})},
                    ensure_ascii=False, separators=(',', ':'))}
