"""Small, stable chat guidance inspired by Humanizer-zh; no rewrite model."""

CHAT_STYLE_VERSION = 'humanizer-chat-v1'
CHAT_STYLE_RULES = """自然聊天表达/v1（沿用角色卡口吻，仅约束面向人的回复）：
先接眼前这句话；没增加信息的复述、夸奖、总结、客套省掉。闲聊不主动铺成分析或人生道理。
顺着意思说，能短就短，不凑排比、三段式或碎句。需要解释、步骤或报告时照常讲清。
可以有判断、犹豫和不同意见；不靠每轮反问、建议、语气词或撒娇维持亲近。亲密与玩笑看当前关系和对方回应。
这些是表达倾向，不是词语黑名单；合适的礼貌、比喻与列表可以保留。已自然的说法无需再改。
不为生动补造经历、记忆、动作或来源；保留否定、条件、归因、时间与完成状态，任务结果和阻塞原因必须说清。
只给对方要看的话，不展示自检或评分；代码、工具调用、气泡标记和结构化回执按原协议。"""


def apply_chat_style(messages, enabled):
    """Replace only our style block, preserving persona and tool pair ordering."""
    result = [m for m in messages if not (m.get('role') == 'system'
        and isinstance(m.get('content'), str) and m['content'].startswith('自然聊天表达/'))]
    if enabled:
        index = 1 if result and result[0].get('role') == 'system' else 0
        result.insert(index, {'role': 'system', 'content': CHAT_STYLE_RULES})
    return result
