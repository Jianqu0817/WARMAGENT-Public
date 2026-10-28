"""Terminal, non-message decision for optional group interjections."""
from dataclasses import dataclass


NO_REPLY_NAME = 'group_no_reply'
NO_REPLY_SCHEMA = {'type': 'function', 'function': {
    'name': NO_REPLY_NAME,
    'description': '本轮不发送任何聊天消息；正常结束且不再调用模型。仅用于可选群聊插话或自主开口，须单独调用。',
    'parameters': {'type': 'object', 'properties': {
        'reason': {'type': 'string', 'enum': ['not_addressed', 'no_new_value', 'conversation_closed']}},
        'required': ['reason'], 'additionalProperties': False}}}
NO_REPLY_RULES = (
    '\n本轮是可选的群聊接话机会，不是必须回答。先看当前话是否在接你的话；'
    '同一发言人也可能已经换话题。对方在叫别人、只是表情/附和、话题结束，'
    '或你只能复述/猜测/无信息地搭话时，单独调用group_no_reply结束，正常旁听。'
    '不要发送“我不说话”“你们聊”等解释沉默的文字，不为使用机会硬找话说。'
)


@dataclass(frozen=True)
class NoReply:
    reason: str
