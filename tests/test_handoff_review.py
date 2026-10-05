import unittest

from src.review import ReviewAuthorizationError
from tests.helpers import build_mission, onboard, reserve, ts


class HandoffTest(unittest.TestCase):
    def test_local_hospital_continues_followup(self) -> None:
        svc = build_mission()
        onboard(svc, case_id="c1", name="Ana", priority=2)
        reserve(svc, case_id="c1", reservation_id="slot-1", start_h=9)
        svc.complete_treatment(
            case_id="c1", procedure="白内障超声乳化+IOL",
            surgeon_id="DR-WANG", license_id="CN-OPH-2031",
            completed_at=ts(10), reservation_id="slot-1",
            supplies_consumed={"IOL-STD": 1, "STERILE-KIT": 1})
        svc.accept_handoff(
            case_id="c1", facility_id="FJ-SUVA-EYE",
            receiving_clinician="Dr. Ravai (FJ-MC-112)",
            followup_plan="术后 1 周裂隙灯复查，1 月验光",
            training_handover="已示范裂隙灯检查与滴眼液方案，附操作卡",
            contacts={"phone": "+679-332-1000"})
        svc.schedule_followup(case_id="c1", facility_id="FJ-SUVA-EYE",
                              followup_date="2026-10-12",
                              clinician="Dr. Ravai")
        case = svc.board.get("c1")
        self.assertEqual(case.handoff["facility_id"], "FJ-SUVA-EYE")
        self.assertEqual(len(case.followups), 1)
        # 治疗完成后四项缺口全部关闭
        self.assertEqual(case.missing_checklist(), [])


class ReviewExportTest(unittest.TestCase):
    def _mission(self):
        svc = build_mission()
        onboard(svc, case_id="c1", name="Ana", priority=3)
        svc.set_priority(case_id="c1", priority_level=1,
                         reason="左眼视力降至手动，疑似成熟期白内障",
                         set_by="DR-WANG")
        svc.set_priority(case_id="c1", priority_level=2,
                         reason="用药后眼压回落，改排次日首台",
                         set_by="DR-WANG")
        return svc

    def test_export_requires_authorization(self) -> None:
        svc = self._mission()
        with self.assertRaises(ReviewAuthorizationError):
            svc.reviews.export_case_review("c1")

    def test_authorized_export_explains_every_priority_change(self) -> None:
        svc = self._mission()
        svc.reviews.authorize_export(authorized_by="mission-lead-chen",
                                     scope="all", purpose="任务质量复盘")
        dossier = svc.reviews.export_review_dossier()
        self.assertEqual(dossier["case_count"], 1)
        report = dossier["cases"][0]
        timeline = report["priority_timeline"]
        self.assertEqual([c["to_level"] for c in timeline], [3, 1, 2])
        self.assertEqual(timeline[1]["from_level"], 3)
        self.assertIn("眼压回落", timeline[2]["reason"])
        # AI 证据与医生签署分列，且明示 AI 仅辅助
        self.assertTrue(report["ai_assists"])
        self.assertEqual(
            report["ai_assists"][0]["algorithm_version"], "3.2.1")
        self.assertTrue(report["physician_review"]["eligible_for_surgery"])
        self.assertIn("AI", report["note"])

    def test_scoped_authorization_covers_only_listed_cases(self) -> None:
        svc = self._mission()
        onboard(svc, case_id="c2", name="Bera", priority=4)
        svc.reviews.authorize_export(authorized_by="lead",
                                     scope="cases", case_ids=["c1"])
        self.assertIsNotNone(svc.reviews.export_case_review("c1"))
        with self.assertRaises(ReviewAuthorizationError):
            svc.reviews.export_case_review("c2")

    def test_expired_authorization_rejected(self) -> None:
        svc = self._mission()
        svc.reviews.authorize_export(authorized_by="lead", scope="all",
                                     expires_at="2026-10-01T00:00:00+12:00")
        with self.assertRaises(ReviewAuthorizationError):
            svc.reviews.export_review_dossier(at="2026-10-05T18:00:00+12:00")


if __name__ == "__main__":
    unittest.main()
