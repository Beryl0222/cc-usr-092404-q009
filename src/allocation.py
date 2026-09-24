"""可解释的候选排序与名额生命周期规则。

本模块是纯函数参考实现，配合 medical_mission_allocation 中的事件合同使用：

- 评估只产生建议（CANDIDATE_EVALUATED），正式归属必须经过确认事件；
- 同样的输入永远得到同样的建议，审计人员可用事件里记录的
  policy_version 与 data_version 复算历史结论；
- 副作用（过期释放、候补升级、交接通知）全部由事件日志派生，
  服务重启后重放同一日志不会重复产生。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------- 政策

DEFAULT_POLICY = {
    "policy_version": "policy-2026.09",
    # 硬性门槛：任一不满足即失去候选资格，
    # 避免签证、术后随访、当地接诊能力等问题到最后才暴露。
    "gates": ("CONSENT", "INDICATION", "SKILL", "TRAVEL_WINDOW", "FOLLOW_UP", "SCREENING_FIT"),
    # 排序因子权重，仅对通过全部门槛的候选计算名次。
    "weights": {"urgency": 0.40, "equity": 0.25, "screening_need": 0.20, "language": 0.15},
    # 地区公平目标：每个地区应占的最低名额比例。
    "equity_targets": {"remote-west": 0.30, "central": 0.50},
    # 名额暂占期限（小时），超时自动释放并按原规则递补。
    "hold_ttl_hours": 72,
}

POLICIES = {DEFAULT_POLICY["policy_version"]: DEFAULT_POLICY}

RELEASE_REASONS = ("EXPIRED", "WITHDRAWN", "MATERIALS_STALE", "SUPERSEDED")


def policy_by_version(version: str) -> dict | None:
    """按版本号取回历史政策，供审计复算。"""
    return POLICIES.get(version)


def data_version(*parts) -> str:
    """把参与一次评估的输入折叠成稳定摘要。"""
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


# --------------------------------------------------------------------- 可解释评估

def evaluate_candidate(candidate: dict, slot: dict, policy: dict | None = None) -> dict:
    """产出一份可解释的评估建议；结果只是建议，不直接形成归属。"""
    policy = policy or DEFAULT_POLICY
    gate_failures = _gate_failures(candidate, slot)
    factors = {
        "urgency": _factor(candidate.get("urgency", 0) / 5, f"紧急程度 {candidate.get('urgency', 0)}/5"),
        "equity": _equity_factor(candidate, slot, policy),
        "screening_need": _factor(candidate.get("risk_score", 0.0), "筛查风险越高越优先"),
        "language": _language_factor(candidate, slot),
    }
    weights = policy["weights"]
    total = round(sum(f["score"] * weights[name] for name, f in factors.items()), 6)
    return {
        "subject_id": candidate.get("subject_id"),
        "slot_id": slot.get("slot_id"),
        "eligible": not gate_failures,
        "gate_failures": gate_failures,
        "factors": factors,
        "total": total,
        "policy_version": policy["policy_version"],
        "data_version": data_version(candidate, slot, policy),
    }


def rank_candidates(candidates, slot, policy: dict | None = None) -> dict:
    """对同一名额评估一批候选，返回建议名次与被门槛拦下的候选。"""
    policy = policy or DEFAULT_POLICY
    evaluations = [evaluate_candidate(c, slot, policy) for c in candidates]
    eligible = [e for e in evaluations if e["eligible"]]
    eligible.sort(key=lambda e: (-e["total"], str(e["subject_id"])))
    for rank, evaluation in enumerate(eligible, start=1):
        evaluation["suggested_rank"] = rank
    return {
        "slot_id": slot.get("slot_id"),
        "policy_version": policy["policy_version"],
        "ranking": eligible,
        "blocked": [e for e in evaluations if not e["eligible"]],
    }


def explain(evaluation: dict) -> str:
    """把评估结果转成可读说明，供协调员与审计核对。"""
    subject = evaluation.get("subject_id")
    if not evaluation.get("eligible"):
        lines = [f"候选 {subject} 未通过门槛："]
        lines += [f"  - {g['gate']}：{g['reason']}" for g in evaluation["gate_failures"]]
        return "\n".join(lines)
    lines = [
        f"候选 {subject} 建议分 {evaluation['total']:.3f}"
        f"（政策 {evaluation['policy_version']}，数据版本 {evaluation['data_version']}）："
    ]
    for name, factor in evaluation["factors"].items():
        lines.append(f"  - {name}={factor['score']:.2f}，{factor['reason']}")
    return "\n".join(lines)


def evaluation_event(candidate, slot, policy: dict | None = None, *,
                     event_id, occurred_at, suggested_rank=None) -> dict:
    """把评估结论包装成可交换的 CANDIDATE_EVALUATED 事件（含复算所需输入）。"""
    policy = policy or DEFAULT_POLICY
    evaluation = evaluate_candidate(candidate, slot, policy)
    if suggested_rank is not None:
        evaluation["suggested_rank"] = suggested_rank
    payload = {
        "slot_id": slot.get("slot_id"),
        "policy_version": policy["policy_version"],
        "data_version": evaluation["data_version"],
        "eligible": evaluation["eligible"],
        "gate_failures": evaluation["gate_failures"],
        "factors": evaluation["factors"],
        "total": evaluation["total"],
        "suggested_rank": evaluation.get("suggested_rank"),
        "inputs": {"candidate": candidate, "slot": slot},
    }
    return {
        "event_id": event_id,
        "kind": "CANDIDATE_EVALUATED",
        "occurred_at": occurred_at,
        "subject_id": candidate.get("subject_id"),
        "payload": payload,
    }


def _gate_failures(candidate, slot) -> list[dict]:
    failures = []
    if not candidate.get("consent_signed"):
        failures.append({"gate": "CONSENT", "reason": "知情授权未签署"})
    if candidate.get("indication") not in slot.get("indications", ()):
        failures.append({"gate": "INDICATION", "reason": "适应证不在本批名额接诊范围"})
    if candidate.get("required_skill") not in slot.get("skills", ()):
        failures.append({"gate": "SKILL", "reason": "名额不具备所需技能与当地接诊能力"})
    if not _windows_overlap(candidate.get("travel_windows", ()), slot.get("mission_window")):
        failures.append({"gate": "TRAVEL_WINDOW", "reason": "签证与行程窗口和任务窗口不相交"})
    if not candidate.get("follow_up_committed"):
        failures.append({"gate": "FOLLOW_UP", "reason": "术后随访与当地后续照护承诺未落实"})
    if candidate.get("screening_status") == "UNFIT":
        failures.append({"gate": "SCREENING_FIT", "reason": "筛查结论为不适宜出行或手术"})
    return failures


def _factor(score, reason, **extra) -> dict:
    return {"score": round(max(0.0, min(1.0, float(score))), 6), "reason": reason, **extra}


def _equity_factor(candidate, slot, policy) -> dict:
    region = candidate.get("region")
    target = policy.get("equity_targets", {}).get(region)
    current = slot.get("region_shares", {}).get(region, 0.0)
    if not target:
        return _factor(0.0, f"地区 {region} 未设定公平目标",
                       region=region, target=target, current_share=current)
    deficit = max(0.0, target - current)
    return _factor(
        min(1.0, deficit / target),
        f"地区 {region} 目标 {target:.0%}，当前 {current:.0%}，缺口 {deficit:.0%}",
        region=region, target=target, current_share=current, deficit=round(deficit, 6),
    )


def _language_factor(candidate, slot) -> dict:
    language = candidate.get("language")
    if language in slot.get("languages", ()):
        return _factor(1.0, "接诊链直接支持该语言", language=language)
    if slot.get("interpreter_available"):
        return _factor(0.5, "需依赖现场翻译支持", language=language)
    return _factor(0.0, "名额暂无该语言支持", language=language)


def _windows_overlap(windows, mission_window) -> bool:
    if not mission_window:
        return False
    start, end = _day(mission_window[0]), _day(mission_window[1])
    return any(_day(w[0]) <= end and _day(w[1]) >= start for w in windows)


def _day(value) -> date:
    return date.fromisoformat(str(value)[:10])


# --------------------------------------------------------------------- 暂占期限

def hold_expires_at(placed_at: str, policy: dict | None = None) -> str:
    """暂占截止时间：放置时间 + 政策规定的暂占期限。"""
    policy = policy or DEFAULT_POLICY
    moment = datetime.fromisoformat(placed_at) + timedelta(hours=policy["hold_ttl_hours"])
    return moment.isoformat()


# --------------------------------------------------------------------- 事件重放

def new_state() -> dict:
    """重放事件日志得到的分配状态。"""
    return {
        "holds": {},              # hold_id -> 暂占记录
        "confirmations": {},      # hold_id -> 确认记录（并发确认只保留第一个归属）
        "slot_owners": {},        # slot_id -> 已确认的 hold_id
        "confirmed_subjects": set(),
        "evaluations": {},        # (slot_id, subject_id) -> 评估事件 payload
        "departed": set(),        # 已出发的 hold_id，不再参与重算
        "completed": set(),       # 已完成诊疗的 hold_id，事实保持不变
        "disclosures": {},        # subject_id -> 已披露的合作方集合
        "notifications": [],      # 派生的副作用通知，重放结果一致
        "seen_keys": set(),       # 已处理的幂等键
        "problems": [],           # 重放中发现的不变量违反
    }


def replay(events, state: dict | None = None) -> dict:
    """按日志顺序折叠事件；带相同 idempotency_key 的事件只生效一次。"""
    state = state or new_state()
    for event in events:
        _expire_holds(state, event.get("occurred_at"))
        key = event.get("payload", {}).get("idempotency_key")
        if key and key in state["seen_keys"]:
            continue
        if key:
            state["seen_keys"].add(key)
        _apply(state, event)
    return state


def recompute_scope(state) -> dict:
    """筛查更正或航班变化只重算未出发的安排；已出发与已完成的诊疗事实冻结。"""
    recompute, frozen = [], []
    for hold_id, hold in state["holds"].items():
        if hold_id in state["departed"] or hold_id in state["completed"]:
            frozen.append(hold_id)
        elif hold["status"] in ("ACTIVE", "CONFIRMED"):
            recompute.append(hold_id)
    return {"recompute": sorted(recompute), "frozen": sorted(frozen)}


def care_chain(state, subject_id) -> set:
    """实际接诊链：持有该患者有效或已确认暂占的合作方。"""
    return {
        hold["partner_id"]
        for hold in state["holds"].values()
        if hold["subject_id"] == subject_id and hold["status"] in ("ACTIVE", "CONFIRMED")
    }


def expected_promotion(state, slot_id, exclude=None) -> dict | None:
    """按原规则（评估事件记录的建议名次）找出应递补的候选。"""
    candidates = []
    for (slot, subject), payload in state["evaluations"].items():
        if slot != slot_id or not payload.get("eligible") or subject == exclude:
            continue
        if subject in state["confirmed_subjects"]:
            continue
        if any(h["subject_id"] == subject and h["status"] == "ACTIVE"
               for h in state["holds"].values()):
            continue
        candidates.append((payload.get("suggested_rank") or 1 << 30, str(subject), subject, payload))
    if not candidates:
        return None
    candidates.sort()
    _, _, subject, payload = candidates[0]
    return {
        "subject_id": subject,
        "policy_version": payload.get("policy_version"),
        "suggested_rank": payload.get("suggested_rank"),
    }


def _apply(state, event) -> None:
    handler = _HANDLERS.get(event.get("kind"))
    if handler:
        handler(state, event)


def _on_evaluation(state, event) -> None:
    payload = event["payload"]
    state["evaluations"][(payload["slot_id"], event["subject_id"])] = payload


def _on_hold_placed(state, event) -> None:
    payload = event["payload"]
    _place_hold(state, event, payload["hold_id"], payload["slot_id"],
                payload["partner_id"], payload["expires_at"])


def _place_hold(state, event, hold_id, slot_id, partner_id, expires_at) -> None:
    subject = event["subject_id"]
    for other in state["holds"].values():
        if (other["subject_id"] == subject and other["status"] == "ACTIVE"
                and other["partner_id"] != partner_id):
            _problem(state, event, "DUPLICATE_HOLD",
                     f"{subject} 已在合作方 {other['partner_id']} 持有有效暂占")
            return
    state["holds"][hold_id] = {
        "subject_id": subject,
        "slot_id": slot_id,
        "partner_id": partner_id,
        "expires_at": expires_at,
        "status": "ACTIVE",
    }


def _on_hold_released(state, event) -> None:
    payload = event["payload"]
    hold = state["holds"].get(payload["hold_id"])
    if payload.get("reason") not in RELEASE_REASONS:
        _problem(state, event, "UNKNOWN_RELEASE_REASON", str(payload.get("reason")))
        return
    if not hold or hold["status"] != "ACTIVE":
        _problem(state, event, "RELEASE_NOT_ACTIVE", payload["hold_id"])
        return
    _release(state, hold, payload["reason"])
    _notify(state, event, "HOLD_RELEASED",
            {"hold_id": payload["hold_id"], "reason": payload["reason"]})


def _release(state, hold, reason) -> None:
    hold["status"] = "RELEASED"
    hold["release_reason"] = reason


def _on_promotion(state, event) -> None:
    payload = event["payload"]
    released = state["holds"].get(payload["released_hold_id"])
    if not released or released["status"] != "RELEASED":
        _problem(state, event, "PROMOTION_WITHOUT_RELEASE", payload["released_hold_id"])
        return
    expected = expected_promotion(state, released["slot_id"], exclude=released["subject_id"])
    if expected is None:
        _problem(state, event, "PROMOTION_NO_CANDIDATE", released["slot_id"])
        return
    if event["subject_id"] != expected["subject_id"]:
        _problem(state, event, "PROMOTION_ORDER",
                 f"应按原规则递补 {expected['subject_id']}，实际 {event['subject_id']}")
        return
    if payload.get("rule_version") != expected["policy_version"]:
        _problem(state, event, "PROMOTION_RULE",
                 f"递补规则 {payload.get('rule_version')} 与原评估 {expected['policy_version']} 不一致")
        return
    _place_hold(state, event, payload["hold_id"], released["slot_id"],
                released["partner_id"], payload["expires_at"])
    _notify(state, event, "WAITLIST_PROMOTED",
            {"hold_id": payload["hold_id"], "released_hold_id": payload["released_hold_id"]})


def _on_confirm(state, event) -> None:
    payload = event["payload"]
    hold = state["holds"].get(payload["hold_id"])
    if not hold or hold["status"] != "ACTIVE":
        _problem(state, event, "CONFIRM_WITHOUT_ACTIVE_HOLD", payload["hold_id"])
        return
    owner = state["slot_owners"].get(hold["slot_id"])
    if owner is not None:
        _problem(state, event, "SLOT_ALREADY_ALLOCATED",
                 f"名额 {hold['slot_id']} 已由 {owner} 归属")
        return
    state["confirmations"][payload["hold_id"]] = {
        "confirmed_by": payload["confirmed_by"],
        "fence_token": payload["fence_token"],
        "policy_version": payload.get("policy_version"),
        "data_version": payload.get("data_version"),
    }
    state["slot_owners"][hold["slot_id"]] = payload["hold_id"]
    state["confirmed_subjects"].add(hold["subject_id"])
    hold["status"] = "CONFIRMED"
    _notify(state, event, "ALLOCATION_CONFIRMED",
            {"hold_id": payload["hold_id"], "slot_id": hold["slot_id"]})


def _on_exception_approved(state, event) -> None:
    payload = event["payload"]
    if not payload.get("approved_by") or payload.get("approved_by") == payload.get("requested_by"):
        _problem(state, event, "EXCEPTION_NEEDS_INDEPENDENT_REVIEW", "批准人必须与申请人不同")
    if not payload.get("impact"):
        _problem(state, event, "EXCEPTION_IMPACT_MISSING", "例外影响未记录")


def _on_departure(state, event) -> None:
    state["departed"].add(event["payload"]["hold_id"])


def _on_completed(state, event) -> None:
    state["completed"].add(event["payload"]["hold_id"])


def _on_disclosure(state, event) -> None:
    payload = event["payload"]
    chain = care_chain(state, event["subject_id"])
    outsiders = [d for d in payload["disclosed_to"] if d not in chain]
    if outsiders:
        _problem(state, event, "DISCLOSURE_OUTSIDE_CARE_CHAIN", ",".join(outsiders))
        return
    state["disclosures"].setdefault(event["subject_id"], set()).update(payload["disclosed_to"])


def _on_handoff(state, event) -> None:
    payload = event["payload"]
    _notify(state, event, "OUTCOME_HANDED_OFF",
            {"hold_id": payload["hold_id"], "handed_to": payload["handed_to"]})


def _expire_holds(state, now) -> None:
    if not now:
        return
    moment = datetime.fromisoformat(str(now))
    for hold_id in sorted(state["holds"]):
        hold = state["holds"][hold_id]
        if hold["status"] == "ACTIVE" and datetime.fromisoformat(str(hold["expires_at"])) <= moment:
            _release(state, hold, "EXPIRED")
            state["notifications"].append({
                "type": "HOLD_EXPIRED",
                "hold_id": hold_id,
                "idempotency_key": f"expire:{hold_id}",
            })


def _notify(state, event, kind, extra) -> None:
    # 事件未显式携带幂等键时回退到 event_id，保证派生通知可去重。
    key = event.get("payload", {}).get("idempotency_key") or event.get("event_id")
    state["notifications"].append({"type": kind, "idempotency_key": key, **extra})


def _problem(state, event, code, detail) -> None:
    state["problems"].append({"code": code, "event_id": event.get("event_id"), "detail": detail})


_HANDLERS = {
    "CANDIDATE_EVALUATED": _on_evaluation,
    "SLOT_HOLD_PLACED": _on_hold_placed,
    "SLOT_HOLD_RELEASED": _on_hold_released,
    "WAITLIST_PROMOTED": _on_promotion,
    "ALLOCATION_CONFIRMED": _on_confirm,
    "EXCEPTION_APPROVED": _on_exception_approved,
    "DEPARTURE_RECORDED": _on_departure,
    "TREATMENT_COMPLETED": _on_completed,
    "IDENTITY_DISCLOSED": _on_disclosure,
    "OUTCOME_HANDED_OFF": _on_handoff,
}
