"""종합게임 아레나 - 경매 웹 서버 (FastAPI + WebSocket).

실행:  arenamatchbot 리포 루트에서
    uvicorn web.server:app --host 0.0.0.0 --port 8000
또는  python -m web.server
"""
from __future__ import annotations

import os
import sys
import json
import secrets
import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

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
templates.env.globals["site_name"] = os.environ.get("SITE_NAME", "아레나")
templates.env.globals["discord_invite"] = os.environ.get("DISCORD_INVITE_URL", "").strip()

# 공개 URL (링크 생성용). 배포 시 환경변수로 지정.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# ---- 관리자(디스코드 OAuth) 설정 ----
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
)
# 관리자 디스코드 고유 ID (안전용 fallback). 서버 관리 권한자는 아래 GUILD 검사로 인정.
ADMIN_IDS = set(filter(None, os.environ.get(
    "ADMIN_DISCORD_IDS", "1505506970361139210,1517544497817583739"
).replace(" ", "").split(",")))
GUILD_ID = int(os.environ.get("GUILD_ID", "1526593162645209188"))
MANAGE_MASK = 0x8 | 0x20  # ADMINISTRATOR | MANAGE_GUILD
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_API = "https://discord.com/api"


def _has_manage_perm(guilds: list) -> bool:
    for g in guilds or []:
        if str(g.get("id")) == str(GUILD_ID):
            try:
                return bool(int(g.get("permissions", 0)) & MANAGE_MASK)
            except (TypeError, ValueError):
                return False
    return False


def _oauth_ready() -> bool:
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET)


def _redirect_uri(request: Request) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}{_p(request)}/auth/callback"


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("admin")) or str(request.session.get("uid", "")) in ADMIN_IDS


def _p(request: Request) -> str:
    """마운트 접두어(root_path). 단독 실행 시 '', /auction 밑에 마운트되면 '/auction'."""
    return request.scope.get("root_path", "")


def _abs(path: str, request: Request) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}{_p(request)}{path}"


# 경매 방을 디스크에 저장할 디렉터리 (재시작해도 복구되도록 공유 볼륨에 저장)
AUCTION_DIR = os.path.join(
    os.environ.get("ARENA_SHARED_DIR", "/home/hxxsx4/shared_data"), "auctions"
)


class RoomManager:
    def __init__(self):
        self.rooms: dict[str, AuctionRoom] = {}
        self.conns: dict[str, set[WebSocket]] = {}

    def _bind(self, room: AuctionRoom):
        room.bind(
            broadcast=lambda rid=room.id: self.broadcast(rid),
            on_finish=post_auction_result,
            save=self._persist,
        )

    def add(self, room: AuctionRoom):
        self.rooms[room.id] = room
        self.conns[room.id] = set()
        self._bind(room)
        self._persist(room)  # 생성 즉시 저장

    def _persist(self, room: AuctionRoom):
        """경매 상태를 파일에 원자적으로 저장."""
        try:
            os.makedirs(AUCTION_DIR, exist_ok=True)
            path = os.path.join(AUCTION_DIR, f"{room.id}.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(room.to_dict(), f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            print(f"[경매 저장 실패] {room.id}: {e}")

    async def load_all(self):
        """재시작 시 저장된 경매를 모두 복구하고 진행 중이던 것은 타이머 재개."""
        if not os.path.isdir(AUCTION_DIR):
            return
        loaded = 0
        for name in os.listdir(AUCTION_DIR):
            if not name.endswith(".json"):
                continue
            try:
                fpath = os.path.join(AUCTION_DIR, name)
                with open(fpath, encoding="utf-8") as f:
                    data = json.load(f)
                # 종료된 지 14일 지난 경매는 정리(파일 삭제)
                import time as _t
                if data.get("phase") == "finished" and (_t.time() - data.get("created_at", 0)) > 14 * 86400:
                    os.remove(fpath)
                    continue
                room = AuctionRoom.from_dict(data)
                self.rooms[room.id] = room
                self.conns[room.id] = set()
                self._bind(room)
                await room.resume()
                loaded += 1
            except Exception as e:
                print(f"[경매 복구 실패] {name}: {e}")
        if loaded:
            print(f"♻️ 저장된 경매 {loaded}개 복구 완료")

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


@app.on_event("startup")
async def _load_saved_auctions():
    await manager.load_all()


# ============ 페이지 ============
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"p": _p(request), "session_admin": _is_admin(request)})


