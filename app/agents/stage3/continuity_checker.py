"""Agent 3.6 场记/连续性检查（app/agents/stage3/continuity_checker.py）。

方案B：检查相邻镜头的穿帮/一致性风险，输出风险列表 + 严重度，仅检查不改动。

输入：state.storyboard[]（shot_id/scene_id/description/camera/shots 等）
     + state.script.scenes[]（可提供角色/道具上下文）
处理：
  - 规则引擎（始终执行）：
      规则1 相邻镜头角色是否合理出现/离开（characters_in_shot 匹配）
      规则2 相邻镜头同一角色状态描述差异过大 -> 可能穿帮
      规则3 相邻镜头场景/灯光描述不一致 -> 可能穿帮
  - LLM 深度分析（可选，settings.mock_critics=False 且提供描述时）：
      对比相邻镜头描述文本，补充规则外的语义风险
输出：state.continuity_report —— list[{risk, shots, severity, suggestion, rule}]
"""

import json
from typing import Dict, List, Optional

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState

# 严重度：high=🔴 / medium=⚠️ / low=🟢
_LEVEL = {"high": "🔴 高", "medium": "⚠️ 中", "low": "🟢 低"}


def _extract_items(text: str) -> set:
    """从描述文本提取关键名词（人物/道具/状态词），用常见中文词分隔简单切分。"""
    import re

    text = text or ""
    # 抓取名词性片段（连续汉字）作为候选
    segs = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    return set(segs)


