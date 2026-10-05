"""获准复盘导出。

- 导出必须先有 REVIEW_EXPORT_AUTHORIZED 授权事件：授权人、用途、范围
  （全部 / 指定病例）与有效期；没有有效授权时拒绝导出。
- 复盘资料逐病例还原关键事实，并对“每一次临床优先级变化”给出
  何时、何人、从几级到几级、依据什么理由，满足任务结束后的审计解释要求。
- 导出内容不做匿名化之外的扩散：调用方只拿到完成复盘所必需的字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.domain import PRIORITY_LEVELS, PatientBoard, parse_dt
from src.events import AggregateType, Event, EventType, make_event, utcnow
from src.store import EventStore
from uuid import uuid4


class ReviewAuthorizationError(PermissionError):
    """缺少有效复盘授权或授权范围/期限不覆盖本次导出。"""


@dataclass
class _Authorization:
    authorization_id: str
    authorized_by: str
    scope: str                 # all / cases
    purpose: str | None
    case_ids: set[str]
    granted_at: str
    expires_at: str | None
    revoked: bool = False


class ReviewService:
    def __init__(self, store: EventStore, board: PatientBoard) -> None:
        self.store = store
        self.board = board

    def authorize_export(self, *, authorized_by: str, scope: str,
                         purpose: str | None = None,
                         case_ids: list[str] | None = None,
                         expires_at: str | None = None,
                         authorization_id: str | None = None,
                         granted_at: str | None = None) -> Event:
        if scope not in ("all", "cases"):
            raise ValueError("scope 必须是 all 或 cases")
        if scope == "cases" and not case_ids:
            raise ValueError("scope=cases 时必须提供 case_ids")
        authorization_id = authorization_id or f"reviewauth-{uuid4().hex[:10]}"
        payload: dict[str, Any] = {
            "authorization_id": authorization_id,
            "authorized_by": authorized_by, "scope": scope,
            "granted_at": granted_at or utcnow(),
        }
        if purpose:
            payload["purpose"] = purpose
        if case_ids:
            payload["case_ids"] = sorted(case_ids)
        if expires_at:
            payload["expires_at"] = expires_at
        return self.store.append(make_event(
            EventType.REVIEW_EXPORT_AUTHORIZED, AggregateType.MISSION_REVIEW,
            "mission-review", self.store.stream_version("mission-review") + 1,
            f"复盘导出授权：{authorized_by}（范围 {scope}）", payload))

    def _active_authorizations(self, at: str) -> list[_Authorization]:
        auths: dict[str, _Authorization] = {}
        for event in self.store.stream_events("mission-review"):
            p = event.payload
            if event.event_type != EventType.REVIEW_EXPORT_AUTHORIZED:
                continue
            auth = _Authorization(
                authorization_id=p["authorization_id"],
                authorized_by=p["authorized_by"], scope=p["scope"],
                purpose=p.get("purpose"),
                case_ids=set(p.get("case_ids", [])),
                granted_at=p["granted_at"], expires_at=p.get("expires_at"))
            auths[auth.authorization_id] = auth
        result = []
        for auth in auths.values():
            if auth.expires_at and parse_dt(auth.expires_at) < parse_dt(at):
                continue
            result.append(auth)
        return result

    def export_case_review(self, case_id: str, *, at: str | None = None
                           ) -> dict[str, Any]:
        """导出单病例复盘；无覆盖该病例的有效授权时抛错。"""
        at = at or utcnow()
        canonical = self.board.canonical(case_id)
        self._ensure_authorized(canonical, at)
        return self._build_case_report(canonical)

    def export_review_dossier(self, *, at: str | None = None
                              ) -> dict[str, Any]:
        """导出授权范围内的完整复盘资料。"""
        at = at or utcnow()
        auths = self._active_authorizations(at)
        if not auths:
            raise ReviewAuthorizationError("没有有效的复盘导出授权")
        allowed: set[str] = set()
        for auth in auths:
            if auth.scope == "all":
                allowed = {c.case_id for c in self.board.cases()}
                break
            allowed.update(self.board.canonical(cid) for cid in auth.case_ids)

        roots = {c.case_id for c in self.board.cases()}
        cases = [self._build_case_report(cid)
                 for cid in sorted(allowed & roots)]
        return {
            "generated_at": at,
            "authorized_by": sorted({a.authorized_by for a in auths}),
            "case_count": len(cases),
            "cases": cases,
        }

    def _ensure_authorized(self, canonical: str, at: str) -> None:
        for auth in self._active_authorizations(at):
            if auth.scope == "all" or canonical in auth.case_ids:
                return
        raise ReviewAuthorizationError(
            f"病例 {canonical} 不在任何有效复盘授权范围内")

    # ---- 报告组装 -----------------------------------------------------------

    def _build_case_report(self, canonical: str) -> dict[str, Any]:
        case = self.board.get(canonical)

        priority_timeline: list[dict[str, Any]] = []
        previous = None
        for change in case.priority_history:
            priority_timeline.append({
                "at": change.at, "set_by": change.set_by,
                "from_level": previous,
                "from_label": PRIORITY_LEVELS.get(previous, "未评级")
                if previous else "未评级",
                "to_level": change.level,
                "to_label": change.level_label,
                "category": change.category,
                "reason": change.reason,
            })
            previous = change.level

        ai_screenings = [
            {"event_id": s["event_id"], "device_id": s["device_id"],
             "algorithm_name": s["algorithm_name"],
             "algorithm_version": s["algorithm_version"],
             "ai_suggestion": s["ai_suggestion"]}
            for s in case.screenings if s["is_ai_result"]
        ]
        physician_review = None
        if case.review:
            physician_review = {
                "reviewer": case.review["reviewer"],
                "license_id": case.review["license_id"],
                "clinical_decision": case.review["clinical_decision"],
                "eligible_for_surgery": case.review["eligible_for_surgery"],
                "diagnosis_codes": case.review["diagnosis_codes"],
                "signed_at": case.review["at"],
            }

        addon_decisions = [
            d for d in self._addon_events_for(canonical)
        ]

        return {
            "case_id": canonical,
            "identity_members": case.member_ids,
            "display_name": case.display_name,
            "name_variants": case.name_variants,
            "ai_assists": ai_screenings,
            "note": "AI 结果仅为辅助证据，诊断与手术资格以下方医生签署为准",
            "physician_review": physician_review,
            "consent": ({"language": case.consent["language"],
                         "consent_version": case.consent["consent_version"],
                         "interpreter_present":
                             case.consent["interpreter_present"],
                         "signed_at": case.consent["at"]}
                        if case.consent else None),
            "priority_timeline": priority_timeline,
            "final_priority": (
                {"level": case.priority.level, "label": case.priority.level_label}
                if case.priority else None),
            "scheduling": self._scheduling_report(canonical),
            "urgent_addon_decisions": addon_decisions,
            "treatment": case.treatment,
            "handoff": case.handoff,
            "followups": case.followups,
            "open_missing": [item["label"] for item in case.missing_checklist()],
        }

    def _addon_events_for(self, canonical: str) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = []
        for event in self.store.all_events():
            if event.event_type != EventType.URGENT_ADDON_DECIDED:
                continue
            req = next((e for e in self.store.stream_events(event.aggregate_id)
                        if e.event_type == EventType.URGENT_ADDON_REQUESTED), None)
            if req is None:
                continue
            if self.board.canonical(req.payload["case_id"]) != canonical:
                continue
            decisions.append({
                "addon_request_id": event.payload["addon_request_id"],
                "approved": event.payload["approved"],
                "rejection_reasons": event.payload.get("rejection_reasons", []),
                "reservation_id": event.payload.get("reservation_id"),
                "decided_by": event.payload["decided_by"],
                "at": event.occurred_at,
            })
        return decisions

    def _scheduling_report(self, canonical: str) -> dict[str, Any] | None:
        return self.board.get(canonical).reservation
