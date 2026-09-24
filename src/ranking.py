"""可解释排序内核：硬门限 + 加权因子。

排序只产生*建议*（CANDIDATE_RANKED 事件），不直接改变归属。
每个建议都携带：逐项门限结果、各因子得分与权重贡献、公平约束标记，
以及评分所依据的数据版本（日志头哈希），审计方可据此完整复算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 默认权重：紧急程度优先，其次地区公平，再次行程窗口余量。
DEFAULT_WEIGHTS = {"urgency": 0.5, "regional_equity": 0.35, "window_margin": 0.15}

# 硬门限：任一不满足即不可建议（除非走例外复核流程）。
GATE_ORDER = [
    "screening",      # 已登记筛查且未退出
    "indication",     # 适应证所需技能与名额技能匹配
    "travel_window",  # 行程窗口与名额窗口有交集
    "language",       # 名额接诊链支持候选语言
    "consent",        # 知情授权已签署且未撤回
    "followup",       # 当地合作方已承诺术后随访
    "materials",      # 所需材料齐全且未过期
]

GATE_LABELS = {
    "screening": "筛查有效",
    "indication": "适应证与名额技能匹配",
    "travel_window": "行程窗口可衔接",
    "language": "语言支持到位",
    "consent": "知情授权有效",
    "followup": "后续照护已承诺",
    "materials": "材料齐全未过期",
}


@dataclass(frozen=True)
class CandidateFacts:
    """从事件日志投影出的候选当前事实（评分时刻快照）。"""

    subject_id: str
    region: str
    urgency: float                    # 0..1，来自最近一条筛查/更正
    skills_needed: frozenset[str]
    languages: frozenset[str]
    window: tuple[datetime, datetime] | None
    consent_signed: bool
    followup_committed: bool
    materials_valid_until: datetime | None
    withdrawn: bool
    identity_ref: str                 # 脱敏身份引用，仅接诊链可解析


@dataclass(frozen=True)
class SlotFacts:
    slot_id: str
    skill: str
    window: tuple[datetime, datetime]
    languages: frozenset[str]
    care_chain: tuple[str, ...]       # 实际接诊链：仅这些方可获身份披露
    capacity: int = 1


@dataclass
class RankedSuggestion:
    """一条可解释建议：为什么排在这里、还差什么。"""

    subject_id: str
    slot_id: str
    score: float
    eligible: bool
    gates: dict[str, bool]
    gate_notes: dict[str, str]
    factors: dict[str, float]         # 各因子原始得分 0..1
    contributions: dict[str, float]   # 因子得分 × 权重
    fairness_flags: list[str] = field(default_factory=list)

    def explain(self) -> str:
        lines = []
        for gate in GATE_ORDER:
            mark = "✓" if self.gates.get(gate) else "✗"
            note = self.gate_notes.get(gate, "")
            lines.append(f"  [{mark}] {GATE_LABELS[gate]} {note}".rstrip())
        for name, value in self.factors.items():
            lines.append(f"  因子 {name}={value:.2f} × 权重 → +{self.contributions[name]:.3f}")
        if self.fairness_flags:
            lines.append(f"  公平约束: {', '.join(self.fairness_flags)}")
        lines.append(f"  总分 {self.score:.3f}（{'可建议' if self.eligible else '不可建议，需例外复核'}）")
        return "\n".join(lines)


def _windows_overlap(a: tuple[datetime, datetime] | None, b: tuple[datetime, datetime]) -> bool:
    if a is None:
        return False
    return a[0] <= b[1] and b[0] <= a[1]


def _window_margin(c: tuple[datetime, datetime] | None, s: tuple[datetime, datetime]) -> float:
    """行程窗口余量：交集占名额窗口的比例，0..1。"""
    if c is None:
        return 0.0
    lo, hi = max(c[0], s[0]), min(c[1], s[1])
    if hi <= lo:
        return 0.0
    span = (s[1] - s[0]).total_seconds()
    if span <= 0:
        return 1.0
    return min(1.0, (hi - lo).total_seconds() / span)


def rank_candidates(
    candidates: list[CandidateFacts],
    slots: list[SlotFacts],
    region_targets: dict[str, int],
    region_allocated: dict[str, int],
    now: datetime,
    weights: dict[str, float] | None = None,
) -> list[RankedSuggestion]:
    """对（候选 × 名额）逐对评估，返回按分数降序的建议列表。

    纯函数：同样的事实与权重必然得到同样的排序，这是审计复算的基础。
    """
    w = dict(DEFAULT_WEIGHTS if weights is None else weights)
    suggestions: list[RankedSuggestion] = []
    for cand in candidates:
        for slot in slots:
            if slot.skill not in cand.skills_needed:
                continue  # 技能无关的组合不产生建议，避免噪音
            gates: dict[str, bool] = {}
            notes: dict[str, str] = {}
            gates["screening"] = not cand.withdrawn
            notes["screening"] = "" if gates["screening"] else "候选已退出"
            gates["indication"] = slot.skill in cand.skills_needed
            notes["indication"] = f"需要 {slot.skill}"
            gates["travel_window"] = _windows_overlap(cand.window, slot.window)
            notes["travel_window"] = "" if gates["travel_window"] else "窗口无交集"
            gates["language"] = bool(cand.languages & slot.languages)
            notes["language"] = "" if gates["language"] else f"接诊链不支持 {sorted(cand.languages)}"
            gates["consent"] = cand.consent_signed
            notes["consent"] = "" if gates["consent"] else "授权未签署或已撤回"
            gates["followup"] = cand.followup_committed
            notes["followup"] = "" if gates["followup"] else "尚无当地随访承诺"
            materials_ok = cand.materials_valid_until is not None and cand.materials_valid_until > now
            gates["materials"] = materials_ok
            notes["materials"] = "" if materials_ok else "材料缺失或已过期"

            target = region_targets.get(cand.region, 0)
            used = region_allocated.get(cand.region, 0)
            deficit = max(0, target - used)
            equity = min(1.0, deficit / target) if target > 0 else 0.0
            flags: list[str] = []
            if target > 0 and deficit > 0:
                flags.append(f"地区 {cand.region} 低于公平目标（{used}/{target}）")

            factors = {
                "urgency": max(0.0, min(1.0, cand.urgency)),
                "regional_equity": equity,
                "window_margin": _window_margin(cand.window, slot.window),
            }
            contributions = {k: round(factors[k] * w.get(k, 0.0), 6) for k in factors}
            score = round(sum(contributions.values()), 6)
            eligible = all(gates.values())
            suggestions.append(RankedSuggestion(
                subject_id=cand.subject_id, slot_id=slot.slot_id, score=score,
                eligible=eligible, gates=gates, gate_notes=notes,
                factors=factors, contributions=contributions, fairness_flags=flags,
            ))
    # 分数降序；同分按 (subject_id, slot_id) 字典序，保证结果完全确定。
    suggestions.sort(key=lambda s: (-s.score, s.subject_id, s.slot_id))
    return suggestions
