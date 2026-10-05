"""事件仓库：只追加、可归并、幂等重传。

设计要点：
- 事件按聚合流（aggregate_id）连续编号 version；已存储事件的标识、发生时间
  与版本永不原地改写。
- event_id 全局唯一；request_id 在同一聚合流内唯一。客户端重发同一批离线
  材料时，仓库识别出同一 event_id / request_id 并原样返回，不会产生第二条
  SLOT_RESERVED，因此重传不会重复消耗名额。
- 离线事件允许携带客户端采集序号到达；归并时按 (captured_at, event_id)
  排序后在流尾续接版本号。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable

from src.events import Event
from src.validator import validate_event


class EventStoreError(Exception):
    """仓库层错误基类。"""


class ConcurrencyError(EventStoreError):
    """流版本与预期不一致（在线乐观并发失败）。"""


class EventConflictError(EventStoreError):
    """同一 event_id / request_id 被用于内容不同的事件。"""


class ValidationError(EventStoreError):
    """事件结构校验未通过。"""


class EventStore:
    def __init__(self) -> None:
        self._events: list[Event] = []                     # 全局追加日志
        self._streams: dict[str, list[Event]] = {}
        self._event_ids: dict[str, Event] = {}
        self._request_ids: dict[tuple[str, str], Event] = {}
        self._listeners: list[Callable[[Event], None]] = []

    # ---- 订阅（读模型重建用） ----------------------------------------------

    def subscribe(self, listener: Callable[[Event], None]) -> None:
        self._listeners.append(listener)

    def _emit(self, event: Event) -> None:
        for listener in self._listeners:
            listener(event)

    # ---- 查询 --------------------------------------------------------------

    def stream_events(self, aggregate_id: str) -> list[Event]:
        return list(self._streams.get(aggregate_id, ()))

    def stream_version(self, aggregate_id: str) -> int:
        return len(self._streams.get(aggregate_id, ()))

    def all_events(self) -> list[Event]:
        return list(self._events)

    # ---- 在线追加 -----------------------------------------------------------

    def append(self, event: Event, *, expected_version: int | None = None) -> Event:
        """追加单个事件。

        expected_version 为调用方看到的流版本；提供时必须与当前版本一致。
        若 event_id 或 request_id 已存在且内容一致，视为重传，原样返回
        （幂等，不会产生副作用）。
        """
        errors = validate_event(event.to_dict())
        if errors:
            raise ValidationError("；".join(errors))

        existing = self._event_ids.get(event.event_id)
        if existing is not None:
            return self._idempotent_return(existing, event)

        if event.request_id:
            key = (event.aggregate_id, event.request_id)
            prior = self._request_ids.get(key)
            if prior is not None:
                return prior  # 同一请求重发：返回首次结果，不新增事件

        current = self.stream_version(event.aggregate_id)
        if expected_version is not None and expected_version != current:
            raise ConcurrencyError(
                f"流 {event.aggregate_id} 版本冲突：预期 {expected_version}，实际 {current}"
            )
        if event.version != current + 1:
            raise ConcurrencyError(
                f"流 {event.aggregate_id} 版本不连续：期望 {current + 1}，收到 {event.version}"
            )

        self._store(event)
        return event

    # ---- 离线归并 -----------------------------------------------------------

    def merge_batch(self, events: Iterable[Event]) -> list[Event]:
        """归并一批（多为离线采集的）事件。

        - 先做结构校验，整批不通过则不写入任何事件；
        - 按 event_id / request_id 去重，已存在且一致的重传事件直接回传；
        - 同一流内未存储的事件按 (captured_at, occurred_at, event_id) 排序，
          在流尾续接版本；
        - 返回“最终生效”的事件列表（重传项映射回已存储事件，保持顺序）。
        """
        incoming = list(events)
        for event in incoming:
            errors = validate_event(event.to_dict())
            if errors:
                raise ValidationError(f"批次中事件 {event.event_id} 校验失败：{'；'.join(errors)}")

        result: list[Event] = []
        by_stream: dict[str, list[Event]] = {}
        seen_event_ids: set[str] = set()       # 本批次内去重
        seen_requests: set[tuple[str, str]] = set()

        for event in incoming:
            existing = self._event_ids.get(event.event_id)
            if existing is not None:
                result.append(self._idempotent_return(existing, event))
                continue
            if event.event_id in seen_event_ids:
                # 同批次内的重复（如离线重发）：定位首次出现并回传同一对象
                result.append(next(e for e in result
                                   if e.event_id == event.event_id))
                continue
            if event.request_id:
                key = (event.aggregate_id, event.request_id)
                prior = self._request_ids.get(key)
                if prior is not None:
                    result.append(prior)
                    continue
                if key in seen_requests:
                    result.append(next(e for e in result
                                       if e.request_id == event.request_id
                                       and e.aggregate_id == event.aggregate_id))
                    continue
                seen_requests.add(key)
            seen_event_ids.add(event.event_id)
            result.append(event)
            by_stream.setdefault(event.aggregate_id, []).append(event)

        final_by_id: dict[str, Event] = dict(self._event_ids)
        for stream_id, stream_events in by_stream.items():
            current = self.stream_version(stream_id)
            stream_events.sort(key=lambda e: (e.captured_at or e.occurred_at,
                                              e.occurred_at, e.event_id))
            for offset, event in enumerate(stream_events, start=1):
                renumbered = replace(event, version=current + offset)
                self._store(renumbered)
                final_by_id[renumbered.event_id] = renumbered

        # 统一映射回最终生效（可能已重编号）的事件对象，重复项指向同一对象。
        return [final_by_id[e.event_id] for e in result]

    # ---- 内部 ---------------------------------------------------------------

    def _store(self, event: Event) -> None:
        if event.event_id in self._event_ids:
            # merge_batch 已在外层去重；走到这里属于编程错误。
            raise EventConflictError(f"event_id 重复：{event.event_id}")
        if event.request_id and (event.aggregate_id, event.request_id) in self._request_ids:
            raise EventConflictError(
                f"request_id 在流内重复：{event.request_id}")
        self._events.append(event)
        self._streams.setdefault(event.aggregate_id, []).append(event)
        self._event_ids[event.event_id] = event
        if event.request_id:
            self._request_ids[(event.aggregate_id, event.request_id)] = event
        self._emit(event)

    def _idempotent_return(self, existing: Event, retried: Event) -> Event:
        """重传事件必须与首次内容一致；标识相同但业务内容不同则拒绝。"""
        same = (
            existing.event_type == retried.event_type
            and existing.aggregate_id == retried.aggregate_id
            and existing.payload == retried.payload
        )
        if not same:
            raise EventConflictError(
                f"event_id {retried.event_id} 已存在但事件内容不同，拒绝覆盖")
        return existing
