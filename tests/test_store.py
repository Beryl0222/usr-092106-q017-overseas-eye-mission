import unittest

from src.events import (AggregateType, Event, EventType, make_event)
from src.store import (ConcurrencyError, EventConflictError, EventStore,
                       ValidationError)


def registered(event_id: str, version: int, captured_at: str | None = None,
               request_id: str | None = None) -> Event:
    return make_event(
        EventType.PATIENT_REGISTERED, AggregateType.PATIENT_CASE,
        "case-offline-1", version, "登记",
        {"local_patient_id": "TMP-1", "display_name": "Ana"},
        event_id=event_id, captured_at=captured_at, request_id=request_id)


class EventStoreTest(unittest.TestCase):
    def test_event_ids_are_global_dedup(self) -> None:
        store = EventStore()
        first = registered("evt-1", 1)
        store.append(first)
        # 同 event_id 同内容的重传原样返回，不新增事件
        again = registered("evt-1", 1)
        returned = store.append(again)
        self.assertIs(returned, first)
        self.assertEqual(len(store.all_events()), 1)

    def test_same_event_id_different_content_rejected(self) -> None:
        store = EventStore()
        store.append(registered("evt-1", 1))
        other = make_event(
            EventType.PATIENT_REGISTERED, AggregateType.PATIENT_CASE,
            "case-offline-1", 1, "登记",
            {"local_patient_id": "TMP-1", "display_name": "不同的人"},
            event_id="evt-1")
        with self.assertRaises(EventConflictError):
            store.append(other)

    def test_request_id_idempotency(self) -> None:
        store = EventStore()
        store.append(registered("evt-1", 1, request_id="req-A"))
        retried = registered("evt-2", 1, request_id="req-A")
        returned = store.append(retried)
        self.assertEqual(returned.event_id, "evt-1")
        self.assertEqual(len(store.all_events()), 1)

    def test_concurrency_guard(self) -> None:
        store = EventStore()
        store.append(registered("evt-1", 1))
        with self.assertRaises(ConcurrencyError):
            store.append(registered("evt-2", 2), expected_version=0)
        store.append(registered("evt-3", 2), expected_version=1)
        self.assertEqual(store.stream_version("case-offline-1"), 2)

    def test_offline_batch_renumbers_and_dedups(self) -> None:
        store = EventStore()
        # 已有版本 1
        store.append(registered("evt-old", 1, captured_at="2026-10-05T08:00:00+12:00"))
        batch = [
            # 版本号是离线客户端本地的，归并时统一重排
            registered("evt-b1", 99, captured_at="2026-10-05T09:00:00+12:00"),
            registered("evt-b2", 3, captured_at="2026-10-05T10:00:00+12:00"),
            registered("evt-b1", 99, captured_at="2026-10-05T09:00:00+12:00"),  # 重传
        ]
        stored = store.merge_batch(batch)
        # 结果按入参顺序返回 3 条，重复项映射到同一最终事件
        self.assertEqual([e.event_id for e in stored],
                         ["evt-b1", "evt-b2", "evt-b1"])
        self.assertIs(stored[0], stored[2])
        # 实际新存储只有 2 条，版本在流尾续接
        self.assertEqual(len(store.all_events()), 3)  # old + b1 + b2
        versions = {e.event_id: e.version for e in store.all_events()}
        self.assertEqual(versions["evt-b1"], 2)
        self.assertEqual(versions["evt-b2"], 3)

    def test_merge_batch_fails_atomically(self) -> None:
        store = EventStore()
        good = registered("evt-g", 1)
        bad = make_event(
            EventType.PATIENT_REGISTERED, AggregateType.PATIENT_CASE,
            "case-x", 1, "登记", {"local_patient_id": "TMP-x"},
            event_id="evt-bad")  # 缺 display_name
        with self.assertRaises(ValidationError):
            store.merge_batch([good, bad])
        self.assertEqual(store.all_events(), [])


if __name__ == "__main__":
    unittest.main()
