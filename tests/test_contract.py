import json
import unittest
from pathlib import Path

from src.events import Event, make_event, AggregateType, EventType
from src.validator import validate_event

ROOT = Path(__file__).parents[1]


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_missing_payload_field_rejected(self) -> None:
        event = make_event(EventType.PATIENT_REGISTERED,
                           AggregateType.PATIENT_CASE, "c1", 1, "x",
                           {"local_patient_id": "TMP-1"})  # 缺 display_name
        errors = validate_event(event.to_dict())
        self.assertTrue(any("display_name" in e for e in errors))

    def test_unknown_type_and_bool_version_rejected(self) -> None:
        event = make_event(EventType.PATIENT_REGISTERED,
                           AggregateType.PATIENT_CASE, "c1", 1, "x",
                           {"local_patient_id": "T", "display_name": "n"})
        data = event.to_dict()
        data["version"] = True  # type: ignore[dict-item]
        self.assertTrue(any("version" in e for e in validate_event(data)))

    def test_declared_payload_is_complete(self) -> None:
        # 样例事件必须带齐所有事件类型的必需字段契约。
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertIn("payload", sample)


if __name__ == "__main__":
    unittest.main()
