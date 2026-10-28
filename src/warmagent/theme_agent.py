"""Positive themes may omit diagnostic reason; evidence requirements stay intact."""
from copy import deepcopy

from myatri.cognition.theme import THEME_SCHEMA as CORE_THEME_SCHEMA, ThemeAgent as CoreThemeAgent
from myatri.cognition.tasks import cognitive_call, proposal_for
from myatri.contracts import Record


THEME_SCHEMA = deepcopy(CORE_THEME_SCHEMA)
THEME_SCHEMA["required"] = ["decision"]
THEME_SCHEMA["allOf"] = [{"if": {"properties": {"decision": {"enum": ["defer", "reject"]}}},
                          "then": {"required": ["reason"]}}]


class ThemeAgent(CoreThemeAgent):
    async def propose(self, theme_id):
        theme = self.index.themes[theme_id]
        material = self.index.theme_materials(theme_id)
        refs = list(dict.fromkeys([*theme["members"], *material.get("counter_refs", [])]))
        try:
            prior = self.store.get_record(theme_id)
        except KeyError:
            prior = None
        if prior and prior.payload.get("material_hash") == theme["material_hash"]:
            return None
        snapshot, sources, data, generator, run = await cognitive_call(
            self.store, self.gateway, "theme", refs,
            "A3：根据群落代表材料、差异与反例给暂定主题命名并摘要。小样本不泛化，不修改人格。"
            "连续性策略 continuity/v1：实体ID须沿用输入中已有稳定ID；叙事只标记有原文证据支持的延续、修正或终止；共同出现须逐条带来源标签，不从共现推断关系。仅依据本次展示材料，不批量重提炼历史。"
            "support_refs/counter_refs只能引用输入原文；说明异质部分与不确定处。", THEME_SCHEMA)
        if "reason" not in data:
            run = Record(**{**run.model_dump(exclude={"content_hash"}),
                            "payload": {**run.payload, "reason_omitted": True}})
        records = [run]
        if data["decision"] == "propose":
            if not data.get("label") or not data.get("summary") or not data.get("support_refs"):
                raise ValueError("incomplete theme proposal")
            if not set([*data["support_refs"], *data.get("counter_refs", [])]) <= {r.ref for r in sources}:
                raise ValueError("theme sources outside snapshot")
            records.append(Record(record_id=theme_id, revision=prior.revision+1 if prior else 1,
                                  supersedes_revision=prior.revision if prior else None,
                                  record_kind="theme", agent_id=self.store.agent_id, user_id=self.store.user_id,
                                  author_kind="model", generator=generator, source_refs=[*[r.ref for r in sources], run.ref],
                                  body_markdown=data["summary"], payload={**data, "strategy_version": "continuity/v1",
                                      "material_hash": theme["material_hash"],
                                      "name": data["label"], "cluster_refs": [theme["cluster_id"]], "status": "provisional",
                                      "graph_generation": self.index.generation, "membership_weights": theme["memberships"],
                                      "material_refs": [r.ref for r in sources],
                                      "material_limitations": run.payload["materials"]["limitations"]}))
        proposal = proposal_for(snapshot, sources, records, "A3", run)
        proposal.read_versions[theme_id] = prior.revision if prior else 0
        return proposal
