"""medical_mission_allocation 领域资料的基础结构。"""

from __future__ import annotations

EVENT_KINDS = ['SCREENING_REGISTERED', 'ELIGIBILITY_SIGNED', 'SLOT_ALLOCATED', 'EXCEPTION_APPROVED', 'OUTCOME_HANDED_OFF']
REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

def validate_event(record: dict) -> list[str]:
    """检查样例事件是否具备可交换的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