@app.get("/create", response_class=HTMLResponse)
async def create_page(request: Request):
    return templates.TemplateResponse(request, "create.html", {"p": _p(request), "session_admin": _is_admin(request)})


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
        return templates.TemplateResponse(request, "gone.html", {"p": _p(request), "session_admin": _is_admin(request)}, status_code=404)
    role, cid, cname = _resolve_role(room, request.query_params)
    if role is None:
        return templates.TemplateResponse(request, "gone.html", {"p": _p(request), "session_admin": _is_admin(request)}, status_code=403)
    return templates.TemplateResponse(request, "room.html", {
        "p": _p(request), "session_admin": _is_admin(request), "rid": rid, "role": role,
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


# ============ 관리자 (디스코드 OAuth 로그인) ============
@app.get("/auth/login")
async def auth_login(request: Request):
    if not _oauth_ready():
        return templates.TemplateResponse(request, "admin.html",
                                          {"p": _p(request), "session_admin": _is_admin(request), "setup_needed": True, "admin": False})
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    from urllib.parse import urlencode
    q = urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
    })
    return RedirectResponse(f"{DISCORD_API}/oauth2/authorize?{q}")


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not _oauth_ready():
        raise HTTPException(status_code=503, detail="OAuth 미설정")
    if request.query_params.get("state") != request.session.get("oauth_state"):
        raise HTTPException(status_code=400, detail="잘못된 요청(state 불일치)")
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="코드 없음")
    async with httpx.AsyncClient(timeout=10) as cli:
        tok = await cli.post(f"{DISCORD_API}/oauth2/token", data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(request),
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if tok.status_code != 200:
            raise HTTPException(status_code=400, detail="토큰 교환 실패")
        access = tok.json().get("access_token")
        auth_hdr = {"Authorization": f"Bearer {access}"}
        me = await cli.get(f"{DISCORD_API}/users/@me", headers=auth_hdr)
        if me.status_code != 200:
            raise HTTPException(status_code=400, detail="유저 조회 실패")
        guilds_resp = await cli.get(f"{DISCORD_API}/users/@me/guilds", headers=auth_hdr)
    user = me.json()
    uid = str(user.get("id"))
    guilds = guilds_resp.json() if guilds_resp.status_code == 200 else []
    # 누구나 로그인 가능. 서버 관리 권한자(또는 고정 ID)만 관리자 플래그.
    request.session["uid"] = uid
    request.session["uname"] = user.get("global_name") or user.get("username", "")
    request.session["admin"] = _has_manage_perm(guilds) or uid in ADMIN_IDS
    request.session["member"] = any(str(g.get("id")) == str(GUILD_ID) for g in guilds)
    avatar_hash = user.get("avatar")
    if avatar_hash:
        request.session["avatar"] = f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png?size=128"
    else:
        request.session["avatar"] = f"https://cdn.discordapp.com/embed/avatars/{(int(uid) >> 22) % 6}.png"
    if request.session["admin"]:
        return RedirectResponse(f"{_p(request)}/admin")
    return RedirectResponse(f"{_p(request)}/")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not _oauth_ready():
        return templates.TemplateResponse(request, "admin.html",
                                          {"p": _p(request), "session_admin": _is_admin(request), "setup_needed": True, "admin": False})
    if not _is_admin(request):
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse(request, "admin.html",
                                      {"p": _p(request), "session_admin": _is_admin(request), "admin": True, "uname": request.session.get("uname", "")})


@app.get("/api/admin/auctions")
async def admin_list(request: Request):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="관리자 전용")
    items = []
    for r in sorted(manager.rooms.values(), key=lambda x: x.created_at, reverse=True):
        done = sum(1 for m in r.members if m.won_by is not None)
        items.append({
            "id": r.id, "title": r.title, "phase": r.phase.value,
            "team_count": r.team_count, "members": len(r.members),
            "assigned": done, "created_at": r.created_at,
        })
    return {"auctions": items}


@app.get("/api/admin/auctions/{rid}/log")
async def admin_log(request: Request, rid: str):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="관리자 전용")
    room = manager.get(rid)
    if not room:
        raise HTTPException(status_code=404, detail="경매를 찾을 수 없습니다.")
    return {
        "title": room.title,
        "summary": room.snapshot("observer"),
        "events": room.event_log(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.server:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), reload=False)
