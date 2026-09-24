"""分配服务：占有、递补、并发归属、例外复核、隐私披露与重算边界。

设计要点：
- 一切状态变化都是事件；服务状态只是事件日志的投影，可随时重放恢复。
- 自动排序只产生建议（CANDIDATE_RANKED）；占有与归属由协调员动作触发。
- 派生事件（到期释放、候补升级、交接通知）使用确定性事件 ID，
  服务重启后重放恢复不会重复产生。
- 已出发/已完成的安排是锁定事实：筛查更正与航班变化只重算未出发安排。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .domain import (
    DomainError,
    head_hash,
    link_event,
    parse_ts,
    validate_event,
    verify_chain,
)
from .ranking import (
    CandidateFacts,
    DEFAULT_WEIGHTS,
    SlotFacts,
    rank_candidates,
)

DEFAULT_HOLD_TTL_SECONDS = 72 * 3600
# 系统递补建立的占有由该名义合作方持有，任何实际合作方均可确认。
BACKFILL_PARTNER = "system-backfill"


class AllocationError(DomainError):
    """违反分配规则（重复占位、归属冲突、越权披露等）。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AllocationService:
    """国际义诊候选分配的领域服务（内存投影 + 追加式事件日志）。"""

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now_fn = now or _utcnow
        self._lock = threading.RLock()
        self.log: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._reset_projection()

    # ------------------------------------------------------------------ #
    # 投影状态
    # ------------------------------------------------------------------ #

    def _reset_projection(self) -> None:
        self._screenings: dict[str, dict[str, Any]] = {}
        self._indications: dict[str, set[str]] = {}
        self._consents: dict[str, bool] = {}
        self._followups: dict[str, bool] = {}
        self._materials_until: dict[str, datetime | None] = {}
        self._withdrawn: set[str] = set()
        self._aliases: dict[str, str] = {}               # "partner:alias" -> subject_id
        self._slots: dict[str, SlotFacts] = {}
        self._targets: dict[str, int] = {}
        self._holds: dict[str, dict[str, Any]] = {}      # hold_id -> 占有记录
        self._allocations: dict[str, dict[str, Any]] = {}  # slot_id -> 归属记录
        self._departed: set[str] = set()
        self._completed: set[str] = set()
        self._exceptions: dict[str, dict[str, Any]] = {}
        self._confirm_keys: dict[str, str] = {}          # 幂等键 -> 归属事件 ID
        self._notified: set[str] = set()                 # 已发送的交接通知键

    # ------------------------------------------------------------------ #
    # 事件写入
    # ------------------------------------------------------------------ #

    def append(
        self,
        kind: str,
        subject_id: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        occurred_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """追加一条领域事件并更新投影。event_id 缺省时按日志位置确定。"""
        with self._lock:
            event = {
                "event_id": event_id or f"evt-{len(self.log) + 1:06d}",
                "kind": kind,
                "occurred_at": parse_ts(occurred_at).isoformat() if occurred_at else self._now_fn().isoformat(),
                "subject_id": subject_id,
                "payload": payload,
            }
            if event["event_id"] in self._by_id:
                return self._by_id[event["event_id"]]  # 幂等：重复提交返回原事件
            link_event(self.log, event)
            self._by_id[event["event_id"]] = event
            self._apply(event)
            return event

    def _emit_derived(self, kind: str, subject_id: str, payload: dict[str, Any], event_id: str) -> dict[str, Any] | None:
        """写入派生事件；确定性 ID 已存在时跳过（重启恢复不重复）。"""
        if event_id in self._by_id:
            return None
        return self.append(kind, subject_id, payload, event_id=event_id)

    def _link_stored(self, event: dict[str, Any]) -> None:
        """逐字重放一条已存储事件（保留 occurred_at 原文，重算链哈希）。"""
        clean = {
            "event_id": event["event_id"],
            "kind": event["kind"],
            "occurred_at": event["occurred_at"],
            "subject_id": event["subject_id"],
            "payload": json.loads(json.dumps(event["payload"])),
        }
        link_event(self.log, clean)
        self._by_id[clean["event_id"]] = clean
        self._apply(clean)

    def _apply(self, event: dict[str, Any]) -> None:
        kind, subject, p = event["kind"], event["subject_id"], event["payload"]
        if kind == "SCREENING_REGISTERED":
            self._screenings[subject] = p
            self._withdrawn.discard(subject)
        elif kind == "SCREENING_CORRECTED":
            # 更正只影响未出发、未完成的候选；锁定事实保持不变。
            if subject not in self._departed and subject not in self._completed:
                merged = dict(self._screenings.get(subject, {}))
                merged.update(p.get("changes", {}))
                self._screenings[subject] = merged
        elif kind == "INDICATION_RECORDED":
            self._indications[subject] = set(p.get("skills_needed", []))
        elif kind == "ELIGIBILITY_SIGNED":
            self._consents[subject] = True
        elif kind == "CONSENT_REVOKED":
            self._consents[subject] = False
        elif kind == "FOLLOWUP_COMMITTED":
            self._followups[subject] = True
        elif kind == "MATERIAL_SUBMITTED":
            self._materials_until[subject] = parse_ts(p["valid_until"])
        elif kind == "MATERIAL_EXPIRED":
            self._materials_until[subject] = None
        elif kind == "CANDIDATE_WITHDRAWN":
            self._withdrawn.add(subject)
        elif kind == "FLIGHT_CHANGED":
            if subject not in self._departed and subject not in self._completed:
                merged = dict(self._screenings.get(subject, {}))
                merged["window"] = p["window"]
                self._screenings[subject] = merged
        elif kind == "REGION_TARGET_SET":
            self._targets[p["region"]] = int(p["target"])
        elif kind == "SLOT_PUBLISHED":
            self._slots[p["slot_id"]] = SlotFacts(
                slot_id=p["slot_id"],
                skill=p["skill"],
                window=(parse_ts(p["window"][0]), parse_ts(p["window"][1])),
                languages=frozenset(p.get("languages", [])),
                care_chain=tuple(p.get("care_chain", [])),
                capacity=int(p.get("capacity", 1)),
            )
        elif kind == "SLOT_HELD":
            self._holds[p["hold_id"]] = {
                "hold_id": p["hold_id"], "subject": subject, "slot_id": p["slot_id"],
                "partner": p["partner"], "expires_at": parse_ts(p["expires_at"]),
                "status": "active",
            }
        elif kind in ("HOLD_EXPIRED", "HOLD_RELEASED"):
            hold = self._holds.get(p["hold_id"])
            if hold:
                hold["status"] = "closed"
        elif kind == "SLOT_ALLOCATED":
            self._allocations[p["slot_id"]] = {
                "subject": subject, "slot_id": p["slot_id"],
                "hold_id": p["hold_id"], "event_id": event["event_id"],
            }
            hold = self._holds.get(p["hold_id"])
            if hold:
                hold["status"] = "confirmed"
            self._confirm_keys[p.get("request_key", event["event_id"])] = event["event_id"]
        elif kind == "ALLOCATION_REVOKED":
            # 未出发前因退出/材料失效撤销归属：名额回到池中并触发递补。
            self._allocations.pop(p["slot_id"], None)
            hold = self._holds.get(p["hold_id"])
            if hold:
                hold["status"] = "closed"
        elif kind == "EXCEPTION_REQUESTED":
            self._exceptions[p["request_id"]] = {
                "subject": subject, "slot_id": p["slot_id"], "reason": p["reason"],
                "requested_by": p["requested_by"], "status": "pending",
            }
        elif kind == "EXCEPTION_APPROVED":
            rec = self._exceptions[p["request_id"]]
            rec["status"] = "approved"
            rec["reviewer"] = p["reviewer"]
            rec["impact"] = p["impact"]
        elif kind == "EXCEPTION_REJECTED":
            self._exceptions[p["request_id"]]["status"] = "rejected"
        elif kind == "PATIENT_DEPARTED":
            self._departed.add(subject)
        elif kind == "CARE_COMPLETED":
            self._completed.add(subject)
        elif kind == "NOTIFICATION_SENT":
            self._notified.add(p["notify_key"])
        # WAITLIST_PROMOTED / DISCLOSURE_MADE / CANDIDATE_RANKED 等仅留痕，无投影变化。

    # ------------------------------------------------------------------ #
    # 事实投影
    # ------------------------------------------------------------------ #

    def _candidate_facts(self, subject: str) -> CandidateFacts | None:
        scr = self._screenings.get(subject)
        if scr is None:
            return None
        window = scr.get("window")
        return CandidateFacts(
            subject_id=subject,
            region=scr.get("region", ""),
            urgency=float(scr.get("urgency", 0.0)),
            skills_needed=frozenset(self._indications.get(subject, set())),
            languages=frozenset(scr.get("languages", [])),
            window=(parse_ts(window[0]), parse_ts(window[1])) if window else None,
            consent_signed=self._consents.get(subject, False),
            followup_committed=self._followups.get(subject, False),
            materials_valid_until=self._materials_until.get(subject),
            withdrawn=subject in self._withdrawn,
            identity_ref=scr.get("identity_ref", ""),
        )

    def _open_slots(self) -> list[SlotFacts]:
        return [s for s in self._slots.values() if s.slot_id not in self._allocations]

    def _region_allocated(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for alloc in self._allocations.values():
            region = self._screenings.get(alloc["subject"], {}).get("region", "")
            counts[region] = counts.get(region, 0) + 1
        return counts

    def _active_hold_for_subject(self, subject: str) -> dict[str, Any] | None:
        now = self._now_fn()
        for hold in self._holds.values():
            if hold["subject"] == subject and hold["status"] == "active" and hold["expires_at"] > now:
                return hold
        return None

    def _active_holds_for_slot(self, slot_id: str) -> list[dict[str, Any]]:
        now = self._now_fn()
        return [h for h in self._holds.values()
                if h["slot_id"] == slot_id and h["status"] == "active" and h["expires_at"] > now]

    # ------------------------------------------------------------------ #
    # 建议排序（只读，不产生归属）
    # ------------------------------------------------------------------ #

    def advise(self, *, persist: bool = True) -> dict[str, Any]:
        """对当前未归属名额产生可解释建议排序；结果只是建议，不改变归属。"""
        with self._lock:
            now = self._now_fn()
            candidates = [f for s in self._screenings if (f := self._candidate_facts(s)) is not None]
            suggestions = rank_candidates(
                candidates, self._open_slots(), self._targets, self._region_allocated(), now,
            )
            result = {
                "data_version": head_hash(self.log),
                "weights": dict(DEFAULT_WEIGHTS),
                "region_targets": dict(self._targets),
                "generated_at": now.isoformat(),
                "suggestions": [
                    {
                        "subject_id": s.subject_id, "slot_id": s.slot_id, "score": s.score,
                        "eligible": s.eligible, "gates": s.gates, "gate_notes": s.gate_notes,
                        "factors": s.factors, "contributions": s.contributions,
                        "fairness_flags": s.fairness_flags, "explanation": s.explain(),
                    }
                    for s in suggestions
                ],
            }
            if persist:
                self.append("CANDIDATE_RANKED", "system", {
                    "data_version": result["data_version"],
                    "weights": result["weights"],
                    "region_targets": result["region_targets"],
                    "suggestions": [
                        {k: v for k, v in s.items() if k != "explanation"}
                        for s in result["suggestions"]
                    ],
                })
            return result

    # ------------------------------------------------------------------ #
    # 占有与归属
    # ------------------------------------------------------------------ #

    def register_alias(self, subject_id: str, partner: str, alias: str) -> None:
        """登记合作方本地 ID 与全局假名 ID 的映射，用于跨合作方去重。"""
        with self._lock:
            key = f"{partner}:{alias}"
            existing = self._aliases.get(key)
            if existing is not None and existing != subject_id:
                raise AllocationError(f"别名 {key} 已映射到 {existing}，不能重复登记")
            self._aliases[key] = subject_id

    def resolve(self, partner: str, alias: str) -> str | None:
        return self._aliases.get(f"{partner}:{alias}")

    def place_hold(
        self,
        subject_id: str,
        slot_id: str,
        partner: str,
        *,
        ttl_seconds: int = DEFAULT_HOLD_TTL_SECONDS,
        hold_id: str | None = None,
    ) -> dict[str, Any]:
        """暂占有名额。同一患者跨合作方只能有一个有效占有或归属。

        需要幂等重试时，调用方应传入固定的 hold_id。
        """
        with self._lock:
            if slot_id not in self._slots:
                raise AllocationError(f"名额不存在: {slot_id}")
            if slot_id in self._allocations:
                raise AllocationError(f"名额已归属: {slot_id}")
            if subject_id in self._withdrawn:
                raise AllocationError(f"候选已退出: {subject_id}")
            existing = self._active_hold_for_subject(subject_id)
            if existing is not None:
                self._emit_derived("HELD_BY_OTHER_PARTNER", subject_id, {
                    "slot_id": slot_id, "rejected_partner": partner,
                    "held_by": existing["partner"], "active_hold_id": existing["hold_id"],
                }, event_id=f"reject-{slot_id}-{subject_id}-{partner}-{len(self.log) + 1:06d}")
                raise AllocationError(
                    f"患者已有有效占有 {existing['hold_id']}（合作方 {existing['partner']}），不能跨合作方重复占位"
                )
            if any(a["subject"] == subject_id for a in self._allocations.values()):
                raise AllocationError(f"患者已有归属名额，不能重复占位: {subject_id}")
            slot = self._slots[slot_id]
            if len(self._active_holds_for_slot(slot_id)) >= slot.capacity:
                raise AllocationError(f"名额容量已满: {slot_id}")
            hold_id = hold_id or f"hold-{slot_id}-{subject_id}-{len(self.log) + 1:06d}"
            known = self._holds.get(hold_id)
            if known is not None:
                if known["status"] == "active":
                    return known  # 幂等重试
                raise AllocationError(f"hold_id 已被使用: {hold_id}")
            expires_at = self._now_fn() + timedelta(seconds=ttl_seconds)
            self.append("SLOT_HELD", subject_id, {
                "hold_id": hold_id, "slot_id": slot_id, "partner": partner,
                "expires_at": expires_at.isoformat(),
            })
            return self._holds[hold_id]

    def confirm(self, hold_id: str, partner: str, *, request_key: str) -> dict[str, Any]:
        """确认归属：并发下只有一个确认成功；同一 request_key 幂等。"""
        with self._lock:
            if request_key in self._confirm_keys:
                return self._by_id[self._confirm_keys[request_key]]
            hold = self._holds.get(hold_id)
            if hold is None or hold["status"] != "active":
                raise AllocationError(f"占有不存在或已关闭: {hold_id}")
            if hold["expires_at"] <= self._now_fn():
                raise AllocationError(f"占有已过期: {hold_id}")
            if hold["partner"] != partner and hold["partner"] != BACKFILL_PARTNER:
                raise AllocationError(f"占有属于合作方 {hold['partner']}，{partner} 无权确认")
            slot_id = hold["slot_id"]
            if slot_id in self._allocations:
                raise AllocationError(f"名额已被归属: {slot_id}")
            snapshot = self.advise(persist=False)
            event = self.append("SLOT_ALLOCATED", hold["subject"], {
                "hold_id": hold_id, "slot_id": slot_id, "partner": partner,
                "request_key": request_key,
                "snapshot": {
                    "data_version": snapshot["data_version"],
                    "weights": snapshot["weights"],
                    "region_targets": snapshot["region_targets"],
                },
            })
            self._notify_handoff(slot_id, hold["subject"])
            return event

    def _notify_handoff(self, slot_id: str, subject: str) -> None:
        """交接通知：确定性键去重，重启恢复不会重复发送。"""
        slot = self._slots[slot_id]
        notify_key = f"handoff-{slot_id}-{subject}"
        self._emit_derived("NOTIFICATION_SENT", subject, {
            "notify_key": notify_key, "slot_id": slot_id,
            "recipients": list(slot.care_chain),
        }, event_id=f"notify-{notify_key}")

    # ------------------------------------------------------------------ #
    # 到期、退出与递补
    # ------------------------------------------------------------------ #

    def sweep_expired(self) -> list[dict[str, Any]]:
        """释放到期占有并按原规则递补。派生事件幂等，可安全重复调用。"""
        with self._lock:
            now = self._now_fn()
            released = []
            for hold in list(self._holds.values()):
                if hold["status"] == "active" and hold["expires_at"] <= now:
                    event = self._emit_derived("HOLD_EXPIRED", hold["subject"], {
                        "hold_id": hold["hold_id"], "slot_id": hold["slot_id"],
                    }, event_id=f"expire-{hold['hold_id']}")
                    if event is not None:
                        released.append(event)
                        # 逾期未确认者回到候补池：本次自动递补不再立即选回他，
                        # 协调员仍可手动重新占位；之后新一轮释放时他重新参与排序。
                        self._backfill(hold["slot_id"], exclude={hold["subject"]})
            return released

    def withdraw(self, subject_id: str, reason: str) -> None:
        """候选退出：释放其占有并按原规则递补。"""
        with self._lock:
            self.append("CANDIDATE_WITHDRAWN", subject_id, {"reason": reason})
            self._release_subject_hold(subject_id, cause="withdrawn")

    def expire_materials(self, subject_id: str) -> None:
        """材料过期：释放其占有并按原规则递补。"""
        with self._lock:
            self.append("MATERIAL_EXPIRED", subject_id, {})
            self._release_subject_hold(subject_id, cause="materials_expired")

    def _release_subject_hold(self, subject: str, cause: str) -> None:
        # 已出发/已完成的事实锁定：退出或材料失效都不能再改动其安排。
        if subject in self._departed or subject in self._completed:
            return
        alloc = next((a for a in self._allocations.values() if a["subject"] == subject), None)
        if alloc is not None:
            self._emit_derived("ALLOCATION_REVOKED", subject, {
                "hold_id": alloc["hold_id"], "slot_id": alloc["slot_id"], "cause": cause,
            }, event_id=f"revoke-{alloc['event_id']}-{cause}")
            self._backfill(alloc["slot_id"])
            return
        hold = self._active_hold_for_subject(subject)
        if hold is None:
            return
        self._emit_derived("HOLD_RELEASED", subject, {
            "hold_id": hold["hold_id"], "slot_id": hold["slot_id"], "cause": cause,
        }, event_id=f"release-{hold['hold_id']}-{cause}")
        self._backfill(hold["slot_id"])

    def _backfill(self, slot_id: str, exclude: set[str] | None = None) -> None:
        """按原排序规则为释放出的名额递补：取当前建议中排名最高的合格候选。"""
        if slot_id in self._allocations or slot_id not in self._slots:
            return
        now = self._now_fn()
        candidates = [f for s in self._screenings if (f := self._candidate_facts(s)) is not None]
        suggestions = rank_candidates(
            candidates, [self._slots[slot_id]], self._targets, self._region_allocated(), now,
        )
        for s in suggestions:
            if not s.eligible:
                continue
            if exclude and s.subject_id in exclude:
                continue
            if self._active_hold_for_subject(s.subject_id) is not None:
                continue
            if any(a["subject"] == s.subject_id for a in self._allocations.values()):
                continue
            hold_id = f"hold-{slot_id}-{s.subject_id}-bf{len(self.log) + 1:06d}"
            self._emit_derived("WAITLIST_PROMOTED", s.subject_id, {
                "slot_id": slot_id, "hold_id": hold_id, "score": s.score,
                "data_version": head_hash(self.log),
            }, event_id=f"promote-{hold_id}")
            self._emit_derived("SLOT_HELD", s.subject_id, {
                "hold_id": hold_id, "slot_id": slot_id, "partner": BACKFILL_PARTNER,
                "expires_at": (now + timedelta(seconds=DEFAULT_HOLD_TTL_SECONDS)).isoformat(),
            }, event_id=f"held-{hold_id}")
            return

    # ------------------------------------------------------------------ #
    # 例外复核
    # ------------------------------------------------------------------ #

    def request_exception(self, subject_id: str, slot_id: str, reason: str, requested_by: str, request_id: str) -> None:
        self.append("EXCEPTION_REQUESTED", subject_id, {
            "request_id": request_id, "slot_id": slot_id,
            "reason": reason, "requested_by": requested_by,
        })

    def approve_exception(self, request_id: str, reviewer: str, impact: str) -> dict[str, Any]:
        """独立复核批准：复核人不得是申请人，也不得在名额接诊链内；必须记录影响。"""
        with self._lock:
            rec = self._exceptions.get(request_id)
            if rec is None or rec["status"] != "pending":
                raise AllocationError(f"例外申请不存在或已处理: {request_id}")
            if reviewer == rec["requested_by"]:
                raise AllocationError("复核人必须独立于申请人")
            slot = self._slots.get(rec["slot_id"])
            if slot and reviewer in slot.care_chain:
                raise AllocationError("复核人不得属于该名额的接诊链")
            if not impact:
                raise AllocationError("例外批准必须记录影响")
            return self.append("EXCEPTION_APPROVED", rec["subject"], {
                "request_id": request_id, "reviewer": reviewer, "impact": impact,
            })

    def reject_exception(self, request_id: str, reviewer: str, note: str) -> None:
        with self._lock:
            rec = self._exceptions.get(request_id)
            if rec is None or rec["status"] != "pending":
                raise AllocationError(f"例外申请不存在或已处理: {request_id}")
            self.append("EXCEPTION_REJECTED", rec["subject"], {
                "request_id": request_id, "reviewer": reviewer, "note": note,
            })

    def exception_allocation(self, request_id: str, partner: str, *, request_key: str) -> dict[str, Any]:
        """凭已批准的例外直接建立归属（绕过自动门限，影响已记录）。"""
        with self._lock:
            rec = self._exceptions.get(request_id)
            if rec is None or rec["status"] != "approved":
                raise AllocationError(f"例外未获批准: {request_id}")
            hold = self.place_hold(rec["subject"], rec["slot_id"], partner,
                                   hold_id=f"hold-exc-{request_id}")
            return self.confirm(hold["hold_id"], partner, request_key=request_key)

    # ------------------------------------------------------------------ #
    # 出发、完成与隐私
    # ------------------------------------------------------------------ #

    def depart(self, subject_id: str) -> None:
        """患者出发：此后筛查更正与航班变化不再影响其安排。"""
        self.append("PATIENT_DEPARTED", subject_id, {})

    def complete_care(self, subject_id: str, outcome: str) -> None:
        """记录已完成的诊疗事实。该事实不可变，更正事件不会改变它。"""
        self.append("CARE_COMPLETED", subject_id, {"outcome": outcome})

    def disclose_identity(self, subject_id: str, requester: str) -> str:
        """身份资料仅向实际接诊链披露；每次披露都留痕。"""
        with self._lock:
            alloc = next((a for a in self._allocations.values() if a["subject"] == subject_id), None)
            if alloc is None:
                raise AllocationError(f"患者尚无归属名额，无接诊链可披露: {subject_id}")
            slot = self._slots[alloc["slot_id"]]
            if requester not in slot.care_chain:
                raise AllocationError(f"{requester} 不在接诊链 {list(slot.care_chain)} 内，拒绝披露")
            self.append("DISCLOSURE_MADE", subject_id, {
                "requester": requester, "slot_id": alloc["slot_id"],
            })
            return self._screenings[subject_id].get("identity_ref", "")

    # ------------------------------------------------------------------ #
    # 重算边界、恢复与持久化
    # ------------------------------------------------------------------ #

    def recompute_pending(self) -> dict[str, Any]:
        """筛查更正/航班变化后重算：只影响未出发、未完成的安排（占有与未出发归属）。"""
        with self._lock:
            locked = self._departed | self._completed
            for hold in list(self._holds.values()):
                if hold["status"] != "active" or hold["subject"] in locked:
                    continue
                if self._revoke_if_ineligible(hold["subject"], hold["slot_id"],
                                              hold_id=hold["hold_id"], via="hold"):
                    continue
            for alloc in list(self._allocations.values()):
                if alloc["subject"] in locked:
                    continue
                self._revoke_if_ineligible(alloc["subject"], alloc["slot_id"],
                                           hold_id=alloc["hold_id"], via="allocation")
            return self.advise(persist=True)

    def _revoke_if_ineligible(self, subject: str, slot_id: str, *, hold_id: str, via: str) -> bool:
        facts = self._candidate_facts(subject)
        if facts is None or facts.withdrawn:
            return False
        suggestions = rank_candidates(
            [facts], [self._slots[slot_id]], self._targets,
            self._region_allocated(), self._now_fn(),
        )
        if suggestions and suggestions[0].eligible:
            return False
        if via == "allocation":
            alloc = self._allocations.get(slot_id)
            if alloc is None or alloc["subject"] != subject:
                return False
            self._emit_derived("ALLOCATION_REVOKED", subject, {
                "hold_id": hold_id, "slot_id": slot_id,
                "cause": "recompute_ineligible",
            }, event_id=f"revoke-alloc-{slot_id}-{subject}-recompute")
        else:
            self._emit_derived("HOLD_RELEASED", subject, {
                "hold_id": hold_id, "slot_id": slot_id,
                "cause": "recompute_ineligible",
            }, event_id=f"release-{hold_id}-recompute")
        self._backfill(slot_id)
        return True

    def recover(self) -> None:
        """服务重启：校验哈希链、重放日志重建投影，并补做到期清扫（幂等）。"""
        with self._lock:
            verify_chain(self.log)
            self._reset_projection()
            for event in self.log:
                self._apply(event)
            self.sweep_expired()

    def dump(self) -> str:
        return json.dumps(self.log, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, data: str, now: Callable[[], datetime] | None = None) -> "AllocationService":
        """从持久化文本恢复；保留存储的链哈希并在 recover 中校验防篡改。"""
        service = cls(now=now)
        for event in json.loads(data):
            problems = validate_event(event)
            if problems:
                raise DomainError(f"持久化事件不合法: {problems}")
            if event["event_id"] in service._by_id:
                raise DomainError(f"持久化事件 ID 重复: {event['event_id']}")
            service.log.append(event)
            service._by_id[event["event_id"]] = event
        service.recover()
        return service

    # ------------------------------------------------------------------ #
    # 审计复算
    # ------------------------------------------------------------------ #

    def audit_allocation(self, allocation_event_id: str) -> dict[str, Any]:
        """复算某次归属：截断到归属前一刻的数据版本，重跑排序并比对。"""
        with self._lock:
            verify_chain(self.log)
            target = self._by_id.get(allocation_event_id)
            if target is None or target["kind"] != "SLOT_ALLOCATED":
                raise AllocationError(f"不是归属事件: {allocation_event_id}")
            idx = next(i for i, e in enumerate(self.log) if e["event_id"] == allocation_event_id)
            snapshot = target["payload"]["snapshot"]

            replay = AllocationService(now=self._now_fn)
            for event in self.log[:idx]:
                replay._link_stored(event)
            if head_hash(replay.log) != snapshot["data_version"]:
                raise DomainError("数据版本与归属记录不符，日志可能被篡改")

            slot_id = target["payload"]["slot_id"]
            subject = target["subject_id"]
            facts = replay._candidate_facts(subject)
            suggestions = rank_candidates(
                [facts] if facts else [], [replay._slots[slot_id]],
                snapshot["region_targets"], replay._region_allocated(),
                parse_ts(target["occurred_at"]), weights=snapshot["weights"],
            )
            reproduced = suggestions[0] if suggestions else None
            return {
                "allocation_event_id": allocation_event_id,
                "data_version": snapshot["data_version"],
                "weights": snapshot["weights"],
                "region_targets": snapshot["region_targets"],
                "subject": subject,
                "slot_id": slot_id,
                "eligible_at_allocation": reproduced.eligible if reproduced else False,
                "score_at_allocation": reproduced.score if reproduced else None,
                "gates": reproduced.gates if reproduced else {},
                "chain_verified": True,
            }
