import unittest

from src.app import MissionService
from src.events import (AggregateType, EventType, make_event)
from tests.helpers import build_mission, onboard, reserve, ts


class OfflineMergeEndToEndTest(unittest.TestCase):
    def test_offline_batch_rebuilds_same_board_and_retransmit_is_safe(self):
        # 现场在线侧已有一条登记
        svc = build_mission()
        onboard(svc, case_id="c1", name="Ana", priority=3)
        before_count = len(svc.store.all_events())

        # 便携设备离线采集：登记新病例 + AI 筛查（事件乱序、本地版本号任意）
        offline = [
            make_event(
                EventType.SCREENING_RECORDED, AggregateType.SCREENING_RESULT,
                "screening-offline-2", 7, "设备离线筛查：c2",
                {"case_id": "c2", "recorded_by": "tech-02",
                 "device_id": "SCREEN-CAM-9", "algorithm_name": "cataract-detect",
                 "algorithm_version": "3.2.1", "is_ai_result": True,
                 "ai_suggestion": "建议复核"},
                event_id="off-evt-2", request_id="dev-req-2",
                source="offline", captured_at="2026-10-05T09:20:00+12:00"),
            make_event(
                EventType.PATIENT_REGISTERED, AggregateType.PATIENT_CASE,
                "c2", 3, "登记患者：Bera（TMP-2）",
                {"local_patient_id": "TMP-2", "display_name": "Bera",
                 "registered_by": "tech-02"},
                event_id="off-evt-1", request_id="dev-req-1",
                source="offline", captured_at="2026-10-05T09:10:00+12:00"),
        ]

        # 网络抖动：同一批被仓库接收两次
        first = svc.import_offline_batch(offline)
        second = svc.import_offline_batch(offline)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)  # 全部识别为已有事件
        self.assertEqual(len(svc.store.all_events()), before_count + 2)

        # 乱序归并后，c2 的登记与筛查都在视图里，但 AI 结果不构成资格
        case2 = svc.board.get("c2")
        self.assertEqual(case2.display_name, "Bera")
        self.assertEqual(len(case2.screenings), 1)
        self.assertFalse(case2.surgery_eligible)

    def test_restart_from_event_log_rebuilds_identical_readmodels(self):
        svc = build_mission()
        onboard(svc, case_id="c1", name="Ana", priority=1)
        reserve(svc, case_id="c1", reservation_id="slot-1", start_h=9)
        log = [e.to_dict() for e in svc.store.all_events()]

        # 新实例从事件日志启动（模拟收队后重建）
        from src.events import Event
        from src.store import EventStore
        rebuilt_store = EventStore()
        rebuilt_store.merge_batch([Event.from_dict(d) for d in log])
        rebuilt = MissionService(rebuilt_store)
        self.assertEqual(
            [r["case_id"] for r in rebuilt.board.worklist()],
            [r["case_id"] for r in svc.board.worklist()])
        self.assertEqual(rebuilt.ledger.available_supplies(),
                         svc.ledger.available_supplies())
        self.assertTrue(rebuilt.board.get("c1").surgery_eligible)
        self.assertEqual(
            rebuilt.ledger.surgeon_reservations("DR-WANG")[0].reservation_id,
            "slot-1")


if __name__ == "__main__":
    unittest.main()
