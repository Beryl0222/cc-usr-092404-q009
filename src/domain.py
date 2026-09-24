"""国际义诊候选分配——领域事件契约。

事件采用追加式（append-only）日志：筛查更正、航班变化等都以*新事件*表达，
既有事件不被修改。相邻事件通过哈希链串联，审计方可校验事件序列是否被篡改，
并可在任一历史哈希处截断日志以复算当时的分配建议。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

# 既有事件名保持不变，保证旧样例与旧消费方继续可用。
EVENT_KINDS = [
    # 候选与资料
    "SCREENING_REGISTERED",      # 筛查登记（含风险分层、地区、语言、窗口、身份资料）
    "SCREENING_CORRECTED",       # 筛查结果更正：不覆盖旧事件，只产生新版本
    "INDICATION_RECORDED",       # 适应证与所需技能
    "MATERIAL_SUBMITTED",        # 签证/病历等材料提交，带过期时间
    "MATERIAL_EXPIRED",          # 材料失效
    "CANDIDATE_WITHDRAWN",       # 候选退出
    "ELIGIBILITY_SIGNED",        # 知情授权签署
    "CONSENT_REVOKED",           # 授权撤回
    "FOLLOWUP_COMMITTED",        # 当地合作方承诺术后随访
    "FLIGHT_CHANGED",            # 航班/行程窗口变化
    # 名额与公平目标
    "REGION_TARGET_SET",         # 地区公平目标（每地区目标名额）
    "SLOT_PUBLISHED",            # 名额公布：技能、行程窗口、语言要求、接诊链
    "CANDIDATE_RANKED",          # 系统产生的*建议*排序（含逐因素解释与数据版本）
    # 占有与归属
    "SLOT_HELD",                 # 名额暂占有，带 expires_at
    "HELD_BY_OTHER_PARTNER",     # 跨合作方占位被拒绝（仅记录拒绝事实）
    "HOLD_EXPIRED",              # 暂存到期
    "HOLD_RELEASED",             # 因退出/失效/重算等原因释放
    "WAITLIST_PROMOTED",         # 候补按原排序升级为新占有
    "SLOT_ALLOCATED",            # 并发确认后唯一的归属事实
    "ALLOCATION_REVOKED",        # 未出发前因退出/材料失效/重算不合格撤销归属并递补
    # 例外
    "EXCEPTION_REQUESTED",
    "EXCEPTION_APPROVED",        # 独立复核人批准，并记录影响
    "EXCEPTION_REJECTED",
    # 隐私与交接
    "DISCLOSURE_MADE",           # 身份资料仅向实际接诊链披露
    "PATIENT_DEPARTED",          # 患者已出发：安排锁定
    "CARE_COMPLETED",            # 已完成的诊疗事实：不可变
    "NOTIFICATION_SENT",         # 交接通知（幂等，重启不重复）
    "OUTCOME_HANDED_OFF",
]

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

# 这些事实一旦发生，筛查更正与航班变化都不得再改变其安排。
IMMUTABLE_ARRANGEMENT_KINDS = ("PATIENT_DEPARTED", "CARE_COMPLETED")


class DomainError(ValueError):
    """事件违反领域契约。"""


def parse_ts(value: str | datetime) -> datetime:
    """解析时间戳，要求带时区，保证跨节点比较确定。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise DomainError(f"时间戳缺少时区信息: {value!r}")
    return dt.astimezone(timezone.utc)


def validate_event(record: dict[str, Any]) -> list[str]:
    """检查事件是否具备可交换的最小字段。

    保持历史签名：返回缺失/不合法字段名列表，空列表表示通过。
    """
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    if "occurred_at" in record:
        try:
            parse_ts(record["occurred_at"])
        except (DomainError, ValueError):
            problems.append("occurred_at")
    if "payload" in record and not isinstance(record["payload"], dict):
        problems.append("payload")
    return problems


def canonical(event: dict[str, Any]) -> bytes:
    """事件的规范化字节序列（不含链哈希本身）。"""
    base = {k: v for k, v in event.items() if k not in ("chain_hash",)}
    return json.dumps(base, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def hash_event(event: dict[str, Any], prev_hash: str) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + canonical(event)).hexdigest()


def link_event(log: list[dict[str, Any]], event: dict[str, Any]) -> dict[str, Any]:
    """校验并把事件挂到哈希链末尾，返回写入后的事件。"""
    problems = validate_event(event)
    if problems:
        raise DomainError(f"事件字段不合法: {problems}")
    if any(e["event_id"] == event["event_id"] for e in log):
        raise DomainError(f"事件 ID 重复: {event['event_id']}")
    prev_hash = log[-1]["chain_hash"] if log else "GENESIS"
    event["prev_hash"] = prev_hash
    event["chain_hash"] = hash_event(event, prev_hash)
    log.append(event)
    return event


def verify_chain(log: list[dict[str, Any]]) -> None:
    """逐条复核哈希链；任何篡改或缺序都会抛出 DomainError。"""
    prev_hash = "GENESIS"
    for event in log:
        if event.get("prev_hash") != prev_hash:
            raise DomainError(f"哈希链断裂于 {event.get('event_id')}")
        if hash_event(event, prev_hash) != event.get("chain_hash"):
            raise DomainError(f"事件内容与哈希不符: {event.get('event_id')}")
        prev_hash = event["chain_hash"]


def head_hash(log: list[dict[str, Any]]) -> str:
    """当前日志头哈希，即评分所依据的数据版本。"""
    return log[-1]["chain_hash"] if log else "GENESIS"
