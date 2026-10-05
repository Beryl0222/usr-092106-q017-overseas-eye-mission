"""排程策略：房间、耗材、团队疲劳与随访容量的统一校验。

规则（对常规预约与紧急加台同样生效）：
1. 手术资格：必须已有医生复核签署确认 eligible_for_surgery 且持执照号，
   并已签知情同意；AI 建议不构成资格。
2. 房间：必须在已登记的可用时段内，且不与现有排期时间重叠。
3. 无菌耗材：每个耗材代码必须已登记，且“余量 = 盘点量 - 已占用量”
   足够覆盖本次需求。
4. 随访：术后随访日期在当地医院的接续容量之内。
5. 疲劳：主刀必须有覆盖该时段的班次声明，台次、在岗时长与台间隔
   休息都不得超限。

紧急加台额外规则：
- 必须把上面 2~5 项连同当时的资源占用重新校验一遍；
- 不得挤掉更高风险（优先级数字更小）或同风险的在排患者；只有比加台
  患者风险更低、且被协调员显式列入顺延名单的排期可以让出名额；
- 被拒加台不产生任何 SLOT_RESERVED，不消耗任何资源；
- 事件仓库的 event_id / request_id 幂等保证重传不会二次占用。
"""

from __future__ import annotations

from datetime import timedelta

from src.domain import PatientBoard, ResourceLedger, contains, overlaps, parse_dt
from src.events import (AggregateType, Event, EventType, make_event)
from src.store import EventStore


class SchedulingError(Exception):
    """排程参数本身有误（区别于规则不通过的 violations）。"""


