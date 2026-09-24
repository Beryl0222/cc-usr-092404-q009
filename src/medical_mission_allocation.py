"""medical_mission_allocation 领域资料的基础结构。"""

from __future__ import annotations

EVENT_KINDS = [
    # 筛查与授权
    "SCREENING_REGISTERED",
    "SCREENING_CORRECTED",
    "ELIGIBILITY_SIGNED",
    # 可解释评估（只产生建议）
    "CANDIDATE_EVALUATED",
    # 名额生命周期
    "SLOT_HOLD_PLACED",
    "SLOT_HOLD_RELEASED",
    "WAITLIST_PROMOTED",
    "ALLOCATION_CONFIRMED",
    "SLOT_ALLOCATED",
    # 例外审批
    "EXCEPTION_REQUESTED",
    "EXCEPTION_APPROVED",
    # 行程与诊疗事实
    "ITINERARY_CHANGED",
    "DEPARTURE_RECORDED",
    "TREATMENT_COMPLETED",
    # 披露与交接
    "IDENTITY_DISCLOSED",
    "OUTCOME_HANDED_OFF",
]

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

# 各事件在 payload 中必须携带的交换字段（最小集合，允许额外字段）。
PAYLOAD_CONTRACTS = {
    "SCREENING_REGISTERED": ("region", "urgency", "screening_status", "risk_score"),
    "SCREENING_CORRECTED": ("corrects_event_id", "changes", "recompute_scope"),
    "ELIGIBILITY_SIGNED": ("indication", "consent_signed", "follow_up_committed", "language"),
    "CANDIDATE_EVALUATED": (
        "slot_id", "policy_version", "data_version", "eligible", "factors",
        "suggested_rank", "inputs",
    ),
    "SLOT_HOLD_PLACED": ("hold_id", "slot_id", "partner_id", "expires_at", "idempotency_key"),
    "SLOT_HOLD_RELEASED": ("hold_id", "reason", "idempotency_key"),
    "WAITLIST_PROMOTED": (
        "released_hold_id", "hold_id", "rule_version", "expires_at", "idempotency_key",
    ),
    "ALLOCATION_CONFIRMED": ("hold_id", "confirmed_by", "fence_token", "policy_version", "data_version"),
    "SLOT_ALLOCATED": ("slot_id", "partner_id"),
    "EXCEPTION_REQUESTED": ("requested_by", "reason", "deviates_from"),
    "EXCEPTION_APPROVED": ("requested_by", "approved_by", "impact"),
    "ITINERARY_CHANGED": ("affects_event_id", "recompute_scope"),
    "DEPARTURE_RECORDED": ("hold_id",),
    "TREATMENT_COMPLETED": ("hold_id", "summary_ref"),
    "IDENTITY_DISCLOSED": ("disclosed_to", "scope"),
    "OUTCOME_HANDED_OFF": ("hold_id", "handed_to", "idempotency_key"),
}


def validate_event(record: dict) -> list[str]:
    """检查样例事件是否具备可交换的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    kind = record.get("kind")
    if kind not in EVENT_KINDS:
        problems.append("kind")
    payload = record.get("payload")
    if kind in EVENT_KINDS and isinstance(payload, dict):
        problems.extend(
            f"payload.{name}"
            for name in PAYLOAD_CONTRACTS.get(kind, ())
            if name not in payload
        )
    return problems
