import unittest
from datetime import datetime, timedelta

from src.app import MissionService
from src.scheduling import SchedulingError
from tests.helpers import (DAY, FOLLOWUP_DATE, ROOM, SURGEON, TZ, build_mission,
                           onboard, reserve, ts)


def addon(svc: MissionService, *, case_id: str, start_h: int, start_m: int = 0,
          duration: int = 45, displace=None, request_id="addon-req-1",
          addon_request_id="urgent-1"):
    start = f"{DAY}T{start_h:02d}:{start_m:02d}:00{TZ}"
    end = (datetime.fromisoformat(start) + timedelta(minutes=duration)).isoformat()
    return svc.scheduler.request_urgent_addon(
        addon_request_id=addon_request_id, case_id=case_id,
        procedure="白内障超声乳化+IOL", room_id=ROOM, start=start, end=end,
        surgeon_id=SURGEON, supplies={"IOL-STD": 1, "STERILE-KIT": 1},
        followup_date=FOLLOWUP_DATE, requested_by="coord-li",
        displace_reservation_ids=displace, request_id=request_id)


class SchedulingTest(unittest.TestCase):
    def test_reserve_happy_path_consumes_supplies(self) -> None:
        svc = build_mission()
        onboard(svc, case_id="c1", name="Ana", priority=3)
        reserve(svc, case_id="c1", reservation_id="slot-1", start_h=9)
        self.assertEqual(svc.ledger.available_supplies()["STERILE-KIT"], 4)

    def test_reserve_without_physician_signoff_rejected(self) -> None:
        svc = build_mission()
        svc.register_patient(case_id="c1", local_patient_id="T",
                             display_name="Ana", registered_by="x")
        with self.assertRaises(SchedulingError) as ctx:
            reserve(svc, case_id="c1", reservation_id="slot-1", start_h=9)
        self.assertIn("医生签署", str(ctx.exception))

    def test_sterile_supply_shortage_blocks(self) -> None:
        svc = build_mission(supplies={"IOL-STD": 1, "STERILE-KIT": 1})
        onboard(svc, case_id="c1", name="Ana", priority=3)
        reserve(svc, case_id="c1", reservation_id="slot-1", start_h=9)
        onboard(svc, case_id="c2", name="Bera", priority=3)
        with self.assertRaises(SchedulingError) as ctx:
            reserve(svc, case_id="c2", reservation_id="slot-2", start_h=10, start_m=5)
        self.assertIn("STERILE-KIT 余量不足", str(ctx.exception))

    def test_followup_capacity_blocks(self) -> None:
        svc = build_mission(followup_capacity=2)
        for i in range(2):
            cid = f"c{i}"
            onboard(svc, case_id=cid, name=f"P{i}", priority=3)
            reserve(svc, case_id=cid, reservation_id=f"slot-{i}",
                    start_h=9 + i, start_m=5 * i)
        onboard(svc, case_id="cx", name="Px", priority=3)
        with self.assertRaises(SchedulingError) as ctx:
            reserve(svc, case_id="cx", reservation_id="slot-x",
                    start_h=12, start_m=30)
        self.assertIn("当地容量不足", str(ctx.exception))

    def test_turnaround_and_shift_fatigue_enforced(self) -> None:
        svc = MissionService()  # 自定义：班内最多 2 台
        svc.update_rooms(facility_id="F", updated_by="l",
                         rooms=[{"room_id": ROOM,
                                 "available_from": ts(7), "available_to": ts(19)}])
        svc.update_supplies(facility_id="F", updated_by="l",
                            items={"IOL-STD": 9, "STERILE-KIT": 9})
        svc.update_followup_capacity(facility_id="F", updated_by="n",
                                     capacity_by_date={FOLLOWUP_DATE: 9})
        svc.declare_shift(staff_id=SURGEON, role="surgeon",
                          shift_start=ts(7), shift_end=ts(19),
                          max_cases=2, max_duty_minutes=600)
        onboard(svc, case_id="c1", name="A", priority=3)
        onboard(svc, case_id="c2", name="B", priority=3)
        onboard(svc, case_id="c3", name="C", priority=3)
        reserve(svc, case_id="c1", reservation_id="slot-1", start_h=9)
        # 间隔不足 20 分钟
        with self.assertRaises(SchedulingError) as ctx:
            reserve(svc, case_id="c2", reservation_id="slot-2",
                    start_h=10, start_m=0)  # 距上一台结束 9:45 仅 15 分钟
        self.assertIn("间隔不足", str(ctx.exception))
        reserve(svc, case_id="c2", reservation_id="slot-2",
                start_h=10, start_m=5)
        # 第三台触发班内台次上限
        with self.assertRaises(SchedulingError) as ctx:
            reserve(svc, case_id="c3", reservation_id="slot-3",
                    start_h=12, start_m=30)
        self.assertIn("台次超限", str(ctx.exception))

    def test_room_outside_window_rejected(self) -> None:
        svc = build_mission()
        onboard(svc, case_id="c1", name="A", priority=3)
        with self.assertRaises(SchedulingError) as ctx:
            reserve(svc, case_id="c1", reservation_id="slot-1",
                    start_h=20, duration=45)  # 房间 19:00 关闭
        self.assertIn("不可用", str(ctx.exception))


