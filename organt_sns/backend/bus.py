"""Organt SNS — 실시간 이벤트 버스(WebSocket fan-out).

ingest가 새 Event를 append하면 Store가 이 버스로 통지 → 연결된 모든 WS 클라이언트에 broadcast.
간단한 asyncio 큐 기반 pub/sub. 확장: 스코프별(프로젝트/Task) 토픽 구독을 여기 얹으면 됨.
"""
from __future__ import annotations

import asyncio
from typing import Any


class Bus:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish_threadsafe(self, msg: dict[str, Any]) -> None:
        """ingest 스레드(블로킹 tail)에서 호출 — 이벤트 루프로 안전 전달."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict[str, Any]) -> None:
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)  # 느린 클라이언트는 끊음(백프레셔)
        for q in dead:
            self._subscribers.discard(q)
