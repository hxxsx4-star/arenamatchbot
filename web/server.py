"""종합게임 아레나 - 경매 웹 서버 (FastAPI + WebSocket).

실행:  arenamatchbot 리포 루트에서
    uvicorn web.server:app --host 0.0.0.0 --port 8000
또는  python -m web.server
"""
from __future__ import annotations

import os
import sys
import json
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# 리포 루트를 path 에 추가 → 봇과 공유하는 utils.logs 사용 가능
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.auction import AuctionRoom, Phase  # noqa: E402

try:
    from web.discord_hook import post_auction_result  # noqa: E402
except Exception:  # 디스코드 연동 실패해도 웹은 동작
    async def post_auction_result(room):  # type: ignore
        return

BASE = Path(__file__).resolve().parent
app = FastAPI(title="아레나 경매")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

# 공개 URL (링크 생성용). 배포 시 환경변수로 지정.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


def _abs(path: str, request: Request) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}{path}"


class RoomManager:
    def __init__(self):
        self.rooms: dict[str, AuctionRoom] = {}
        self.conns: dict[str, set[WebSocket]] = {}

    def add(self, room: AuctionRoom):
        self.rooms[room.id] = room
        self.conns[room.id] = set()
        room.bind(broadcast=lambda: self.broadcast(room.id),
                  on_finish=post_auction_result)

    def get(self, rid: str) -> AuctionRoom | None:
        return self.rooms.get(rid)

    async def broadcast(self, rid: str):
        room = self.rooms.get(rid)
        if not room:
            return
        dead = []
        for ws in list(self.conns.get(rid, ())):
            role = getattr(ws, "_role", "observer")
            vcid = getattr(ws, "_cid", None)
            try:
                await ws.send_json({"type": "state", "state": room.snapshot(role, vcid)})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.conns[rid].discard(ws)


manager = RoomManager()


# ============ 페이지 ============
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/create", response_class=HTMLResponse)
async def create_page(request: Request):
    return templates.TemplateResponse(request, "create.html")


@app.post("/api/auctions")
async def create_auction(request: Request):
    body = await request.json()
    # 기본 검증
    try:
        title = (body.get("title") or "").strip()
        if not title:
            raise ValueError("경매 타이틀을 입력하세요.")
        captains = [c for c in body.get("captains", []) if (c.get("name") or "").strip()]
        members = [m for m in body.get("members", []) if (m.get("name") or "").strip()]
        team_count = int(body.get("team_count", 0))
        team_size = int(body.get("team_size", 0))
        if len(captains) != team_count:
            raise ValueError(f"팀장 수({len(captains)})가 팀 수({team_count})와 다릅니다.")
        expected_members = (team_size - 1) * team_count
        if len(members) == 0:
            raise ValueError("경매 대상 팀원을 최소 1명 입력하세요.")
        config = {
            "title": title, "team_count": team_count, "team_size": team_size,
            "total_points": int(body.get("total_points", 1000)),
            "show_order": bool(body.get("show_order", True)),
            "bid_mode": body.get("bid_mode", "fixed"),
            "fixed_unit": int(body.get("fixed_unit", 5)),
            "ratio_percent": float(body.get("ratio_percent", 10)),
            "bid_time": int(body.get("bid_time", 15)),
            "extend_time": int(body.get("extend_time", 5)),
            "captains": captains, "members": members,
        }
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    room = AuctionRoom(config)
    manager.add(room)

    host_link = _abs(f"/room/{room.id}?host={room.host_token}", request)
    observer_link = _abs(f"/room/{room.id}?ob={room.observer_token}", request)
    captain_links = [
        {"name": c.name, "cid": c.cid,
         "link": _abs(f"/room/{room.id}?cap={c.token}", request)}
        for c in room.captains
    ]
    return JSONResponse({
        "id": room.id,
        "host_link": host_link,
        "observer_link": observer_link,
        "captain_links": captain_links,
    })


@app.get("/room/{rid}", response_class=HTMLResponse)
async def room_page(request: Request, rid: str):
    room = manager.get(rid)
    if not room:
        return templates.TemplateResponse(request, "gone.html", status_code=404)
    role, cid, cname = _resolve_role(room, request.query_params)
    if role is None:
        return templates.TemplateResponse(request, "gone.html", status_code=403)
    return templates.TemplateResponse(request, "room.html", {
        "rid": rid, "role": role,
        "cid": cid or "", "cname": cname or "",
        "title": room.title,
    })


def _resolve_role(room: AuctionRoom, qp):
    """쿼리 토큰으로 역할 판별 → (role, cid, name)."""
    if qp.get("host") == room.host_token:
        return "host", None, None
    if qp.get("ob") == room.observer_token:
        return "observer", None, None
    cap_tok = qp.get("cap")
    if cap_tok:
        cap = room.get_captain_by_token(cap_tok)
        if cap:
            return "captain", cap.cid, cap.name
    return None, None, None


# ============ WebSocket ============
@app.websocket("/ws/{rid}")
async def ws_room(ws: WebSocket, rid: str):
    room = manager.get(rid)
    if not room:
        await ws.close(code=4404)
        return
    role, cid, _ = _resolve_role(room, ws.query_params)
    if role is None:
        await ws.close(code=4403)
        return

    await ws.accept()
    ws._role = role       # type: ignore[attr-defined]
    ws._cid = cid         # type: ignore[attr-defined]
    manager.conns[rid].add(ws)
    await ws.send_json({"type": "state", "state": room.snapshot(role, cid)})

    try:
        while True:
            msg = await ws.receive_json()
            await handle_message(room, role, cid, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.conns[rid].discard(ws)


async def handle_message(room: AuctionRoom, role: str, cid, msg: dict):
    action = msg.get("action")

    # 주최자 전용 액션
    if action in ("start", "skip", "reshuffle") and role != "host":
        return
    if action == "start":
        await room.start()
    elif action == "skip":
        await room.skip_current()
    elif action == "reshuffle":
        await room.reshuffle()
        await manager.broadcast(room.id)
    # 팀장 입찰
    elif action == "bid" and role == "captain":
        try:
            amount = int(msg.get("amount"))
        except (TypeError, ValueError):
            return
        ok, reason = await room.place_bid(cid, amount)
        # 입찰 결과는 해당 팀장에게만 개별 통지(에러 메시지)
        if not ok:
            for ws in manager.conns.get(room.id, ()):
                if getattr(ws, "_cid", None) == cid and getattr(ws, "_role", "") == "captain":
                    try:
                        await ws.send_json({"type": "notice", "ok": False, "msg": reason})
                    except Exception:
                        pass


@app.get("/api/auctions/{rid}")
async def get_state(rid: str):
    room = manager.get(rid)
    if not room:
        raise HTTPException(status_code=404, detail="경매를 찾을 수 없습니다.")
    return room.snapshot("observer")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.server:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), reload=False)