class UrgentAddonTest(unittest.TestCase):
    def _two_patients(self, urgent_level: int = 1):
        svc = build_mission()
        onboard(svc, case_id="urgent", name="Ura", priority=urgent_level)
        onboard(svc, case_id="high", name="Hia", priority=2)
        onboard(svc, case_id="low", name="Loa", priority=4)
        reserve(svc, case_id="high", reservation_id="slot-high", start_h=9)
        reserve(svc, case_id="low", reservation_id="slot-low",
                start_h=10, start_m=5)
        return svc

    def test_cannot_bump_higher_or_equal_risk_patient(self) -> None:
        svc = build_mission()
        onboard(svc, case_id="addon-case", name="Ada", priority=2)
        onboard(svc, case_id="high", name="Hia", priority=1)
        reserve(svc, case_id="high", reservation_id="slot-high", start_h=9)
        # 加台（2 级）直接撞上更高风险的 1 级在排台，未安排顺延 -> 拒绝
        result = addon(svc, case_id="addon-case", start_h=9)
        self.assertFalse(result["approved"])
        self.assertTrue(any("风险" in r for r in result["reasons"]))
        # 即使显式要求顺延高风险台，也被保护规则拒绝（新请求，避免回放旧决定）
        result = addon(svc, case_id="addon-case", start_h=9,
                       displace=["slot-high"], request_id="addon-req-2",
                       addon_request_id="urgent-2")
        self.assertFalse(result["approved"])
        self.assertTrue(any("slot-high" in r for r in result["reasons"]))
        # 高风险台完好无损
        self.assertIn("slot-high", svc.ledger.reservations)

    def test_rejected_addon_consumes_nothing_and_replays(self) -> None:
        svc = self._two_patients()
        available_before = dict(svc.ledger.available_supplies())
        result = addon(svc, case_id="urgent", start_h=9)
        self.assertFalse(result["approved"])
        # 无新增预留，耗材余量不变
        self.assertEqual(svc.ledger.available_supplies(), available_before)
        self.assertNotIn("slot-addon-urgent-1", svc.ledger.reservations)
        # 重传：回放原决定，不产生第二条决定、不占名额
        replay = addon(svc, case_id="urgent", start_h=9)
        self.assertFalse(replay["approved"])
        self.assertTrue(replay.get("replayed"))
        stream = svc.store.stream_events("urgent-1")
        self.assertEqual(len(stream), 2)  # 仅 REQUESTED + DECIDED 各一条
        self.assertEqual(svc.ledger.available_supplies(), available_before)

    def test_addon_displaces_only_lower_risk_and_rechecks(self) -> None:
        svc = self._two_patients()
        # 加台落在低风险台时段，显式顺延后通过
        result = addon(svc, case_id="urgent", start_h=10, start_m=5,
                       displace=["slot-low"])
        self.assertTrue(result["approved"], msg=str(result["reasons"]))
        self.assertEqual(result["displaced"], ["slot-low"])
        self.assertIn("slot-addon-urgent-1", svc.ledger.reservations)
        self.assertNotIn("slot-low", svc.ledger.reservations)  # 已顺延释放
        # 高风险台完好
        self.assertIn("slot-high", svc.ledger.reservations)

    def test_addon_revalidates_sterile_supplies_after_displacement(self) -> None:
        svc = build_mission(supplies={"IOL-STD": 2, "STERILE-KIT": 2})
        onboard(svc, case_id="urgent", name="Ura", priority=1)
        onboard(svc, case_id="lowA", name="La", priority=4)
        onboard(svc, case_id="lowB", name="Lb", priority=4)
        reserve(svc, case_id="lowA", reservation_id="slot-a", start_h=9)
        reserve(svc, case_id="lowB", reservation_id="slot-b",
                start_h=10, start_m=5)
        # 11:10 房间与主刀都空闲，但无菌包已被两台占完 -> 先拒
        result = addon(svc, case_id="urgent", start_h=11, start_m=10,
                       request_id="req-no-kit")
        self.assertFalse(result["approved"])
        self.assertTrue(any("STERILE-KIT" in r for r in result["reasons"]))
        # 顺延一台低风险后重新校验，耗材释放，批准（新请求，避免回放拒绝决定）
        result2 = addon(svc, case_id="urgent", start_h=11, start_m=10,
                        displace=["slot-b"], request_id="req-with-kit",
                        addon_request_id="urgent-2")
        self.assertTrue(result2["approved"], msg=str(result2["reasons"]))

    def test_equal_risk_cannot_be_displaced(self) -> None:
        svc = build_mission()
        onboard(svc, case_id="addon-case", name="Ad", priority=2)
        onboard(svc, case_id="peer", name="Pe", priority=2)
        reserve(svc, case_id="peer", reservation_id="slot-peer", start_h=9)
        result = addon(svc, case_id="addon-case", start_h=9,
                       displace=["slot-peer"], request_id="req-eq")
        self.assertFalse(result["approved"])
        self.assertTrue(any("风险不更低" in r for r in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
