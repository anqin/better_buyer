"""进程内事件总线：后台智能体推送决策，SSE 按游标增量读取。"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from ..models import now_iso


class EventBus:
    def __init__(self, maxlen: int = 500):
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._counter = 0

    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._counter += 1
            item = {"id": self._counter, "at": now_iso(), **event}
            self._events.append(item)
            return item

    def since(self, cursor: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [item for item in self._events if item["id"] > cursor]

    def latest_cursor(self) -> int:
        with self._lock:
            return self._counter

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]
