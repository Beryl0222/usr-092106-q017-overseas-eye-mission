"""校验领域事件信封及其业务负载。

校验只判断结构是否完整，不做任何临床裁决。诊断与手术资格的有效性
属于聚合/排程层，必须由有资质医生签署的事件承载。
"""

from __future__ import annotations

from typing import Any

from src.events import PAYLOAD_SPEC, AggregateType, EventType

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id",
            "occurred_at", "version", "summary")

KNOWN_EVENT_TYPES = {t.value for t in EventType}
KNOWN_AGGREGATE_TYPES = {t.value for t in AggregateType}


def validate_event(record: dict[str, Any]) -> list[str]:
    """返回错误信息列表；空列表表示通过。"""
    errors: list[str] = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if errors:
        return errors

    if not isinstance(record["event_id"], str) or not record["event_id"].strip():
        errors.append("event_id 必须是非空字符串")
    if not isinstance(record["aggregate_id"], str) or not record["aggregate_id"].strip():
        errors.append("aggregate_id 必须是非空字符串")
    if not isinstance(record["summary"], str) or not record["summary"].strip():
        errors.append("summary 必须是非空字符串")

    event_type = record.get("event_type")
    if event_type not in KNOWN_EVENT_TYPES:
        errors.append(f"未知 event_type：{event_type}")
    aggregate_type = record.get("aggregate_type")
    if aggregate_type not in KNOWN_AGGREGATE_TYPES:
        errors.append(f"未知 aggregate_type：{aggregate_type}")

    version = record.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("version 必须是正整数")

    payload = record.get("payload", {})
    if not isinstance(payload, dict):
        errors.append("payload 必须是对象")
        return errors

    spec = PAYLOAD_SPEC.get(event_type)
    if spec is not None:
        for name in spec["required"]:
            if name not in payload or payload[name] in (None, ""):
                errors.append(f"{event_type} 缺少必需负载字段：{name}")
        unknown = set(payload) - spec["required"] - spec["optional"]
        if unknown:
            errors.append(f"{event_type} 含未声明负载字段：{sorted(unknown)}")

    return errors
