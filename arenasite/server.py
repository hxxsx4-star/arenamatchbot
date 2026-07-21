"""종합게임 아레나 - 내전 커뮤니티 사이트 (FastAPI).

전적 검색 · 소환사 명단 · 모집/참여(내기·사다리타기) · 기록실 · 대시보드.
pokeball-lol('몬스터 볼') 스타일을 참고한, 아레나 서버 전용 내전 허브.

실행:  arenamatchbot 리포 루트에서
    uvicorn arenasite.server:app --host 0.0.0.0 --port 8100
또는  python -m arenasite.server
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from arenasite import store, riot, tiericons

BASE = Path(__file__).resolve().parent
app = FastAPI(title="종합게임 아레나 · 내전")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

templates.env.globals["MODES"] = store.MODES
templates.env.globals["site_name"] = os.environ.get("SITE_NAME", "아레나")
templates.env.globals["tier_key"] = tiericons.tier_key

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
)
# 항상 관리자로 인정할 디스코드 ID(안전용 fallback). 비워둬도 됨.
ADMIN_IDS = set(filter(None, os.environ.get(
    "ADMIN_DISCORD_IDS", "1505506970361139210,1517544497817583739"
).replace(" ", "").split(",")))
# 이 서버(길드)에서 '서버 관리' 이상 권한을 가진 사람은 누구나 사이트 관리자.
GUILD_ID = int(os.environ.get("GUILD_ID", "1526593162645209188"))
# Discord 권한 비트: ADMINISTRATOR(0x8) | MANAGE_GUILD(0x20)
MANAGE_MASK = 0x8 | 0x20
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_INVITE = os.environ.get("DISCORD_INVITE_URL", "").strip()
DISCORD_API = "https://discord.com/api"


def _has_manage_perm(guilds: list) -> bool:
    """OAuth guilds 응답에서 대상 서버의 관리 권한 보유 여부를 판단."""
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
    return f"{base}/auth/callback"


def _is_admin(request: Request) -> bool:
    # 로그인 시 서버 관리 권한이 확인되면 session["admin"] 이 True. fallback 으로 고정 ID 도 인정.
    return bool(request.session.get("admin")) or str(request.session.get("uid", "")) in ADMIN_IDS


def _ctx(request: Request, **kw) -> dict:
    d = {
        "request": request,
        "is_admin": _is_admin(request),
        "uid": request.session.get("uid", ""),
        "uname": request.session.get("uname", ""),
        "avatar": request.session.get("avatar", ""),
        "discord_invite": DISCORD_INVITE,
        "riot_enabled": riot.enabled(),
    }
    d.update(kw)
    return d


def _require_admin(request: Request):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="관리자 로그인이 필요합니다.")


# ── 경매 서브앱 통합 (/auction) ──────────────────────────────
# 기존 경매 웹앱(web/server.py)을 아레나 사이트의 /auction 하위에 마운트한다.
# 도메인·포트·OAuth·관리자(ADMIN_IDS/SESSION_SECRET)를 그대로 공유한다.
try:
    from web.server import app as _auction_app, manager as _auction_manager
    app.mount("/auction", _auction_app)
    AUCTION_ON = True

    @app.on_event("startup")
    async def _boot_auction():
        # 마운트된 서브앱은 자체 startup 이벤트가 실행되지 않으므로 여기서 복구를 호출한다.
        try:
            await _auction_manager.load_all()
        except Exception as e:  # noqa: BLE001
            print(f"[경매 복구 실패] {e}")
except Exception as e:  # noqa: BLE001
    print(f"[경매 통합 건너뜀] {e}")
    AUCTION_ON = False

templates.env.globals["auction_on"] = AUCTION_ON


# ============ 페이지 ============
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "index.html",
                                      _ctx(request, dash=store.weekly_dashboard()))


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    q = (request.query_params.get("q") or "").strip()
    result = None
    live = None
    if q:
        result = store.summoner_stats(q)
        if riot.enabled():
            live = await riot.lookup(q)
    return templates.TemplateResponse(request, "search.html",
                                      _ctx(request, q=q, result=result, live=live))


@app.get("/summoners", response_class=HTMLResponse)
async def summoners_page(request: Request):
    return templates.TemplateResponse(request, "summoners.html",
                                      _ctx(request, summoners=store.list_summoners()))


@app.get("/records", response_class=HTMLResponse)
async def records_page(request: Request):
    mode = request.query_params.get("mode") or "normal"
    if mode not in store.MODES:
        mode = "normal"
    return templates.TemplateResponse(request, "records.html",
                                      _ctx(request, mode=mode,
                                           matches=store.list_matches(mode)))


@app.get("/betting", response_class=HTMLResponse)
async def betting_page(request: Request):
    cat = request.query_params.get("cat") or "lol"
    if cat not in ("lol", "tft"):
        cat = "lol"
    return templates.TemplateResponse(request, "betting.html",
                                      _ctx(request, cat=cat, bets=store.list_bets(cat)))


@app.get("/ladder", response_class=HTMLResponse)
async def ladder_page(request: Request):
    return templates.TemplateResponse(request, "ladder.html", _ctx(request))


@app.get("/me", response_class=HTMLResponse)
async def my_profile(request: Request):
    uid = request.session.get("uid")
    if not uid:
        # 미로그인 → 디스코드 로그인으로 (로그인 후 다시 /me)
        return RedirectResponse("/auth/login?next=/me")
    profile = store.user_profile(uid)
    # 등록된 롤 닉네임이 있으면 내전 전적도 함께
    match_stats = store.summoner_stats(profile["lol_nick"]) if profile["lol_nick"] else None
    live = None
    if profile["lol_nick"] and riot.enabled():
        live = await riot.lookup(profile["lol_nick"])
    return templates.TemplateResponse(request, "me.html",
                                      _ctx(request, profile=profile,
                                           match_stats=match_stats, live=live,
                                           is_member=bool(request.session.get("member"))))


# ============ 티어 엠블럼 이미지 ============
@app.get("/tiericon/{game}/{key}.png")
async def tier_icon(game: str, key: str):
    if game not in ("lol", "val"):
        raise HTTPException(404)
    path = await tiericons.get_icon_path(game, key)
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


# ============ API: 소환사 ============
@app.post("/api/summoners")
async def api_add_summoner(request: Request):
    _require_admin(request)
    body = await request.json()
    riot_id = (body.get("riot_id") or "").strip()
    if not riot_id or "#" not in riot_id:
        raise HTTPException(400, "라이엇 ID를 '이름#태그' 형식으로 입력하세요.")
    tier = (body.get("tier") or "").strip()
    position = (body.get("position") or "").strip()
    if not tier and riot.enabled():
        info = await riot.lookup(riot_id)
        if info and info.get("tier"):
            tier = f"{info.get('tier_ko', info['tier'])} {info.get('rank','')}".strip()
    rec = store.add_summoner(riot_id, tier=tier, position=position)
    return JSONResponse(rec)


@app.delete("/api/summoners/{sid}")
async def api_del_summoner(request: Request, sid: str):
    _require_admin(request)
    return {"ok": store.remove_summoner(sid)}


# ============ API: 경기 기록 ============
@app.post("/api/matches")
async def api_add_match(request: Request):
    _require_admin(request)
    body = await request.json()
    try:
        rec = store.add_match(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(rec)


@app.delete("/api/matches/{mid}")
async def api_del_match(request: Request, mid: str):
    _require_admin(request)
    return {"ok": store.remove_match(mid)}


# ============ API: 일정 ============
@app.post("/api/schedules")
async def api_add_schedule(request: Request):
    _require_admin(request)
    body = await request.json()
    date = (body.get("date") or "").strip()
    if not date:
        raise HTTPException(400, "날짜를 입력하세요.")
    rec = store.add_schedule(date, body.get("time", ""), body.get("title", "내전"),
                             body.get("mode", "normal"), body.get("note", ""))
    return JSONResponse(rec)


@app.delete("/api/schedules/{sid}")
async def api_del_schedule(request: Request, sid: str):
    _require_admin(request)
    return {"ok": store.remove_schedule(sid)}


# ============ API: 내기 ============
@app.post("/api/bets")
async def api_add_bet(request: Request):
    body = await request.json()
    try:
        rec = store.add_bet(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(rec)


@app.post("/api/bets/{bid}/join")
async def api_join_bet(request: Request, bid: str):
    body = await request.json()
    ok = store.join_bet(bid, (body.get("name") or "").strip())
    if not ok:
        raise HTTPException(400, "참가에 실패했습니다. 이름을 확인하세요.")
    return {"ok": True}


@app.delete("/api/bets/{bid}")
async def api_del_bet(request: Request, bid: str):
    _require_admin(request)
    return {"ok": store.remove_bet(bid)}


# ============ 관리자 (디스코드 OAuth) ============
@app.get("/auth/login")
async def auth_login(request: Request):
    if not _oauth_ready():
        return HTMLResponse(
            "<h3>OAuth 미설정</h3><p>DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET "
            "환경변수를 설정하세요.</p><a href='/'>← 홈</a>", status_code=503)
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    nxt = request.query_params.get("next", "")
    request.session["oauth_next"] = nxt if nxt.startswith("/") else ""
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
        raise HTTPException(503, "OAuth 미설정")
    if request.query_params.get("state") != request.session.get("oauth_state"):
        raise HTTPException(400, "잘못된 요청(state 불일치)")
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(400, "코드 없음")
    async with httpx.AsyncClient(timeout=10) as cli:
        tok = await cli.post(f"{DISCORD_API}/oauth2/token", data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(request),
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if tok.status_code != 200:
            raise HTTPException(400, "토큰 교환 실패")
        access = tok.json().get("access_token")
        auth_hdr = {"Authorization": f"Bearer {access}"}
        me = await cli.get(f"{DISCORD_API}/users/@me", headers=auth_hdr)
        if me.status_code != 200:
            raise HTTPException(400, "유저 조회 실패")
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
    return RedirectResponse(request.session.pop("oauth_next", "") or "/me")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("arenasite.server:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8100)), reload=False)
