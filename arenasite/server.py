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

from arenasite import store, riot, tiericons, champions, pokedex, pokeadmin

BASE = Path(__file__).resolve().parent
app = FastAPI(title="종합게임 아레나 · 내전")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

templates.env.globals["MODES"] = store.MODES
templates.env.globals["site_name"] = os.environ.get("SITE_NAME", "아레나")
templates.env.globals["tier_key"] = tiericons.tier_key
templates.env.globals["champ_icon"] = champions.icon_url
templates.env.globals["profile_icon_url"] = lambda i: (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    f"global/default/v1/profile-icons/{i}.jpg")

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
)
# 항상 관리자로 인정할 디스코드 ID(안전용 fallback). 비워둬도 됨.
ADMIN_IDS = set(filter(None, os.environ.get(
    "ADMIN_DISCORD_IDS",
    "1505506970361139210,1517544497817583739,1077647513114394724,"
    "1474783486643539978,1526223759634202676"
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
    await champions.ensure()
    q = (request.query_params.get("q") or "").strip()
    result = None
    live = None
    roster = None
    if q:
        result = store.summoner_stats(q)
        roster = store.find_summoner(q)  # 디스코드 아바타 폴백용
        if riot.enabled():
            live = await riot.lookup(q)
    return templates.TemplateResponse(request, "search.html",
                                      _ctx(request, q=q, result=result, live=live,
                                           roster=roster))


@app.get("/summoners", response_class=HTMLResponse)
async def summoners_page(request: Request):
    return templates.TemplateResponse(request, "summoners.html",
                                      _ctx(request, summoners=store.list_summoners(),
                                           verified_ids=store.verified_riot_ids()))


@app.get("/records", response_class=HTMLResponse)
async def records_page(request: Request):
    await champions.ensure()
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


def _require_admin(request: Request):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="관리자만 접근할 수 있습니다.")


@app.get("/admin/pokemon", response_class=HTMLResponse)
async def pokemon_admin(request: Request):
    """포켓몬 관리자 페이지 — 유저 아이템 관리 + 전설 소환."""
    _require_admin(request)
    q = (request.query_params.get("q") or "").strip()
    return templates.TemplateResponse(request, "pokeadmin.html", _ctx(
        request,
        ready=pokeadmin.available(),
        trainers=pokeadmin.list_trainers(),
        ball_names=pokeadmin.BALL_NAMES,
        requests=pokeadmin.recent_requests(),
        q=q,
        results=pokeadmin.search_species(q, only_legendary=True) if q else [],
    ))


@app.post("/api/admin/pokemon/item")
async def pokemon_admin_item(request: Request):
    """유저 아이템 개수 변경."""
    _require_admin(request)
    form = await request.form()
    try:
        uid = int(form.get("uid"))
        amount = int(form.get("amount"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="잘못된 입력입니다.")
    item = (form.get("item") or "").strip()
    if not item:
        raise HTTPException(status_code=400, detail="아이템을 지정하세요.")
    final = pokeadmin.set_item(uid, item, amount)
    return JSONResponse({"ok": True, "uid": uid, "item": item, "amount": final})


@app.post("/api/admin/pokemon/summon")
async def pokemon_admin_summon(request: Request):
    """전설/환상 소환을 봇에 요청."""
    _require_admin(request)
    form = await request.form()
    name = (form.get("name") or "").strip()
    actor = f"{request.session.get('uname', '?')}({request.session.get('uid', '?')})"
    ok = pokeadmin.request_summon(name, actor)
    return JSONResponse({"ok": ok,
                         "message": ("소환 요청을 보냈습니다. 곧 스폰 채널에 등장합니다."
                                     if ok else "요청에 실패했습니다.")})


@app.get("/pokecenter", response_class=HTMLResponse)
async def pokecenter_page(request: Request):
    """포켓몬 센터 — 펫봇 도감/포획 현황을 웹에서 열람."""
    q = (request.query_params.get("q") or "").strip()
    rarity = request.query_params.get("rarity") or ""
    try:
        page = max(0, int(request.query_params.get("page") or 0))
    except ValueError:
        page = 0

    rows, total = pokedex.list_species(q, rarity, page)
    uid = request.session.get("uid")
    return templates.TemplateResponse(request, "pokecenter.html", _ctx(
        request,
        ready=pokedex.available(),
        summary=pokedex.summary(),
        species=rows,
        total=total,
        page=page,
        page_size=pokedex.PAGE_SIZE,
        pages=max(1, (total + pokedex.PAGE_SIZE - 1) // pokedex.PAGE_SIZE),
        q=q,
        rarity=rarity,
        rarities=pokedex.RARITY_ORDER,
        balls=pokedex.BALLS,
        ranking=pokedex.trainer_ranking(),
        recent=pokedex.recent_catches(),
        my_box=pokedex.user_box(int(uid)) if uid else [],
    ))


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
                                           verified=store.get_verified(uid),
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


# ============ 라이엇 계정 본인 인증 (아이콘 인증) ============
VERIFY_TTL = 600  # 인증 제한시간 10분
# 아이콘 이미지 표시용 CDN (브라우저에서 직접 로드)
ICON_CDN = ("https://raw.communitydragon.org/latest/plugins/"
            "rcp-be-lol-game-data/global/default/v1/profile-icons/{id}.jpg")


@app.post("/api/verify/start")
async def verify_start(request: Request):
    """인증 시작: 계정 확인 후 변경할 아이콘을 랜덤 지정."""
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(401, "디스코드 로그인이 필요합니다.")
    if not riot.enabled():
        raise HTTPException(503, "라이엇 연동(RIOT_API_KEY)이 설정되지 않았습니다.")
    body = await request.json()
    riot_id = (body.get("riot_id") or "").strip()
    if "#" not in riot_id:
        raise HTTPException(400, "라이엇 ID를 '이름#태그' 형식으로 입력하세요.")
    puuid = await riot.get_puuid(riot_id)
    if not puuid:
        raise HTTPException(404, "해당 라이엇 계정을 찾을 수 없습니다. ID를 확인하세요.")
    summ = await riot.get_summoner(puuid)
    current_icon = (summ or {}).get("profileIconId", -1)
    import random
    # 기본 제공(스타터) 아이콘 0~27 중 현재와 다른 것 지정
    candidates = [i for i in range(28) if i != current_icon]
    icon_id = random.choice(candidates)
    request.session["verify"] = {
        "riot_id": riot_id, "puuid": puuid, "icon": icon_id,
        "exp": int(__import__("time").time()) + VERIFY_TTL,
    }
    return {"icon_id": icon_id, "icon_url": ICON_CDN.format(id=icon_id),
            "expires_in": VERIFY_TTL}


@app.post("/api/verify/check")
async def verify_check(request: Request):
    """인증 확인: 현재 프로필 아이콘이 지정 아이콘과 일치하면 본인 인증."""
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(401, "디스코드 로그인이 필요합니다.")
    ch = request.session.get("verify")
    if not ch:
        raise HTTPException(400, "진행 중인 인증이 없습니다. 먼저 인증을 시작하세요.")
    import time as _t
    if _t.time() > ch.get("exp", 0):
        request.session.pop("verify", None)
        raise HTTPException(400, "인증 시간이 만료되었습니다. 다시 시작해주세요.")
    summ = await riot.get_summoner(ch["puuid"])
    if not summ:
        raise HTTPException(502, "라이엇 조회에 실패했습니다. 잠시 후 다시 시도하세요.")
    if summ.get("profileIconId") != ch["icon"]:
        raise HTTPException(400, "아이콘이 아직 변경되지 않았습니다. 클라이언트에서 변경 후 잠시 뒤 다시 확인하세요.")
    store.set_verified(uid, ch["riot_id"], ch["puuid"])
    # 인증된 계정은 소환사 명단에 자동 등록(실제 티어 조회)
    tier = ""
    info = await riot.lookup(ch["riot_id"])
    if info and info.get("tier"):
        tier = f"{info.get('tier_ko', info['tier'])} {info.get('rank', '')}".strip()
    store.add_summoner(ch["riot_id"], tier=tier, discord_id=str(uid),
                       avatar=request.session.get("avatar", ""))
    request.session.pop("verify", None)
    return {"ok": True, "riot_id": ch["riot_id"]}


@app.delete("/api/verify")
async def verify_unlink(request: Request):
    """본인 인증 해제."""
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(401, "디스코드 로그인이 필요합니다.")
    return {"ok": store.clear_verified(uid)}


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
