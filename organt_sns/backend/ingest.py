"""Organt SNS — 이벤트 인제스트(두뇌의 flow.jsonl + audit.jsonl을 읽어 Store에 흘림).

Phase 1 = read-only·무위험: 두뇌가 *이미* 쓰는 로그를 tail만 한다(두뇌 안 건드림). 초기엔 기존
전체를 ts 순으로 적재, 이후 새 줄을 폴링으로 tail해 실시간 반영. 이후 Phase에서 in-process 싱크
(Sys._log/AuditLog.record 직결)로 교체하면 무폴링·무지연이 된다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

from events import normalize
from store import Store
from bus import Bus

DEFAULT_LOGS = "/home/user/PJT/logs"


def _read_lines(path: str, offset: int) -> tuple[list[str], int]:
    """offset 이후 완결된 줄들과 새 offset 반환(미완 줄은 다음으로)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:          # 로테이션/재생성 → 처음부터
        offset = 0
    if size == offset:
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    text = data.decode("utf-8", "replace")
    if "\n" not in text:
        return [], offset                     # 완결 줄 없음
    last_nl = text.rfind("\n")
    complete = text[: last_nl]
    new_offset = offset + len(complete.encode("utf-8")) + 1
    return [ln for ln in complete.split("\n") if ln.strip()], new_offset


class Ingestor:
    def __init__(self, store: Store, bus: Bus, logs_dir: str = DEFAULT_LOGS,
                 poll_sec: float = 1.0):
        self.store = store
        self.bus = bus
        self.flow = os.path.join(logs_dir, "flow.jsonl")
        self.audit = os.path.join(logs_dir, "audit.jsonl")
        self.poll_sec = poll_sec
        self._off = {self.flow: 0, self.audit: 0}
        self._stop = threading.Event()

    def _push(self, line: str, source: str, live: bool) -> None:
        try:
            rec = json.loads(line)
        except Exception:
            return
        ev = normalize(rec, source)
        if ev is None:
            return
        ev = self.store.append(ev)
        if live:
            self.bus.publish_threadsafe({"type": "event", "event": ev.model_dump()})

    def load_initial(self, max_bytes: int = 1_500_000, max_lines: int = 4000) -> None:
        """*최근* 윈도우만 ts 순 병합 적재(초기 스냅샷). 전체(20MB audit)를 읽으면 기동이 느리고
        메모리가 큼 — 라이브 대시보드엔 최근 이벤트면 충분. 이후 tail은 파일 끝에서 이어간다."""
        pending = []
        for path, source in ((self.flow, "flow"), (self.audit, "audit")):
            try:
                size = os.path.getsize(path)
            except OSError:
                self._off[path] = 0
                continue
            start = max(0, size - max_bytes)
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read()
            self._off[path] = size                      # tail은 현재 끝부터(중복 방지)
            text = data.decode("utf-8", "replace")
            lines = text.split("\n")
            if start > 0 and lines:
                lines = lines[1:]                        # 앞 미완 줄 버림
            for ln in [l for l in lines if l.strip()][-max_lines:]:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                ev = normalize(rec, source)
                if ev is not None:
                    pending.append(ev)
        pending.sort(key=lambda e: e.ts)
        for ev in pending:
            self.store.append(ev)   # seq를 ts 순으로 부여

    def _tail_loop(self) -> None:
        while not self._stop.is_set():
            for path, source in ((self.flow, "flow"), (self.audit, "audit")):
                lines, off = _read_lines(path, self._off[path])
                self._off[path] = off
                for ln in lines:
                    self._push(ln, source, live=True)
            self._stop.wait(self.poll_sec)

    def start_tail(self) -> threading.Thread:
        t = threading.Thread(target=self._tail_loop, name="organt-sns-ingest", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
