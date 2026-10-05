"""海外光明行任务台领域事件定义。

事件是事实记录：一经追加，event_id / aggregate_id / occurred_at / version
不允许原地修改；业务更正必须产生后继事件（例如优先级变化、撤销合并）。

AI 设备输出只作为辅助证据进入系统，诊断与手术资格始终由有资质医生在
SCREENING_REVIEWED 等签署事件中确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class EventType(str, Enum):
    # 患者与身份
    PATIENT_REGISTERED = "PATIENT_REGISTERED"
    IDENTITY_MERGED = "IDENTITY_MERGED"
    IDENTITY_MERGE_REVERTED = "IDENTITY_MERGE_REVERTED"
    # 筛查
    SCREENING_RECORDED = "SCREENING_RECORDED"   # 纸质/便携设备结果（可能来自 AI）
    SCREENING_REVIEWED = "SCREENING_REVIEWED"   # 有资质医生复核签署
    # 同意
    CONSENT_SIGNED = "CONSENT_SIGNED"
    # 优先级（每次变化都留痕，供复盘解释）
    PRIORITY_SET = "PRIORITY_SET"
    # 排程与资源
    RESOURCE_LEDGER_UPDATED = "RESOURCE_LEDGER_UPDATED"  # 房间/耗材/随访容量盘点
    TEAM_SHIFT_DECLARED = "TEAM_SHIFT_DECLARED"          # 团队班次（疲劳核算依据）
    SLOT_RESERVED = "SLOT_RESERVED"
    SLOT_RELEASED = "SLOT_RELEASED"
    URGENT_ADDON_REQUESTED = "URGENT_ADDON_REQUESTED"
    URGENT_ADDON_DECIDED = "URGENT_ADDON_DECIDED"
    TREATMENT_PLANNED = "TREATMENT_PLANNED"
    TREATMENT_COMPLETED = "TREATMENT_COMPLETED"
    # 术后接续
    HANDOFF_ACCEPTED = "HANDOFF_ACCEPTED"
    FOLLOWUP_SCHEDULED = "FOLLOWUP_SCHEDULED"
    # 复盘
    REVIEW_EXPORT_AUTHORIZED = "REVIEW_EXPORT_AUTHORIZED"


class AggregateType(str, Enum):
    PATIENT_CASE = "patient_case"
    SCREENING_RESULT = "screening_result"
    CONSENT = "consent"
    TREATMENT_SLOT = "treatment_slot"
    RESOURCE_LEDGER = "resource_ledger"
    TEAM_ROSTER = "team_roster"
    IDENTITY_MERGE = "identity_merge"
    FOLLOWUP_HANDOFF = "followup_handoff"
    MISSION_REVIEW = "mission_review"


# 每种事件携带的业务字段约定。required 为必须项；其余为可选项。
PAYLOAD_SPEC: dict[str, dict[str, set[str]]] = {
    EventType.PATIENT_REGISTERED: {
        "required": {"local_patient_id", "display_name"},
        "optional": {"name_variants", "gender", "approx_age", "preferred_language",
                      "source", "registered_by", "notes"},
    },
    EventType.IDENTITY_MERGED: {
        "required": {"kept_case_id", "merged_case_id", "reason"},
        "optional": {"merged_by"},
    },
    EventType.IDENTITY_MERGE_REVERTED: {
        "required": {"merge_event_id", "reason"},
        "optional": {"reverted_by"},
    },
    EventType.SCREENING_RECORDED: {
        "required": {"case_id", "recorded_by"},
        "optional": {"device_id", "algorithm_name", "algorithm_version",
                      "findings", "ai_suggestion", "is_ai_result",
                      "source_document", "measured_at"},
    },
    EventType.SCREENING_REVIEWED: {
        "required": {"case_id", "reviewer", "license_id", "clinical_decision"},
        "optional": {"screening_event_ids", "findings", "diagnosis_codes",
                      "eligible_for_surgery", "notes"},
    },
    EventType.CONSENT_SIGNED: {
        "required": {"case_id", "language", "consent_version", "signed_by"},
        "optional": {"interpreter_present", "interpreter_language",
                      "procedure_scope", "witness", "signed_at"},
    },
    EventType.PRIORITY_SET: {
        "required": {"case_id", "priority_level", "reason", "set_by"},
        "optional": {"category", "expires_at"},
    },
    EventType.RESOURCE_LEDGER_UPDATED: {
        "required": {"ledger_kind", "facility_id"},
        # kind=room:   rooms=[{room_id, available_from, available_to}]
        # kind=supply: items={supply_code: available_count}（无菌物资等）
        # kind=followup_capacity: capacity_by_date={date: slots}
        "optional": {"rooms", "items", "capacity_by_date", "note", "updated_by"},
    },
    EventType.TEAM_SHIFT_DECLARED: {
        "required": {"staff_id", "role", "shift_start", "shift_end"},
        "optional": {"max_cases", "max_duty_minutes", "facility_id"},
    },
    EventType.SLOT_RESERVED: {
        "required": {"reservation_id", "case_id", "procedure", "room_id",
                      "start", "end", "surgeon_id", "supplies",
                      "followup_date"},
        "optional": {"request_id", "kind", "addon_request_id"},
    },
    EventType.SLOT_RELEASED: {
        "required": {"reservation_id", "reason"},
        "optional": {"released_by"},
    },
    EventType.URGENT_ADDON_REQUESTED: {
        "required": {"addon_request_id", "case_id", "procedure", "room_id",
                      "requested_start", "requested_end", "surgeon_id",
                      "supplies", "followup_date", "requested_by"},
        "optional": {"displace_reservation_ids", "note"},
    },
    EventType.URGENT_ADDON_DECIDED: {
        "required": {"addon_request_id", "approved", "decided_by"},
        "optional": {"reservation_id", "rejection_reasons", "checked_at"},
    },
    EventType.TREATMENT_PLANNED: {
        "required": {"case_id", "procedure", "planned_by"},
        "optional": {"reservation_id", "notes"},
    },
    EventType.TREATMENT_COMPLETED: {
        "required": {"case_id", "procedure", "surgeon_id", "license_id",
                      "completed_at"},
        "optional": {"reservation_id", "supplies_consumed", "notes"},
    },
    EventType.HANDOFF_ACCEPTED: {
        "required": {"case_id", "facility_id", "receiving_clinician",
                      "followup_plan"},
        "optional": {"training_handover", "accepted_at", "contacts",
                      "reservation_id"},
    },
    EventType.FOLLOWUP_SCHEDULED: {
        "required": {"case_id", "facility_id", "followup_date"},
        "optional": {"clinician", "note"},
    },
    EventType.REVIEW_EXPORT_AUTHORIZED: {
        "required": {"authorization_id", "authorized_by", "scope",
                      "granted_at"},
        "optional": {"case_ids", "purpose", "expires_at"},
    },
}


@dataclass(frozen=True)
class Event:
    """不可变事件信封。payload 之外的字段归并后不得再改。"""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    source: str = "online"          # online / offline
    captured_at: str | None = None  # 离线采集时间

    def to_dict(self) -> dict[str, Any]:
        data = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "summary": self.summary,
            "payload": self.payload,
            "source": self.source,
        }
        if self.request_id:
            data["request_id"] = self.request_id
        if self.captured_at:
            data["captured_at"] = self.captured_at
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            aggregate_type=data["aggregate_type"],
            aggregate_id=data["aggregate_id"],
            occurred_at=data["occurred_at"],
            version=data["version"],
            summary=data["summary"],
            payload=data.get("payload", {}),
            request_id=data.get("request_id"),
            source=data.get("source", "online"),
            captured_at=data.get("captured_at"),
        )


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event(
    event_type: EventType,
    aggregate_type: AggregateType,
    aggregate_id: str,
    version: int,
    summary: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    occurred_at: str | None = None,
    request_id: str | None = None,
    source: str = "online",
    captured_at: str | None = None,
) -> Event:
    """构造事件；业务代码统一经此函数，确保标识与时间完整。"""
    return Event(
        event_id=event_id or f"evt-{uuid4().hex[:12]}",
        event_type=event_type.value if isinstance(event_type, EventType) else event_type,
        aggregate_type=aggregate_type.value if isinstance(aggregate_type, AggregateType) else aggregate_type,
        aggregate_id=aggregate_id,
        occurred_at=occurred_at or utcnow(),
        version=version,
        summary=summary,
        payload=payload,
        request_id=request_id,
        source=source,
        captured_at=captured_at,
    )
