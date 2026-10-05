# 海外光明行任务台

中国眼科医疗队在斐济等海外任务点，数日之内要在本地纸质名单、便携筛查设备与
医院手术排期之间完成筛查、转诊与治疗。本仓库是任务台后端核心：事件溯源的
领域模型，把每位患者的缺口、可用房间/耗材/团队负荷与当地接续能力放进同一
套排程与审计语义。

## 领域原则

- **事件不可变**：事件一经追加，`event_id` / `aggregate_id` / `occurred_at` /
  `version` 不得原地改写；业务更正（优先级变化、撤销合并、顺延排期）一律
  产生后继事件。
- **AI 仅作辅助证据**：便携设备结果（含设备 ID、算法名与版本）以
  `SCREENING_RECORDED` 记录并显式标注 `is_ai_result`；诊断与手术资格只承认
  有资质医生持执照号的 `SCREENING_REVIEWED` 签署。
- **身份合并可撤销**：译名差异或多次筛查形成的重复身份可合并；
  `IDENTITY_MERGE_REVERTED` 撤销后各登记重新独立，合并期间的记录仍归属原
  登记，不丢失、不错配。
- **重传不重复消耗名额**：事件仓库按 `event_id`（全局）与 `request_id`
  （流内）幂等；离线批次通过仓库事件契约归并，重发返回首次结果。
- **紧急加台有保护**：加台必须重新校验房间、无菌物资、随访容量与团队疲劳；
  只能顺延风险**严格更低**且被显式列入名单的在排患者，更高或同等风险一律
  不可挤压；被拒不占任何资源。
- **复盘须获准**：任务结束后的复盘导出必须有 `REVIEW_EXPORT_AUTHORIZED`
  授权（范围/用途/有效期），资料逐次解释每次临床优先级变化。

## 代码结构

| 路径 | 职责 |
| --- | --- |
| `contracts/domain.schema.json` | 事件信封、事件类型与聚合类型契约 |
| `src/events.py` | 事件定义、各事件 payload 字段契约（`PAYLOAD_SPEC`）、构造器 |
| `src/validator.py` | 信封与 payload 结构校验（不做临床裁决） |
| `src/store.py` | 只追加事件仓库：版本连续、乐观并发、幂等重传、离线批次归并 |
| `src/domain.py` | 读模型：患者任务台（四项缺口、身份合并）、资源台账（房间/耗材/班次/随访容量） |
| `src/scheduling.py` | 排程与紧急加台策略：资格、房间、无菌物资、疲劳、风险保护统一校验 |
| `src/app.py` | `MissionService`：协调组操作 → 领域事件，读模型自动重放 |
| `src/review.py` | 复盘授权门控与逐次优先级变化的复盘资料导出 |
| `data/sample.json` | 中文样例事件（离线登记） |
| `tests/` | 33 个测试：契约、仓库幂等/归并、任务台、排程/加台、交接、复盘、离线全链路 |

## 任务台四项缺口

每位患者实时标出：

1. `screening_review` — 尚缺医生筛查复核签署（AI 结果不能替代）；
2. `informed_consent` — 尚缺知情同意（记录同意语言、版本、译员）；
3. `surgery_resource` — 尚未落实手术房间/耗材/团队排期；
4. `postop_destination` — 尚缺术后去向与当地接续随访。

## 快速上手

```python
from src.app import MissionService

svc = MissionService()
svc.update_rooms(facility_id="FJ-SUVA-EYE", updated_by="logistics",
                 rooms=[{"room_id": "OR-1",
                         "available_from": "2026-10-05T07:00:00+12:00",
                         "available_to":   "2026-10-05T19:00:00+12:00"}])
svc.update_supplies(facility_id="FJ-SUVA-EYE", updated_by="logistics",
                    items={"IOL-STD": 5, "STERILE-KIT": 5})
svc.update_followup_capacity(facility_id="FJ-SUVA-EYE", updated_by="local-nurse",
                             capacity_by_date={"2026-10-12": 8})
svc.declare_shift(staff_id="DR-WANG", role="ophthalmic_surgeon",
                  shift_start="2026-10-05T07:00:00+12:00",
                  shift_end="2026-10-05T19:00:00+12:00",
                  max_cases=6, max_duty_minutes=600)

# 登记 → AI 筛查 → 医生复核签署 → 多语同意 → 优先级
svc.register_patient(case_id="case-1", local_patient_id="SUVA-2026-017",
                     display_name="Ana Tuwai", registered_by="coord-li")
svc.record_screening(case_id="case-1", recorded_by="tech-01",
                     device_id="SCREEN-CAM-7", algorithm_name="cataract-detect",
                     algorithm_version="3.2.1", is_ai_result=True,
                     ai_suggestion="疑似白内障，建议医生复核")
svc.review_screening(case_id="case-1", reviewer="王医生",
                     license_id="CN-OPH-2031",
                     clinical_decision="年龄相关性白内障，建议手术",
                     eligible_for_surgery=True)
svc.sign_consent(case_id="case-1", language="fj", consent_version="v4-fj-en",
                 signed_by="Ana Tuwai", interpreter_present=True,
                 interpreter_language="fj")
svc.set_priority(case_id="case-1", priority_level=2, reason="筛查初评",
                 set_by="triage-nurse")

# 统一排程（资格/房间/耗材/疲劳/随访容量任一不过即拒绝）
svc.scheduler.reserve(
    reservation_id="slot-1", case_id="case-1",
    procedure="白内障超声乳化+IOL", room_id="OR-1",
    start="2026-10-05T09:00:00+12:00", end="2026-10-05T09:45:00+12:00",
    surgeon_id="DR-WANG", supplies={"IOL-STD": 1, "STERILE-KIT": 1},
    followup_date="2026-10-12")

svc.board.worklist()          # 按风险排序的任务台
svc.ledger.team_load()        # 团队负荷
svc.ledger.available_supplies()  # 耗材余量（盘点 - 已占用）

# 任务结束：当地医院接续 + 获准复盘
svc.accept_handoff(case_id="case-1", facility_id="FJ-SUVA-EYE",
                   receiving_clinician="Dr. Ravai",
                   followup_plan="术后 1 周与 1 月复查",
                   training_handover="裂隙灯复查与用药指导已培训")
svc.reviews.authorize_export(authorized_by="mission-lead-chen",
                             scope="all", purpose="任务质量复盘")
svc.reviews.export_review_dossier()
```

离线材料归并：

```python
svc.import_offline_batch(offline_events)  # 去重、按采集时间排序、续版本
```

## 本地检查

```bash
python3 -m unittest discover -s tests
```
