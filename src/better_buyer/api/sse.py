"""SSE：向手机端增量推送后台盯盘决策与通知事件。"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from ..core.events import EventBus


async def event_stream(bus: EventBus, cursor: int = 0, keepalive: int = 15) -> AsyncIterator[str]:
    last_keepalive = 0.0
    loop = asyncio.get_running_loop()
    while True:
        events = bus.since(cursor)
        if events:
            for event in events:
                cursor = int(event["id"])
                payload = json.dumps(event, ensure_ascii=False, default=str)
                yield f"id: {cursor}\nevent: {event.get('event', 'message')}\ndata: {payload}\n\n"
            last_keepalive = loop.time()
        elif loop.time() - last_keepalive >= keepalive:
            yield ": keepalive\n\n"
            last_keepalive = loop.time()
        await asyncio.sleep(1)
