"""medical_mission_allocation 领域资料的基础结构。

保持历史模块路径与公开函数不变；完整实现见
`src.domain`（事件契约）、`src.ranking`（可解释排序）、`src.service`（分配服务）。
"""

from __future__ import annotations

from .domain import EVENT_KINDS, REQUIRED_FIELDS, validate_event
from .ranking import rank_candidates
from .service import AllocationService

__all__ = [
    "EVENT_KINDS",
    "REQUIRED_FIELDS",
    "validate_event",
    "rank_candidates",
    "AllocationService",
]
