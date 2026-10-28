"""Bridge-local emotion expression over the frozen core's persisted PAD state."""
from datetime import datetime
import json

from myatri._vendor.emotional_memory import pad_distance
from myatri.contracts.emotion import PAD
from myatri.persona.emotion import EmotionState


# Same expression window as standalone myatry; repeated expression is prompt-guided.
MEANING_FRESH_SECONDS = 2700


class BridgeEmotionState(EmotionState):
    def context_block(self, state, *, max_bytes=1400):
        if not state["enabled"] or not state["appraisal_refs"]:
            return ""
        payload = {"affect": state["affect"], "mood": state["mood"],
                   "appraisal_refs": state["appraisal_refs"][-2:], "as_of": state["evaluated_at"],
                   "open_concern_refs": state["open_concern_refs"][-2:]}
        # A historical appraisal label is not an eternal current emotion.
        distance = pad_distance(PAD.model_validate(state["affect"]).vector(), self.config.baseline.vector())
        if distance >= 0.05 and state["last_appraisal"]:
            # Only the meaning expires; PAD data remains. Avoiding repeated
            # expression within this window is guided by the prompt below.
            observed = state.get("last_observed_at")
            try:
                age = None if not observed else (
                    datetime.fromisoformat(state["evaluated_at"])
                    - datetime.fromisoformat(observed)).total_seconds()
            except ValueError:
                age = None
            if age is not None and age <= MEANING_FRESH_SECONDS:
                payload["last_event_meaning"] = state["last_appraisal"]["meaning"][:120]
        prefix = ("\n情绪连续性（暂定数据，可调整；不改变事实、人格或工具权限，不要求生气或讨好）：\n"
                  "可在语气、用词和节奏上自然体现当前情绪与心境，不直接报数值，不刻意表演情绪。\n"
                  "last_event_meaning 是最近一次心情变化的含义：只在第一次开口时可以自然带出一点，"
                  "同一次变化不要在后续轮次反复提起。\n")
        rendered = json.dumps(payload, ensure_ascii=False)
        if len((prefix + rendered).encode("utf-8")) > max_bytes:
            payload.pop("last_event_meaning", None)
            payload["open_concern_refs"] = []
            rendered = json.dumps(payload, ensure_ascii=False)
        if len((prefix + rendered).encode("utf-8")) > max_bytes:
            return ""
        return prefix + rendered

