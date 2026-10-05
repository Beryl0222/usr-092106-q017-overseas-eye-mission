"""测试共用构造：一个已盘点资源的斐济任务现场。"""

from src.app import MissionService

TZ = "+12:00"
DAY = "2026-10-05"
FACILITY = "FJ-SUVA-EYE"
ROOM = "OR-1"
SURGEON = "DR-WANG"
SUPPLIES = {"IOL-STD": 1, "STERILE-KIT": 1}
FOLLOWUP_DATE = "2026-10-12"


def ts(hour: int, minute: int = 0, day: str = DAY) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:00{TZ}"


def build_mission(*, supplies=None, followup_capacity: int = 8,
                  max_cases: int = 6) -> MissionService:
    svc = MissionService()
    svc.update_rooms(facility_id=FACILITY, updated_by="logistics",
                     rooms=[{"room_id": ROOM,
                             "available_from": ts(7), "available_to": ts(19)}])
    svc.update_supplies(facility_id=FACILITY, updated_by="logistics",
                        items=supplies or {"IOL-STD": 5, "STERILE-KIT": 5})
    svc.update_followup_capacity(facility_id=FACILITY, updated_by="local-nurse",
                                 capacity_by_date={FOLLOWUP_DATE: followup_capacity})
    svc.declare_shift(staff_id=SURGEON, role="ophthalmic_surgeon",
                      shift_start=ts(7), shift_end=ts(19),
                      max_cases=max_cases, max_duty_minutes=600)
    return svc


def onboard(svc: MissionService, *, case_id: str, name: str,
            priority: int, eligible: bool = True,
            consent_language: str = "fj") -> str:
    """登记 -> AI 筛查 -> 医生复核签署 -> 多语同意 -> 优先级。"""
    svc.register_patient(case_id=case_id, local_patient_id=f"TMP-{case_id}",
                         display_name=name, registered_by="coord",
                         preferred_language=consent_language)
    svc.record_screening(case_id=case_id, recorded_by="tech-01",
                         device_id="SCREEN-CAM-7",
                         algorithm_name="cataract-detect",
                         algorithm_version="3.2.1",
                         findings={"opacity": "suspected"},
                         ai_suggestion="疑似皮质性白内障，建议医生复核",
                         is_ai_result=True)
    svc.review_screening(
        case_id=case_id, reviewer="王医生", license_id="CN-OPH-2031",
        clinical_decision="年龄相关性白内障，建议手术",
        eligible_for_surgery=eligible, diagnosis_codes=["H25.9"])
    svc.sign_consent(case_id=case_id, language=consent_language,
                     consent_version="v4-fj-en", signed_by=name,
                     interpreter_present=True,
                     interpreter_language=consent_language,
                     procedure_scope="白内障超声乳化+人工晶体植入")
    svc.set_priority(case_id=case_id, priority_level=priority,
                     reason="筛查初评", set_by="triage-nurse")
    return case_id


def reserve(svc: MissionService, *, case_id: str, reservation_id: str,
            start_h: int, start_m: int = 0, duration: int = 45,
            supplies=None):
    from datetime import datetime, timedelta
    start = f"{DAY}T{start_h:02d}:{start_m:02d}:00{TZ}"
    end_dt = datetime.fromisoformat(start) + timedelta(minutes=duration)
    end = end_dt.isoformat()
    return svc.scheduler.reserve(
        reservation_id=reservation_id, case_id=case_id,
        procedure="白内障超声乳化+IOL", room_id=ROOM, start=start, end=end,
        surgeon_id=SURGEON, supplies=supplies or dict(SUPPLIES),
        followup_date=FOLLOWUP_DATE)
