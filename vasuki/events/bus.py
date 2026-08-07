"""Small async-friendly publish/subscribe bus."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any

from vasuki.events.events import MissionEvent

EventHandler = Callable[[MissionEvent], Awaitable[None] | None]


async def _await_handler(result: Awaitable[None]) -> None:
    await result


@dataclass(slots=True)
class EventSubscription:
    bus: EventBus
    handler: EventHandler

    def close(self) -> None:
        self.bus.unsubscribe(self.handler)


class EventBus:
    """Fan events out to callbacks and async session queues.

    Publishing is non-blocking for queue consumers. Async callbacks are scheduled
    on the running loop; synchronous persistence callbacks complete inline.
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self._queues: set[asyncio.Queue[MissionEvent]] = set()
        self._lock = RLock()

    def subscribe(self, handler: EventHandler) -> EventSubscription:
        with self._lock:
            self._handlers.append(handler)
        return EventSubscription(self, handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def open_stream(self, *, maxsize: int = 1000) -> asyncio.Queue[MissionEvent]:
        queue: asyncio.Queue[MissionEvent] = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._queues.add(queue)
        return queue

    def close_stream(self, queue: asyncio.Queue[MissionEvent]) -> None:
        with self._lock:
            self._queues.discard(queue)

    def publish(self, event: MissionEvent) -> None:
        with self._lock:
            handlers = tuple(self._handlers)
            queues = tuple(self._queues)
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Preserve recent state for slow UI consumers.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        for handler in handlers:
            result = handler(event)
            if inspect.isawaitable(result):
                try:
                    asyncio.get_running_loop().create_task(_await_handler(result))
                except RuntimeError:
                    asyncio.run(_await_handler(result))

    async def wait_for(
        self,
        event_type: type[MissionEvent],
        *,
        timeout: float | None = None,
    ) -> MissionEvent:
        queue = self.open_stream()
        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout)
                if isinstance(event, event_type):
                    return event
        finally:
            self.close_stream(queue)

    def publish_data(self, event_type: type[MissionEvent], **data: Any) -> None:
        self.publish(event_type(**data))
