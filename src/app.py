"""应用服务：把协调组的操作翻译为领域事件，并装配读模型。

所有写操作都经 EventStore 追加事件；MissionService 不保存可变业务状态，
读模型由订阅事件自动更新，因此离线批次归并重放后视图天然一致。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from src.domain import PatientBoard, ResourceLedger
from src.events import (AggregateType, Event, EventType, make_event)
from src.review import ReviewService
from src.scheduling import Scheduler
from src.store import EventStore


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


class MissionService:
    def __init__(self, store: EventStore | None = None,
                 *, turnaround_minutes: int = 20) -> None:
        self.store = store or EventStore()
        self.board = PatientBoard()
        self.ledger = ResourceLedger()
        # 重放已有事件，保证从持久仓库启动时视图一致。
        for event in self.store.all_events():
            self.board.handle(event)
            self.ledger.handle(event)
        self.store.subscribe(self._on_event)
        self.scheduler = Scheduler(self.store, self.board, self.ledger,
                                   turnaround_minutes=turnaround_minutes)
        self.reviews = ReviewService(self.store, self.board)

    def _on_event(self, event: Event) -> None:
        self.board.handle(event)
        self.ledger.handle(event)

    # ---- 离线归并 -----------------------------------------------------------

    def import_offline_batch(self, events: list[Event]) -> list[Event]:
        """把离线采集的事件批次按仓库契约归并（去重、续版本、重放读模型）。"""
        return self.store.merge_batch(events)

    # ---- 患者与身份 ---------------------------------------------------------

    def register_patient(self, *, local_patient_id: str, display_name: str,
                         name_variants: list[str] | None = None,
                         preferred_language: str | None = None,
                         source: str = "paper_list",
                         registered_by: str,
                         notes: str | None = None,
                         case_id: str | None = None,
                         event_id: str | None = None,
                         request_id: str | None = None,
                         captured_at: str | None = None) -> Event:
        case_id = case_id or f"case-{uuid4().hex[:10]}"
        payload: dict[str, Any] = {
            "local_patient_id": local_patient_id, "display_name": display_name,
            "name_variants": name_variants or [], "source": source,
            "registered_by": registered_by,
        }
        if preferred_language:
            payload["preferred_language"] = preferred_language
        if notes:
            payload["notes"] = notes
        return self.store.append(make_event(
            EventType.PATIENT_REGISTERED, AggregateType.PATIENT_CASE,
            case_id, 1, f"登记患者：{display_name}（临时身份 {local_patient_id}）",
            payload, event_id=event_id, request_id=request_id,
            source="offline" if captured_at else "online",
            captured_at=captured_at), expected_version=0)

    def merge_identities(self, *, kept_case_id: str, merged_case_id: str,
                         reason: str, merged_by: str,
                         merge_id: str | None = None) -> Event:
        """合并译名差异或多次筛查形成的重复身份；合并本身可撤销。"""
        if kept_case_id == merged_case_id:
            raise ValueError("不能将病例与自身合并")
        self.board.get(kept_case_id)
        self.board.get(merged_case_id)
        merge_id = merge_id or _new_id("merge")
        return self.store.append(make_event(
            EventType.IDENTITY_MERGED, AggregateType.IDENTITY_MERGE, merge_id, 1,
            f"身份合并：{merged_case_id} 并入 {kept_case_id}",
            {"kept_case_id": kept_case_id, "merged_case_id": merged_case_id,
             "reason": reason, "merged_by": merged_by}), expected_version=0)

    def revert_merge(self, *, merge_event_id: str, reason: str,
                     reverted_by: str) -> Event:
        return self.store.append(make_event(
            EventType.IDENTITY_MERGE_REVERTED, AggregateType.IDENTITY_MERGE,
            merge_event_id, self.store.stream_version(merge_event_id) + 1,
            f"撤销身份合并 {merge_event_id}",
            {"merge_event_id": merge_event_id, "reason": reason,
             "reverted_by": reverted_by}))

    # ---- 筛查与医生签署 -----------------------------------------------------

    def record_screening(self, *, case_id: str, recorded_by: str,
                         device_id: str | None = None,
                         algorithm_name: str | None = None,
                         algorithm_version: str | None = None,
                         findings: Any = None, ai_suggestion: str | None = None,
                         is_ai_result: bool = False,
                         source_document: str | None = None,
                         measured_at: str | None = None,
                         request_id: str | None = None,
                         captured_at: str | None = None) -> Event:
        """录入纸质名单或便携设备结果。AI 输出必须显式标注，仅供医生参考。"""
        case_id = self.board.canonical(case_id)
        screening_id = _new_id("screening")
        payload: dict[str, Any] = {"case_id": case_id, "recorded_by": recorded_by}
        for key, value in (("device_id", device_id), ("algorithm_name", algorithm_name),
                           ("algorithm_version", algorithm_version),
                           ("findings", findings), ("ai_suggestion", ai_suggestion),
                           ("is_ai_result", is_ai_result),
                           ("source_document", source_document),
                           ("measured_at", measured_at)):
            if value is not None:
                payload[key] = value
        return self.store.append(make_event(
            EventType.SCREENING_RECORDED, AggregateType.SCREENING_RESULT,
            screening_id, 1,
            f"录入{'AI 设备' if is_ai_result else '筛查'}结果：病例 {case_id}",
            payload, request_id=request_id,
            source="offline" if captured_at else "online",
            captured_at=captured_at), expected_version=0)

    def review_screening(self, *, case_id: str, reviewer: str, license_id: str,
                         clinical_decision: str,
                         eligible_for_surgery: bool | None = None,
                         diagnosis_codes: list[str] | None = None,
                         findings: Any = None, notes: str | None = None,
                         screening_event_ids: list[str] | None = None) -> Event:
        """有资质医生复核签署：诊断与手术资格的唯一权威来源。"""
        case_id = self.board.canonical(case_id)
        payload: dict[str, Any] = {
            "case_id": case_id, "reviewer": reviewer, "license_id": license_id,
            "clinical_decision": clinical_decision,
        }
        if eligible_for_surgery is not None:
            payload["eligible_for_surgery"] = eligible_for_surgery
        if diagnosis_codes is not None:
            payload["diagnosis_codes"] = diagnosis_codes
        if findings is not None:
            payload["findings"] = findings
        if notes:
            payload["notes"] = notes
        if screening_event_ids:
            payload["screening_event_ids"] = screening_event_ids
        return self.store.append(make_event(
            EventType.SCREENING_REVIEWED, AggregateType.PATIENT_CASE, case_id,
            self.store.stream_version(case_id) + 1,
            f"医生 {reviewer}（{license_id}）复核签署：病例 {case_id}",
            payload))

    # ---- 同意与优先级 -------------------------------------------------------

    def sign_consent(self, *, case_id: str, language: str, consent_version: str,
                     signed_by: str, interpreter_present: bool = False,
                     interpreter_language: str | None = None,
                     procedure_scope: str | None = None,
                     witness: str | None = None,
                     signed_at: str | None = None) -> Event:
        case_id = self.board.canonical(case_id)
        payload: dict[str, Any] = {
            "case_id": case_id, "language": language,
            "consent_version": consent_version, "signed_by": signed_by,
            "interpreter_present": interpreter_present,
        }
        for key, value in (("interpreter_language", interpreter_language),
                           ("procedure_scope", procedure_scope),
                           ("witness", witness), ("signed_at", signed_at)):
            if value is not None:
                payload[key] = value
        return self.store.append(make_event(
            EventType.CONSENT_SIGNED, AggregateType.CONSENT,
            f"consent-{case_id}", self.store.stream_version(f"consent-{case_id}") + 1,
            f"知情同意签署（{language}）：病例 {case_id}", payload))

    def set_priority(self, *, case_id: str, priority_level: int, reason: str,
                     set_by: str, category: str | None = None,
                     expires_at: str | None = None) -> Event:
        """每次临床优先级变化都独立留痕，供任务结束后复盘解释。"""
        if priority_level not in (1, 2, 3, 4):
            raise ValueError("priority_level 必须为 1（最高）~4（最低）")
        case_id = self.board.canonical(case_id)
        payload: dict[str, Any] = {
            "case_id": case_id, "priority_level": priority_level,
            "reason": reason, "set_by": set_by,
        }
        for key, value in (("category", category), ("expires_at", expires_at)):
            if value is not None:
                payload[key] = value
        return self.store.append(make_event(
            EventType.PRIORITY_SET, AggregateType.PATIENT_CASE, case_id,
            self.store.stream_version(case_id) + 1,
            f"优先级调整为 {priority_level}：病例 {case_id}（{reason}）", payload))

    # ---- 资源与团队 ---------------------------------------------------------

    def update_rooms(self, *, facility_id: str, rooms: list[dict[str, str]],
                     updated_by: str, note: str | None = None) -> Event:
        return self._ledger_update(facility_id, "room",
                                   {"rooms": rooms, "updated_by": updated_by,
                                    **({"note": note} if note else {})})

    def update_supplies(self, *, facility_id: str, items: dict[str, int],
                        updated_by: str, note: str | None = None) -> Event:
        return self._ledger_update(facility_id, "supply",
                                   {"items": items, "updated_by": updated_by,
                                    **({"note": note} if note else {})})

    def update_followup_capacity(self, *, facility_id: str,
                                 capacity_by_date: dict[str, int],
                                 updated_by: str) -> Event:
        return self._ledger_update(facility_id, "followup_capacity",
                                   {"capacity_by_date": capacity_by_date,
                                    "updated_by": updated_by})

    def _ledger_update(self, facility_id: str, kind: str,
                       extra: dict[str, Any]) -> Event:
        stream = f"ledger-{facility_id}-{kind}"
        payload = {"ledger_kind": kind, "facility_id": facility_id, **extra}
        return self.store.append(make_event(
            EventType.RESOURCE_LEDGER_UPDATED, AggregateType.RESOURCE_LEDGER,
            stream, self.store.stream_version(stream) + 1,
            f"资源盘点（{kind}）：{facility_id}", payload))

    def declare_shift(self, *, staff_id: str, role: str, shift_start: str,
                      shift_end: str, max_cases: int | None = None,
                      max_duty_minutes: int | None = None,
                      facility_id: str | None = None) -> Event:
        payload: dict[str, Any] = {
            "staff_id": staff_id, "role": role,
            "shift_start": shift_start, "shift_end": shift_end,
        }
        for key, value in (("max_cases", max_cases),
                           ("max_duty_minutes", max_duty_minutes),
                           ("facility_id", facility_id)):
            if value is not None:
                payload[key] = value
        stream = f"shift-{staff_id}"
        return self.store.append(make_event(
            EventType.TEAM_SHIFT_DECLARED, AggregateType.TEAM_ROSTER, stream,
            self.store.stream_version(stream) + 1,
            f"班次声明：{staff_id}（{role}）{shift_start}~{shift_end}", payload))

    # ---- 治疗与接续 ---------------------------------------------------------

    def plan_treatment(self, *, case_id: str, procedure: str, planned_by: str,
                       reservation_id: str | None = None,
                       notes: str | None = None) -> Event:
        """术式计划（计划本身不占用房间/耗材，占用以 SLOT_RESERVED 为准）。"""
        case_id = self.board.canonical(case_id)
        payload: dict[str, Any] = {
            "case_id": case_id, "procedure": procedure, "planned_by": planned_by}
        for key, value in (("reservation_id", reservation_id), ("notes", notes)):
            if value is not None:
                payload[key] = value
        return self.store.append(make_event(
            EventType.TREATMENT_PLANNED, AggregateType.PATIENT_CASE, case_id,
            self.store.stream_version(case_id) + 1,
            f"术式计划：病例 {case_id}（{procedure}）", payload))

    def complete_treatment(self, *, case_id: str, procedure: str,
                           surgeon_id: str, license_id: str, completed_at: str,
                           reservation_id: str | None = None,
                           supplies_consumed: dict[str, int] | None = None,
                           notes: str | None = None) -> Event:
        case_id = self.board.canonical(case_id)
        stream = reservation_id or f"treatment-{case_id}-{uuid4().hex[:8]}"
        payload: dict[str, Any] = {
            "case_id": case_id, "procedure": procedure,
            "surgeon_id": surgeon_id, "license_id": license_id,
            "completed_at": completed_at,
        }
        for key, value in (("reservation_id", reservation_id),
                           ("supplies_consumed", supplies_consumed),
                           ("notes", notes)):
            if value is not None:
                payload[key] = value
        return self.store.append(make_event(
            EventType.TREATMENT_COMPLETED, AggregateType.TREATMENT_SLOT, stream,
            self.store.stream_version(stream) + 1,
            f"治疗完成：病例 {case_id}（{procedure}，主刀 {surgeon_id}）",
            payload))

    def accept_handoff(self, *, case_id: str, facility_id: str,
                       receiving_clinician: str, followup_plan: str,
                       training_handover: str | None = None,
                       contacts: dict[str, str] | None = None,
                       accepted_at: str | None = None,
                       reservation_id: str | None = None) -> Event:
        """当地医院接续：任务结束后随访与培训交接由此闭环。"""
        case_id = self.board.canonical(case_id)
        stream = f"handoff-{case_id}"
        payload: dict[str, Any] = {
            "case_id": case_id, "facility_id": facility_id,
            "receiving_clinician": receiving_clinician,
            "followup_plan": followup_plan,
        }
        for key, value in (("training_handover", training_handover),
                           ("contacts", contacts), ("accepted_at", accepted_at),
                           ("reservation_id", reservation_id)):
            if value is not None:
                payload[key] = value
        return self.store.append(make_event(
            EventType.HANDOFF_ACCEPTED, AggregateType.FOLLOWUP_HANDOFF, stream,
            self.store.stream_version(stream) + 1,
            f"当地医院 {facility_id} 接续病例 {case_id}", payload))

    def schedule_followup(self, *, case_id: str, facility_id: str,
                          followup_date: str, clinician: str | None = None,
                          note: str | None = None) -> Event:
        case_id = self.board.canonical(case_id)
        stream = f"handoff-{case_id}"
        payload: dict[str, Any] = {"case_id": case_id, "facility_id": facility_id,
                                   "followup_date": followup_date}
        for key, value in (("clinician", clinician), ("note", note)):
            if value is not None:
                payload[key] = value
        return self.store.append(make_event(
            EventType.FOLLOWUP_SCHEDULED, AggregateType.FOLLOWUP_HANDOFF, stream,
            self.store.stream_version(stream) + 1,
            f"随访安排：病例 {case_id} 于 {followup_date} 到 {facility_id}",
            payload))