class Scheduler:
    def __init__(self, store: EventStore, board: PatientBoard,
                 ledger: ResourceLedger, *, turnaround_minutes: int = 20) -> None:
        self.store = store
        self.board = board
        self.ledger = ledger
        self.turnaround = timedelta(minutes=turnaround_minutes)

    # ---- 规则评估 -----------------------------------------------------------

    def evaluate(self, case_id: str, *, room_id: str, start: str, end: str,
                 surgeon_id: str, supplies: dict[str, int],
                 followup_date: str,
                 exclude_reservations: set[str] | None = None) -> list[str]:
        """返回违反规则的说明；空列表表示可以排程。

        exclude_reservations 中的排期视为已顺延（紧急加台重算余量时使用）。
        """
        excluded = exclude_reservations or set()
        violations: list[str] = []

        case = self.board.get(case_id)
        if not case.surgery_eligible:
            violations.append("患者尚无有资质医生签署的手术资格确认（AI 结果不可替代）")
        if case.consent is None:
            violations.append("患者尚未签署知情同意")

        # 房间：登记、开放时段、占用冲突
        window = self.ledger.rooms.get(room_id)
        if window is None:
            violations.append(f"房间 {room_id} 未登记可用时段")
        else:
            if not contains(window[0], window[1], start, end):
                violations.append(f"房间 {room_id} 在 {start}~{end} 不可用")
            clash = [r for r in self.ledger.room_busy(room_id, start, end)
                     if r.reservation_id not in excluded]
            if clash:
                violations.append(
                    f"房间 {room_id} 与排期 {','.join(c.reservation_id for c in clash)} 时间冲突")

        # 无菌耗材（顺延排期会释放其占用）
        available = self._available_supplies(excluded)
        for code, qty in supplies.items():
            if qty <= 0:
                violations.append(f"耗材 {code} 需求量必须为正数")
                continue
            if code not in self.ledger.supplies:
                violations.append(f"耗材 {code} 未登记，无法确认无菌余量")
            elif available.get(code, 0) < qty:
                violations.append(
                    f"无菌耗材 {code} 余量不足：需要 {qty}，可支配 {available.get(code, 0)}")

        # 当地随访容量（顺延排期同时释放其随访名额）
        capacity = self.ledger.followup_capacity.get(followup_date)
        used = self._followup_used(followup_date, excluded)
        if capacity is None:
            violations.append(f"随访日期 {followup_date} 未登记当地接续容量")
        elif used + 1 > capacity:
            violations.append(
                f"随访日期 {followup_date} 当地容量不足：已排 {used}/{capacity}")

        # 团队疲劳
        violations.extend(
            self._fatigue_violations(surgeon_id, start, end, excluded))
        return violations

    def _available_supplies(self, excluded: set[str]) -> dict[str, int]:
        used: dict[str, int] = {}
        for r in self.ledger.reservations.values():
            if r.reservation_id in excluded:
                continue
            for code, qty in r.supplies.items():
                used[code] = used.get(code, 0) + qty
        return {code: total - used.get(code, 0)
                for code, total in self.ledger.supplies.items()}

    def _followup_used(self, date: str, excluded: set[str]) -> int:
        return sum(1 for r in self.ledger.reservations.values()
                   if r.followup_date == date and r.reservation_id not in excluded)

    def _fatigue_violations(self, surgeon_id: str, start: str, end: str,
                            excluded: set[str]) -> list[str]:
        violations: list[str] = []
        shifts = self.ledger.shifts.get(surgeon_id, [])
        s, e = parse_dt(start), parse_dt(end)
        shift = next((sh for sh in shifts
                      if parse_dt(sh["start"]) <= s and e <= parse_dt(sh["end"])), None)
        if shift is None:
            violations.append(f"主刀 {surgeon_id} 没有覆盖 {start}~{end} 的班次声明")
            return violations

        existing = [r for r in self.ledger.surgeon_reservations(surgeon_id)
                    if r.reservation_id not in excluded]
        in_shift = [r for r in existing
                    if parse_dt(r.start) >= parse_dt(shift["start"])
                    and parse_dt(r.end) <= parse_dt(shift["end"])]

        max_cases = shift.get("max_cases")
        if max_cases is not None and len(in_shift) + 1 > max_cases:
            violations.append(
                f"主刀 {surgeon_id} 班内台次超限：已排 {len(in_shift)}，上限 {max_cases}")

        # 台间隔休息（无论是否在同一班次，相邻台都要留周转时间）
        gap = self.turnaround
        for r in existing:
            re_, rs = parse_dt(r.end), parse_dt(r.start)
            if re_ <= s and s - re_ < gap:
                violations.append(
                    f"主刀 {surgeon_id} 与上一台 {r.reservation_id} 间隔不足 {gap}")
            if rs >= e and rs - e < gap:
                violations.append(
                    f"主刀 {surgeon_id} 与下一台 {r.reservation_id} 间隔不足 {gap}")

        max_duty = shift.get("max_duty_minutes")
        if max_duty is not None:
            duty_start = min([s] + [parse_dt(r.start) for r in in_shift])
            duty_end = max([e] + [parse_dt(r.end) for r in in_shift])
            duty_minutes = (duty_end - duty_start).total_seconds() / 60
            if duty_minutes > max_duty:
                violations.append(
                    f"主刀 {surgeon_id} 在岗时长超限：{duty_minutes:.0f} 分钟，上限 {max_duty}")
        return violations

    # ---- 常规预约 -----------------------------------------------------------

    def reserve(self, *, reservation_id: str, case_id: str, procedure: str,
                room_id: str, start: str, end: str, surgeon_id: str,
                supplies: dict[str, int], followup_date: str,
                request_id: str | None = None) -> Event:
        case_id = self.board.canonical(case_id)
        violations = self.evaluate(
            case_id, room_id=room_id, start=start, end=end,
            surgeon_id=surgeon_id, supplies=supplies,
            followup_date=followup_date)
        if violations:
            raise SchedulingError("排程被拒绝：" + "；".join(violations))

        event = make_event(
            EventType.SLOT_RESERVED, AggregateType.TREATMENT_SLOT,
            reservation_id, 1, f"为病例 {case_id} 预留手术台：{procedure}",
            {"reservation_id": reservation_id, "case_id": case_id,
             "procedure": procedure, "room_id": room_id, "start": start,
             "end": end, "surgeon_id": surgeon_id, "supplies": dict(supplies),
             "followup_date": followup_date, "kind": "scheduled"},
            request_id=request_id)
        return self.store.append(event, expected_version=0)

    # ---- 紧急加台 -----------------------------------------------------------

    def request_urgent_addon(self, *, addon_request_id: str, case_id: str,
                             procedure: str, room_id: str, start: str, end: str,
                             surgeon_id: str, supplies: dict[str, int],
                             followup_date: str, requested_by: str,
                             displace_reservation_ids: list[str] | None = None,
                             request_id: str | None = None,
                             note: str | None = None) -> dict[str, object]:
        """处理紧急加台：登记请求 → 保护规则 → 顺延低风险台 → 重校验。

        只有全部通过才追加 SLOT_RESERVED 与 URGENT_ADDON_DECIDED(approved)；
        任何一步失败只写拒绝决定，不释放/占用任何资源。
        返回 {"approved", "reasons", "reservation_id", "replayed"?}。
        """
        case_id = self.board.canonical(case_id)

        # 幂等重传：请求流已有决定则原样回放，不再消耗名额。
        existing = self.store.stream_events(addon_request_id)
        decided = next((e for e in existing
                        if e.event_type == EventType.URGENT_ADDON_DECIDED), None)
        if decided is not None:
            return {"approved": decided.payload["approved"],
                    "reasons": decided.payload.get("rejection_reasons", []),
                    "reservation_id": decided.payload.get("reservation_id"),
                    "replayed": True}

        declared = set(displace_reservation_ids or [])
        request_payload = {
            "addon_request_id": addon_request_id, "case_id": case_id,
            "procedure": procedure, "room_id": room_id,
            "requested_start": start, "requested_end": end,
            "surgeon_id": surgeon_id, "supplies": dict(supplies),
            "followup_date": followup_date, "requested_by": requested_by,
        }
        if declared:
            request_payload["displace_reservation_ids"] = sorted(declared)
        if note:
            request_payload["note"] = note
        if not existing:
            self.store.append(make_event(
                EventType.URGENT_ADDON_REQUESTED, AggregateType.TREATMENT_SLOT,
                addon_request_id, 1,
                f"紧急加台请求：病例 {case_id}（{procedure}）",
                request_payload, request_id=request_id),
                expected_version=0)

        addon_case = self.board.get(case_id)
        addon_level = addon_case.priority.level if addon_case.priority else None

        def victim_level_of(view) -> int | None:
            victim = self.board.get(view.case_id)
            return victim.priority.level if victim.priority else None

        # 第一步：保护规则——显式顺延名单的合法性，以及资源冲突中受影响患者的风险。
        violations: list[str] = []
        valid_displacements: set[str] = set()
        for rid in sorted(declared):
            view = self.ledger.reservations.get(rid)
            if view is None:
                violations.append(f"拟顺延排期 {rid} 不存在或已结束")
                continue
            victim_level = victim_level_of(view)
            if not self._is_displaceable(victim_level, addon_level):
                violations.append(
                    f"不得为加台挤掉风险不更低的在排患者：{rid}"
                    f"（在排优先级 {victim_level or '未评级'}，加台 {addon_level or '未评级'}）")
                continue
            valid_displacements.add(rid)

        # 未被列入顺延、却会与加台在房间/主刀上冲突的排期，同样属于“被挤掉”，
        # 必须风险更低才行（耗材与随访容量冲突在重校验阶段统一报告）。
        if addon_level is not None:
            for view in self.ledger.active_reservations():
                if view.reservation_id in declared:
                    continue
                clashes = (
                    view.room_id == room_id
                    and overlaps(view.start, view.end, start, end)
                ) or (
                    view.surgeon_id == surgeon_id
                    and overlaps(view.start, view.end, start, end)
                )
                if clashes and not self._is_displaceable(
                        victim_level_of(view), addon_level):
                    violations.append(
                        f"加台与更高/同等风险在排患者冲突且未安排顺延：{view.reservation_id}"
                        f"（在排优先级 {victim_level_of(view) or '未评级'}，加台 {addon_level}）")
        else:
            violations.append("紧急加台患者尚未评定临床优先级，无法比较风险")

        # 第二步：在“合法顺延已释放”的假设下重算全部资源与疲劳。
        if not violations:
            violations = self.evaluate(
                case_id, room_id=room_id, start=start, end=end,
                surgeon_id=surgeon_id, supplies=supplies,
                followup_date=followup_date,
                exclude_reservations=valid_displacements)

        # 第三步：落决定。拒绝时不产生任何资源变更。
        if violations:
            self.store.append(make_event(
                EventType.URGENT_ADDON_DECIDED, AggregateType.TREATMENT_SLOT,
                addon_request_id, self.store.stream_version(addon_request_id) + 1,
                f"紧急加台 {addon_request_id} 被拒绝",
                {"addon_request_id": addon_request_id, "approved": False,
                 "decided_by": requested_by, "rejection_reasons": violations}))
            return {"approved": False, "reasons": violations,
                    "reservation_id": None}

        reservation_id = f"slot-addon-{addon_request_id}"
        for rid in sorted(valid_displacements):
            self.store.append(make_event(
                EventType.SLOT_RELEASED, AggregateType.TREATMENT_SLOT,
                rid, self.store.stream_version(rid) + 1,
                f"为紧急加台 {addon_request_id} 顺延低风险排期 {rid}",
                {"reservation_id": rid,
                 "reason": f"紧急加台顺延（请求 {addon_request_id}）",
                 "released_by": requested_by}))

        self.store.append(make_event(
            EventType.SLOT_RESERVED, AggregateType.TREATMENT_SLOT,
            reservation_id, 1,
            f"紧急加台预留：病例 {case_id}（{procedure}）",
            {"reservation_id": reservation_id, "case_id": case_id,
             "procedure": procedure, "room_id": room_id, "start": start,
             "end": end, "surgeon_id": surgeon_id, "supplies": dict(supplies),
             "followup_date": followup_date, "kind": "urgent_addon",
             "addon_request_id": addon_request_id}))
        self.store.append(make_event(
            EventType.URGENT_ADDON_DECIDED, AggregateType.TREATMENT_SLOT,
            addon_request_id, self.store.stream_version(addon_request_id) + 1,
            f"紧急加台 {addon_request_id} 已批准（排期 {reservation_id}）",
            {"addon_request_id": addon_request_id, "approved": True,
             "decided_by": requested_by, "reservation_id": reservation_id}))
        return {"approved": True, "reasons": [],
                "reservation_id": reservation_id,
                "displaced": sorted(valid_displacements)}

    @staticmethod
    def _is_displaceable(victim_level: int | None,
                         addon_level: int | None) -> bool:
        """只有明确风险更低（优先级数字更大）的在排患者可被顺延。

        任一方未评级，或优先级相同，均按不可挤压处理。
        """
        if victim_level is None or addon_level is None:
            return False
        return victim_level > addon_level
