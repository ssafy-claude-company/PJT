"""Organt SNS — FastAPI 앱 (Phase 1: read-only 라이브 대시보드).

기동 시 두뇌의 기존 이벤트(flow/audit)를 적재 + tail 시작. 제공:
  GET  /                → 프론트(라이브 대시보드)
  GET  /api/snapshot    → 전 투영 스냅샷(+직무기준·프로젝트명 보강)
  GET  /api/profiles    → role_profiles.json (증류된 직무기준 = 에이전트 성장)
  WS   /ws              → 연결 시 스냅샷 → 이후 실시간 이벤트 스트림
무위험: 두뇌를 안 건드리고 *읽기만* 한다.
"""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from store import Store
from bus import Bus
from ingest import Ingestor

PJT = os.environ.get("ORGANT_PJT", "/home/user/PJT")
LOGS = os.path.join(PJT, "logs")
FRONTEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

app = FastAPI(title="Organt SNS", version="0.1.0")
store = Store()
bus = Bus()
ingestor = Ingestor(store, bus, logs_dir=LOGS)


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _profiles() -> dict:
    d = _load_json(os.path.join(LOGS, "role_profiles.json"), {})
    return {"profiles": d.get("profiles", {}), "experience_counts":
            {k: len(v) for k, v in (d.get("experience") or {}).items()}}


def _project_names() -> dict:
    d = _load_json(os.path.join(LOGS, "projects.json"), {})
    out = {}
    for v in (d.get("projects") or {}).values():
        if isinstance(v, dict) and v.get("id"):
            out[v["id"]] = {"name": v.get("name"), "leader": v.get("leader"),
                            "has_open_task": bool(v.get("open_task"))}
    return out


@app.on_event("startup")
async def _startup():
    bus.bind_loop(asyncio.get_event_loop())
    ingestor.load_initial()      # 기존 이벤트 ts순 적재
    ingestor.start_tail()        # 실시간 tail 시작


@app.get("/api/snapshot")
async def snapshot():
    snap = store.snapshot()
    snap["profiles"] = _profiles()
    snap["project_names"] = _project_names()
    return JSONResponse(snap)


@app.get("/api/profiles")
async def profiles():
    return JSONResponse(_profiles())


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    q = bus.subscribe()
    try:
        snap = store.snapshot()
        snap["profiles"] = _profiles()
        snap["project_names"] = _project_names()
        await socket.send_json({"type": "snapshot", "snapshot": snap})
        while True:
            msg = await q.get()
            await socket.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        bus.unsubscribe(q)


@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND, "index.html"))


# 정적 자산(있으면)
if os.path.isdir(FRONTEND):
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
