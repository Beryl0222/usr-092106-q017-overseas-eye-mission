import unittest

from tests.helpers import build_mission, onboard, reserve
from src.app import MissionService


class BoardTest(unittest.TestCase):
    def test_checklist_shows_four_gaps_and_closes(self) -> None:
        svc = build_mission()
        svc.register_patient(case_id="c1", local_patient_id="TMP-1",
                             display_name="Ana", registered_by="coord")
        row = next(r for r in svc.board.worklist() if r["case_id"] == "c1")
        codes = {item["code"] for item in row["missing"]}
        self.assertEqual(
            codes,
            {"screening_review", "informed_consent",
             "surgery_resource", "postop_destination"})

        onboard(svc, case_id="c2", name="Bera", priority=3)
        reserve(svc, case_id="c2", reservation_id="slot-1", start_h=9)
        svc.accept_handoff(case_id="c2", facility_id="FJ-SUVA-EYE",
                           receiving_clinician="Dr. Ravai",
                           followup_plan="术后 1 周与 1 月复查",
                           training_handover="裂隙灯复查与用药指导已培训")
        row = next(r for r in svc.board.worklist() if r["case_id"] == "c2")
        self.assertEqual(row["missing"], [])

    def test_ai_result_alone_never_grants_eligibility(self) -> None:
        svc = MissionService()
        svc.register_patient(case_id="c1", local_patient_id="TMP-1",
                             display_name="Ana", registered_by="coord")
        svc.record_screening(case_id="c1", recorded_by="tech",
                             device_id="CAM-1", algorithm_name="ai",
                             algorithm_version="9", ai_suggestion="立即手术",
                             is_ai_result=True)
        case = svc.board.get("c1")
        self.assertFalse(case.surgery_eligible)
        # 医生签署 eligible=True 才成立
        svc.review_screening(case_id="c1", reviewer="王医生",
                             license_id="CN-OPH-2031",
                             clinical_decision="白内障",
                             eligible_for_surgery=True)
        self.assertTrue(svc.board.get("c1").surgery_eligible)

    def test_identity_merge_is_reversible(self) -> None:
        svc = MissionService()
        svc.register_patient(case_id="paper-7", local_patient_id="P-7",
                             display_name="Ana Tuwai", registered_by="coord",
                             name_variants=["Anna Tuwai"])
        svc.register_patient(case_id="device-9", local_patient_id="D-9",
                             display_name="阿娜·图怀", registered_by="tech")

        merge = svc.merge_identities(kept_case_id="paper-7",
                                     merged_case_id="device-9",
                                     reason="译名差异且指纹相同",
                                     merged_by="coord-li")
        self.assertEqual(svc.board.canonical("device-9"), "paper-7")
        # 合并后两条登记的信息在同一视图下
        merged_view = svc.board.get("device-9")
        self.assertEqual(merged_view.case_id, "paper-7")
        self.assertEqual(len(merged_view.member_ids), 2)

        # 撤销合并：身份重新独立，历史数据未被销毁
        svc.revert_merge(merge_event_id=merge.event_id,
                         reason="指纹复核发现并非同一人",
                         reverted_by="coord-li")
        self.assertEqual(svc.board.canonical("device-9"), "device-9")
        self.assertEqual(svc.board.get("device-9").display_name, "阿娜·图怀")
        self.assertEqual(svc.board.get("paper-7").display_name, "Ana Tuwai")

    def test_merge_period_data_stays_with_original_registration(self) -> None:
        svc = build_mission()
        onboard(svc, case_id="paper-7", name="Ana", priority=2)
        onboard(svc, case_id="device-9", name="Anna", priority=3)
        merge = svc.merge_identities(kept_case_id="paper-7",
                                     merged_case_id="device-9",
                                     reason="同一人两次筛查", merged_by="coord")
        # 合并期间对权威病例排了台
        reserve(svc, case_id="paper-7", reservation_id="slot-1", start_h=9)
        svc.revert_merge(merge_event_id=merge.event_id,
                         reason="确认不是同一人", reverted_by="coord")
        # 排台记录仍归属于 paper-7，device-9 不受影响
        self.assertIsNotNone(svc.board.get("paper-7").reservation)
        self.assertIsNone(svc.board.get("device-9").reservation)

    def test_worklist_sorted_by_risk(self) -> None:
        svc = build_mission()
        onboard(svc, case_id="low", name="Lo", priority=4)
        onboard(svc, case_id="urgent", name="Ur", priority=1)
        onboard(svc, case_id="mid", name="Mi", priority=3)
        order = [r["case_id"] for r in svc.board.worklist()]
        self.assertEqual(order[:3], ["urgent", "mid", "low"])


if __name__ == "__main__":
    unittest.main()