class ContinuityChecker(BaseAgent):
    name = "continuity_checker"

    def run(self, state: CrewState) -> dict:
        """场记
        inputs:
            storyboard: list[dict]   分镜列表
            script: dict             分场剧本（可选上下文）
        outputs:
            continuity_report: list[dict]  穿帮/风险列表
        """
        storyboard = list(state.get("storyboard") or [])
        if len(storyboard) < 2:
            return {"continuity_report": [], "current_phase": self.name}

        risks: List[dict] = []
        # 预提取每镜的关键信息 + 基于 scene_design_profiles 的角色分布
        # 场景设计师已为每个 scene 标注 characters_in_scene，用作真实角色名来源
        scene_char_map = self._build_scene_char_map(state)
        shots = [self._extract_shot(s, scene_char_map) for s in storyboard]

        # 规则引擎：逐对相邻镜头
        for i in range(len(shots) - 1):
            a, b = shots[i], shots[i + 1]
            self._rule1(a, b, risks)
            self._rule2(a, b, risks)
            self._rule3(a, b, risks)

        # LLM 深度分析（可选）：描述文本对比，补充语义风险
        if not settings.mock_critics and risks is not None:
            llm_extra = self._llm_deep_check(shots)
            risks.extend(llm_extra)

        # 按严重度排序：high > medium > low
        order = {"high": 0, "medium": 1, "low": 2}
        risks.sort(key=lambda r: order.get(r.get("severity", "low"), 2))

        return {"continuity_report": risks, "current_phase": self.name}

    # ---------------------------------------------------------------
    # 镜头信息抽取
    # ---------------------------------------------------------------
    def _build_scene_char_map(self, state: CrewState) -> Dict[str, list]:
        """从 scene_design_profiles 建立 scene_id -> [characters_in_scene] 的映射。"""
        profiles = state.get("scene_design_profiles") or {}
        out: Dict[str, list] = {}
        for sid, prof in profiles.items():
            if isinstance(prof, dict) and prof.get("characters_in_scene"):
                out[str(sid)] = [str(c) for c in prof["characters_in_scene"]]
        return out

    def _extract_shot(self, s: dict, scene_char_map: Dict[str, list]) -> dict:
        sid = str(s.get("shot_id") or "").strip()
        desc = str(s.get("description") or "")
        camera = str(s.get("camera") or "")
        scene = str(s.get("scene_id") or "").strip()
        # 该镜头所属场景的真实角色名（来自场景设计师）
        return {"shot_id": sid, "scene_id": scene, "description": desc,
                "camera": camera, "characters": scene_char_map.get(scene, [])}

    # ---------------------------------------------------------------
    # 规则引擎
    # ---------------------------------------------------------------
    def _rule1(self, a: dict, b: dict, risks: List[dict]) -> None:
        """规则1：相邻镜头角色是否合理出现/离开（基于真实角色名）。

        用场景设计师标注的 characters_in_scene 判定：镜头 A 所在场景有角色 X，
        镜头 B 所在场景没有角色 X（且描述里也未出现 X）-> 标记为「角色离开/入画」风险。
        """
        a_chars = set(a.get("characters") or [])
        b_chars = set(b.get("characters") or [])
        # 描述里也搜一遍角色名（双保险，防止场景映射缺失）
        for name in list(a_chars | b_chars):
            in_a_desc = name in a["description"]
            in_b_desc = name in b["description"]
            a_has = (name in a_chars) or in_a_desc
            b_has = (name in b_chars) or in_b_desc
            # 仅当相邻镜头出现状态变化时标记
            if a_has and not b_has:
                risks.append({
                    "rule": "1",
                    "risk": f"角色「{name}」在镜头 {a['shot_id']} 出现，镜头 {b['shot_id']} 未出现，请确认是否为合理的离开/出画。",
                    "shots": [a["shot_id"], b["shot_id"]],
                    "severity": "low",
                    "suggestion": "若角色应继续在场，请补充该角色在之后的镜头；否则确认为有意出画/转场。",
                })
            elif b_has and not a_has:
                risks.append({
                    "rule": "1",
                    "risk": f"角色「{name}」在镜头 {b['shot_id']} 新出现，但镜头 {a['shot_id']} 未出现，请确认入画是否合理。",
                    "shots": [a["shot_id"], b["shot_id"]],
                    "severity": "low",
                    "suggestion": "确认为新角色入场，或补充前导镜头交代其来源。",
                })

    def _rule2(self, a: dict, b: dict, risks: List[dict]) -> None:
        """规则2：相邻镜头**同一角色**的状态描述差异过大 -> 潜在穿帮。

        只对两个镜头都出现的真实角色，比较其描述中与角色相关联的状态关键词
        （服装/伤势/发型/持械等）。避免跨角色/跨元素的状态误报。
        """
        a_chars = set(a.get("characters") or [])
        b_chars = set(b.get("characters") or [])
        both_present = a_chars & b_chars
        # 若场景映射缺失，则用描述里共同出现的关键实体兜底
        if not both_present:
            both_present = _extract_items(a["description"]) & _extract_items(b["description"])
            both_present = {x for x in both_present if len(x) >= 2}

        state_keywords = ["受伤", "血迹", "完好", "破损", "湿", "干", "束发", "披发",
                          "持剑", "空手", "戴刀", "戴笠", "斗笠", "面具", "蒙面", "持枪", "佩剑"]

        for entity in list(both_present)[:3]:
            entity = str(entity)
            # 取该角色/实体在各自描述里的状态关键词（不分段，仅在本镜描述里找）
            a_kw = [k for k in state_keywords if k in a["description"]]
            b_kw = [k for k in state_keywords if k in b["description"]]
            if a_kw and b_kw and a_kw != b_kw:
                risks.append({
                    "rule": "2",
                    "risk": f"角色/元素「{entity}」在镜头 {a['shot_id']} 与 {b['shot_id']} 的状态描述不一致"
                            f"（{','.join(a_kw)} vs {','.join(b_kw)}），疑似穿帮。",
                    "shots": [a["shot_id"], b["shot_id"]],
                    "severity": "medium",
                    "suggestion": "统一相邻镜头中该角色的状态/服装/道具描述。",
                })
                break  # 每对相邻镜头只报一条状态不一致即可

    def _rule3(self, a: dict, b: dict, risks: List[dict]) -> None:
        """规则3：相邻镜头场景/灯光描述不一致 -> 潜在穿帮。"""
        scene_kw = ["白天", "夜晚", "雨", "晴", "室内", "室外", "黄昏", "清晨", "灯光", "暗", "亮"]
        a_kw = [k for k in scene_kw if k in a["description"]]
        b_kw = [k for k in scene_kw if k in b["description"]]
        if a_kw and b_kw and a_kw != b_kw:
            risks.append({
                "rule": "3",
                "risk": f"镜头 {a['shot_id']} 与 {b['shot_id']} 的场景/灯光描述不一致"
                        f"（{','.join(a_kw)} vs {','.join(b_kw)}），疑似穿帮。",
                "shots": [a["shot_id"], b["shot_id"]],
                "severity": "high",
                "suggestion": "统一相邻镜头的场景与灯光基调，或补充转场说明。",
            })

    # ---------------------------------------------------------------
    # LLM 深度分析（可选）
    # ---------------------------------------------------------------
    def _llm_deep_check(self, shots: List[dict]) -> List[dict]:
        """用 LLM 对比相邻镜头描述，补充语义级风险（规则之外的）。"""
        try:
            from app.agents._utils import call_deepseek, parse_json

            # 只取前 N 对镜头做深度分析，避免过长
            pairs = []
            for i in range(min(len(shots) - 1, 6)):
                pairs.append({
                    "a": shots[i]["shot_id"], "a_desc": shots[i]["description"],
                    "b": shots[i + 1]["shot_id"], "b_desc": shots[i + 1]["description"],
                })
            system = (
                "你是漫剧场记。对比每对相邻镜头描述，找出穿帮/一致性风险。只输出 JSON 数组：\n"
                '[{"risk": str, "shots": ["S1-01","S1-02"], "severity": "high|medium|low", "suggestion": str}]\n'
                "若某对无风险则不输出该项。只输出 JSON。"
            )
            data = parse_json(call_deepseek(system, json.dumps(pairs, ensure_ascii=False), role="continuity"))
            if isinstance(data, dict):
                data = data.get("risks") or data.get("items") or []
            if not isinstance(data, list):
                return []
            out = []
            for item in data:
                if isinstance(item, dict) and item.get("risk"):
                    out.append({
                        "rule": "llm",
                        "risk": str(item.get("risk", "")),
                        "shots": list(item.get("shots") or []),
                        "severity": str(item.get("severity", "medium")),
                        "suggestion": str(item.get("suggestion", "")),
                    })
            return out
        except Exception as exc:  # noqa: BLE001
            print(f"[continuity_checker] LLM 深度分析失败，跳过：{exc}")
            return []
