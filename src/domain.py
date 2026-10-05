"""事件投影：患者任务台与资源台账。

两个读模型都只由事件构建，不接受直接修改；离线批次经仓库归并后，
重放同样的事件即可得到与现场一致的状态。

身份合并采用“登记行 + 可撤销别名”模型：每条登记保留自己的数据行，
合并只改变别名指向；撤销合并后，各行重新独立，合并期间产生的签署、
排期等记录仍按原 case_id 归属，不会丢失或错配。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from src.events import Event, EventType

# 临床优先级：1 最高（视力威胁，需紧急处理），4 最低。
PRIORITY_LEVELS: dict[int, str] = {
    1: "紧急-视力威胁",
    2: "高",
    3: "常规",
    4: "低",
}


def parse_dt(value: str) -> Any:
    from datetime import datetime
    return datetime.fromisoformat(value)


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return parse_dt(a_start) < parse_dt(b_end) and parse_dt(b_start) < parse_dt(a_end)


def contains(outer_start: str, outer_end: str, start: str, end: str) -> bool:
    return parse_dt(outer_start) <= parse_dt(start) and parse_dt(end) <= parse_dt(outer_end)


@dataclass
class MergeRecord:
    event_id: str
    kept_case_id: str
    merged_case_id: str
    reason: str
    at: str
    reverted: bool = False
    revert_reason: str | None = None


@dataclass
class PriorityChange:
    level: int
    level_label: str
    reason: str
    set_by: str
    at: str
    category: str | None = None


@dataclass
class PatientCase:
    """一位患者在当前身份解析下的聚合视图。"""

    case_id: str
    member_ids: list[str] = field(default_factory=list)
    display_name: str = ""
    local_patient_id: str = ""
    name_variants: list[str] = field(default_factory=list)
    preferred_language: str | None = None
    screenings: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] | None = None        # 最近一次医生复核签署
    consent: dict[str, Any] | None = None       # 最近一次有效同意
    priority: PriorityChange | None = None
    priority_history: list[PriorityChange] = field(default_factory=list)
    reservation: dict[str, Any] | None = None   # 当前有效排期
    treatment: dict[str, Any] | None = None     # 已完成治疗
    handoff: dict[str, Any] | None = None       # 术后接续
    followups: list[dict[str, Any]] = field(default_factory=list)

    @property
    def surgery_eligible(self) -> bool:
        """手术资格只承认有资质医生签署的复核结论，AI 建议永不构成资格。"""
        return bool(
            self.review is not None
            and self.review.get("eligible_for_surgery") is True
            and self.review.get("license_id")
        )

    def missing_checklist(self) -> list[dict[str, str]]:
        """任务台四项缺口：筛查复核 / 知情同意 / 手术资源 / 术后去向。"""
        items: list[dict[str, str]] = []
        if self.review is None:
            items.append({"code": "screening_review",
                          "label": "尚缺医生筛查复核签署"})
        elif self.review.get("eligible_for_surgery") is not True:
            items.append({"code": "screening_review",
                          "label": "医生未确认手术资格（AI 结果不可替代签署）"})
        if self.consent is None:
            items.append({"code": "informed_consent",
                          "label": "尚缺知情同意签署"})
        if self.treatment is None and self.reservation is None:
            items.append({"code": "surgery_resource",
                          "label": "尚未落实手术房间/耗材/团队排期"})
        if self.handoff is None:
            items.append({"code": "postop_destination",
                          "label": "尚缺术后去向与当地接续随访"})
        return items


@dataclass
class _CaseRow:
    """单次登记产生的原始数据行，合并/撤销都不销毁行本身。"""

    case_id: str
    display_name: str = ""
    local_patient_id: str = ""
    name_variants: list[str] = field(default_factory=list)
    preferred_language: str | None = None
    screenings: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] | None = None
    consent: dict[str, Any] | None = None
    priority_history: list[PriorityChange] = field(default_factory=list)
    reservation: dict[str, Any] | None = None
    treatment: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None
    followups: list[dict[str, Any]] = field(default_factory=list)

    @property
    def priority(self) -> PriorityChange | None:
        return self.priority_history[-1] if self.priority_history else None


class PatientBoard:
    def __init__(self) -> None:
        self._rows: dict[str, _CaseRow] = {}
        self._aliases: dict[str, str] = {}   # 被合并 id -> 保留 id（仅未撤销）
        self.merges: list[MergeRecord] = []
        self._orphans: dict[str, list[Event]] = {}  # 早于登记到达的病例事件

    def handle(self, event: Event) -> None:
        et = event.event_type
        p = event.payload
        if et == EventType.PATIENT_REGISTERED:
            row = self._rows.setdefault(event.aggregate_id, _CaseRow(event.aggregate_id))
            row.local_patient_id = p["local_patient_id"]
            row.display_name = p["display_name"]
            row.name_variants = list(dict.fromkeys(p.get("name_variants", [])))
            row.preferred_language = p.get("preferred_language")
            # 离线归并可能让筛查/签署事件早于登记到达，登记时补放。
            for pending in self._orphans.pop(event.aggregate_id, []):
                self._route_case_event(row, pending)
        elif et == EventType.IDENTITY_MERGED:
            self.merges.append(MergeRecord(
                event_id=event.event_id, kept_case_id=p["kept_case_id"],
                merged_case_id=p["merged_case_id"], reason=p["reason"],
                at=event.occurred_at))
            self._aliases[p["merged_case_id"]] = self.canonical(p["kept_case_id"])
        elif et == EventType.IDENTITY_MERGE_REVERTED:
            record = next((m for m in self.merges
                           if m.event_id == p["merge_event_id"]), None)
            if record is not None and not record.reverted:
                record.reverted = True
                record.revert_reason = p["reason"]
                self._aliases.pop(record.merged_case_id, None)
        else:
            case_id = p.get("case_id")
            if not case_id:
                return
            row = self._rows.get(case_id)
            if row is None:
                self._orphans.setdefault(case_id, []).append(event)
            else:
                self._route_case_event(row, event)

    def _route_case_event(self, row: _CaseRow, event: Event) -> None:
        et = event.event_type
        p = event.payload
        if et == EventType.SCREENING_RECORDED:
            row.screenings.append({
                "event_id": event.event_id,
                "device_id": p.get("device_id"),
                "algorithm_name": p.get("algorithm_name"),
                "algorithm_version": p.get("algorithm_version"),
                "findings": p.get("findings"),
                "ai_suggestion": p.get("ai_suggestion"),
                "is_ai_result": bool(p.get("is_ai_result")),
                "recorded_by": p["recorded_by"],
                "at": event.occurred_at,
            })
        elif et == EventType.SCREENING_REVIEWED:
            row.review = {
                "reviewer": p["reviewer"], "license_id": p["license_id"],
                "clinical_decision": p["clinical_decision"],
                "eligible_for_surgery": p.get("eligible_for_surgery"),
                "diagnosis_codes": p.get("diagnosis_codes", []),
                "findings": p.get("findings"),
                "notes": p.get("notes"), "at": event.occurred_at,
            }
        elif et == EventType.CONSENT_SIGNED:
            row.consent = {
                "language": p["language"], "consent_version": p["consent_version"],
                "signed_by": p["signed_by"],
                "interpreter_present": p.get("interpreter_present", False),
                "interpreter_language": p.get("interpreter_language"),
                "procedure_scope": p.get("procedure_scope"),
                "at": p.get("signed_at", event.occurred_at),
            }
        elif et == EventType.PRIORITY_SET:
            row.priority_history.append(PriorityChange(
                level=p["priority_level"],
                level_label=PRIORITY_LEVELS.get(p["priority_level"], "未知"),
                reason=p["reason"], set_by=p["set_by"],
                at=event.occurred_at, category=p.get("category")))
        elif et == EventType.SLOT_RESERVED:
            row.reservation = {
                "reservation_id": p["reservation_id"], "procedure": p["procedure"],
                "room_id": p["room_id"], "start": p["start"], "end": p["end"],
                "surgeon_id": p["surgeon_id"], "supplies": dict(p["supplies"]),
                "followup_date": p["followup_date"], "kind": p.get("kind", "scheduled"),
                "addon_request_id": p.get("addon_request_id"),
                "at": event.occurred_at,
            }
        elif et == EventType.SLOT_RELEASED:
            if row.reservation and row.reservation["reservation_id"] == p["reservation_id"]:
                row.reservation = None
        elif et == EventType.TREATMENT_PLANNED:
            # 计划事件不改变四项缺口；资源缺口由 SLOT_RESERVED 关闭。
            pass
        elif et == EventType.TREATMENT_COMPLETED:
            row.treatment = {
                "procedure": p["procedure"], "surgeon_id": p["surgeon_id"],
                "license_id": p["license_id"], "completed_at": p["completed_at"],
                "reservation_id": p.get("reservation_id"),
                "notes": p.get("notes"),
            }
            if row.reservation and row.reservation["reservation_id"] == p.get("reservation_id"):
                # 治疗完成后房间/团队时段成为历史，不再占用未来排期。
                row.reservation = None
        elif et == EventType.HANDOFF_ACCEPTED:
            row.handoff = {
                "facility_id": p["facility_id"],
                "receiving_clinician": p["receiving_clinician"],
                "followup_plan": p["followup_plan"],
                "training_handover": p.get("training_handover"),
                "contacts": p.get("contacts"),
                "at": p.get("accepted_at", event.occurred_at),
            }
        elif et == EventType.FOLLOWUP_SCHEDULED:
            row.followups.append({
                "facility_id": p["facility_id"], "date": p["followup_date"],
                "clinician": p.get("clinician"), "note": p.get("note"),
                "at": event.occurred_at,
            })

    # ---- 查询 --------------------------------------------------------------

    def canonical(self, case_id: str) -> str:
        """跟随未撤销的合并别名，返回当前权威病例 id。"""
        seen: set[str] = set()
        current = case_id
        while current in self._aliases and current not in seen:
            seen.add(current)
            current = self._aliases[current]
        return current

    def _members(self, canonical_id: str) -> list[_CaseRow]:
        return [row for cid, row in self._rows.items()
                if self.canonical(cid) == canonical_id]

    def _roots(self) -> list[str]:
        roots: list[str] = []
        for cid in self._rows:
            if self.canonical(cid) == cid:
                roots.append(cid)
        return roots

    def get(self, case_id: str) -> PatientCase:
        canonical = self.canonical(case_id)
        rows = self._members(canonical)
        if not rows:
            raise KeyError(f"未登记的病例：{case_id}")
        return self._assemble(canonical, rows)

    @staticmethod
    def _latest(rows: list[_CaseRow], attr: str) -> Any:
        candidates = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
        if not candidates:
            return None
        if attr == "reservation":
            return max(candidates, key=lambda v: v["at"])
        if attr == "review" or attr == "consent" or attr == "handoff":
            return max(candidates, key=lambda v: v["at"])
        if attr == "treatment":
            return max(candidates, key=lambda v: v["completed_at"])
        return candidates[-1]

    def _assemble(self, canonical: str, rows: list[_CaseRow]) -> PatientCase:
        rows = sorted(rows, key=lambda r: r.case_id)
        primary = next((r for r in rows if r.case_id == canonical), rows[0])
        history = sorted((c for r in rows for c in r.priority_history),
                         key=lambda c: c.at)
        return PatientCase(
            case_id=canonical,
            member_ids=[r.case_id for r in rows],
            display_name=primary.display_name,
            local_patient_id=primary.local_patient_id,
            name_variants=list(dict.fromkeys(
                v for r in rows for v in r.name_variants)),
            preferred_language=primary.preferred_language,
            screenings=[s for r in rows for s in r.screenings],
            review=self._latest(rows, "review"),
            consent=self._latest(rows, "consent"),
            priority=history[-1] if history else None,
            priority_history=history,
            reservation=self._latest(rows, "reservation"),
            treatment=self._latest(rows, "treatment"),
            handoff=self._latest(rows, "handoff"),
            followups=[f for r in rows for f in r.followups],
        )

    def cases(self) -> list[PatientCase]:
        return [self.get(root) for root in self._roots()]

    def worklist(self) -> list[dict[str, Any]]:
        """任务台列表：按优先级（1 最高）与缺口数量排序。"""
        rows = []
        for case in self.cases():
            rows.append({
                "case_id": case.case_id,
                "member_ids": case.member_ids,
                "display_name": case.display_name,
                "priority_level": case.priority.level if case.priority else None,
                "priority_label": case.priority.level_label if case.priority else "未评级",
                "surgery_eligible": case.surgery_eligible,
                "missing": case.missing_checklist(),
                "reservation": case.reservation,
                "handoff_facility": case.handoff["facility_id"] if case.handoff else None,
                "treatment_completed": case.treatment is not None,
            })
        rows.sort(key=lambda r: (r["priority_level"] is None,
                                 r["priority_level"] or 99,
                                 -len(r["missing"])))
        return rows


@dataclass
class ReservationView:
    reservation_id: str
    case_id: str
    procedure: str
    room_id: str
    start: str
    end: str
    surgeon_id: str
    supplies: dict[str, int]
    followup_date: str
    kind: str = "scheduled"
    addon_request_id: str | None = None


class ResourceLedger:
    """房间、无菌耗材、团队班次与当地随访容量的统一投影。"""

    def __init__(self) -> None:
        self.rooms: dict[str, tuple[str, str]] = {}
        self.supplies: dict[str, int] = {}
        self.followup_capacity: dict[str, int] = {}   # date -> 总容量
        self.shifts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.reservations: dict[str, ReservationView] = {}
        self.addon_decisions: dict[str, dict[str, Any]] = {}

    def handle(self, event: Event) -> None:
        et = event.event_type
        p = event.payload
        if et == EventType.RESOURCE_LEDGER_UPDATED:
            kind = p["ledger_kind"]
            if kind == "room":
                for room in p.get("rooms", []):
                    self.rooms[room["room_id"]] = (room["available_from"],
                                                   room["available_to"])
            elif kind == "supply":
                self.supplies.update({k: int(v) for k, v in p.get("items", {}).items()})
            elif kind == "followup_capacity":
                self.followup_capacity.update(
                    {d: int(v) for d, v in p.get("capacity_by_date", {}).items()})
        elif et == EventType.TEAM_SHIFT_DECLARED:
            self.shifts[p["staff_id"]].append({
                "staff_id": p["staff_id"], "role": p["role"],
                "start": p["shift_start"], "end": p["shift_end"],
                "max_cases": p.get("max_cases"),
                "max_duty_minutes": p.get("max_duty_minutes"),
            })
        elif et == EventType.SLOT_RESERVED:
            view = ReservationView(
                reservation_id=p["reservation_id"], case_id=p["case_id"],
                procedure=p["procedure"], room_id=p["room_id"],
                start=p["start"], end=p["end"], surgeon_id=p["surgeon_id"],
                supplies={k: int(v) for k, v in p.get("supplies", {}).items()},
                followup_date=p["followup_date"], kind=p.get("kind", "scheduled"),
                addon_request_id=p.get("addon_request_id"))
            self.reservations[view.reservation_id] = view
        elif et == EventType.SLOT_RELEASED:
            self.reservations.pop(p["reservation_id"], None)
        elif et == EventType.TREATMENT_COMPLETED:
            rid = p.get("reservation_id")
            if rid:
                self.reservations.pop(rid, None)
        elif et == EventType.URGENT_ADDON_DECIDED:
            self.addon_decisions[p["addon_request_id"]] = {
                "approved": p["approved"],
                "reservation_id": p.get("reservation_id"),
                "rejection_reasons": p.get("rejection_reasons", []),
                "decided_by": p["decided_by"], "at": event.occurred_at,
            }

    # ---- 容量查询 ----------------------------------------------------------

    def active_reservations(self) -> list[ReservationView]:
        return list(self.reservations.values())

    def room_busy(self, room_id: str, start: str, end: str) -> list[ReservationView]:
        return [r for r in self.reservations.values()
                if r.room_id == room_id and overlaps(r.start, r.end, start, end)]

    def held_supplies(self) -> dict[str, int]:
        used: dict[str, int] = defaultdict(int)
        for r in self.reservations.values():
            for code, qty in r.supplies.items():
                used[code] += qty
        return dict(used)

    def available_supplies(self) -> dict[str, int]:
        used = self.held_supplies()
        return {code: total - used.get(code, 0)
                for code, total in self.supplies.items()}

    def followup_used(self, date: str) -> int:
        return sum(1 for r in self.reservations.values()
                   if r.followup_date == date)

    def surgeon_reservations(self, staff_id: str) -> list[ReservationView]:
        return sorted((r for r in self.reservations.values()
                       if r.surgeon_id == staff_id), key=lambda r: r.start)

    def team_load(self) -> dict[str, dict[str, Any]]:
        """每位团队成员在已声明班次内的负荷。"""
        load: dict[str, dict[str, Any]] = {}
        for staff_id, shifts in self.shifts.items():
            cases = self.surgeon_reservations(staff_id)
            load[staff_id] = {
                "shifts": shifts,
                "cases": [{"reservation_id": c.reservation_id,
                           "case_id": c.case_id, "start": c.start, "end": c.end}
                          for c in cases],
                "case_count": len(cases),
            }
        return load
