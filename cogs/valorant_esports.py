"""e스포츠(발로란트 VCT) 자동 예측 게시.

- 경기 시작 24시간 전에 승부예측을 자동 생성해 지정 채널에 게시
- 경기 시작 시각에 맞춰 자동 마감 (predict 의 close_at 스케줄러가 처리)
- 결과 확정(정산)은 자동화하지 않는다 — 관리자가 /예측결과 로 직접 처리

라이엇이 발로란트는 롤과 달리 공식 일정 API를 공개하지 않는다. 그래서 커뮤니티가
vlr.gg(팬사이트)를 스크래핑해 만든 비공식 API(vlr-api.vercel.app)를 쓴다.
개인이 운영하는 무료 API라 롤 공식 API보다 안정성이 낮을 수 있다.

일정(upcoming_extended)에는 팀 로고가 없지만, 같은 API의 검색(v2/search)
엔드포인트에 팀 이름을 넣으면 로고 URL을 따로 찾을 수 있어 그걸로 배너를
채운다. 검색이 실패하거나 팀을 못 찾으면 기본 VS 패널로 조용히 대체된다.
실패해도 예측 기능 자체는 멀쩡하고 자동 게시만 멈춘다.

대상 대회: VCT 아메리카스/EMEA/퍼시픽 정규 시즌 + 챔피언스. (Game Changers,
VCL 등 하위 리그와 China 리그는 대상 아님 — 필요하면 _is_target_event 만
고치면 된다.)
"""
import re
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import aiosqlite

from .ui_predict import (PREDICT_DB_PATH, local_create_bet_session, local_get_bet_session,
                         local_set_bet_close_time, generate_bet_embed, BettingView)

ANNOUNCE_CH = 1530869657731338262        # 승부예측 채널 (롤 e스포츠와 동일)

API_BASE = "https://vlr-api.vercel.app"
API_PAGES = 3                            # upcoming_extended 페이지 수

POST_BEFORE_SEC = 24 * 3600
CHECK_INTERVAL_MIN = 10
KST = timezone(timedelta(hours=9))

_REGION_RE = re.compile(r"^VCT\s+\d{4}:\s*(Americas|EMEA|Pacific)\b", re.I)


def _short_label(event_name: str) -> str:
    m = _REGION_RE.match(event_name or "")
    if m:
        return f"VCT {m.group(1)}"
    if "champions" in (event_name or "").lower():
        return "VCT Champions"
    return "VAL"


def _is_target_event(event_name: str) -> bool:
    if _REGION_RE.match(event_name or ""):
        return True
    return "champions" in (event_name or "").lower()


_PLACEHOLDER_IMG = "vlr/tmp/vlr.png"   # 로고 없는 팀에 붙는 vlr.gg 기본 이미지 — 못 찾은 걸로 취급


async def _search_team_logo(session: aiohttp.ClientSession, team_name: str) -> str | None:
    """vlr.gg 팀 검색에서 이름이 가장 잘 맞는 팀의 로고 URL을 찾는다. 실패/미발견 시 None."""
    try:
        async with session.get(f"{API_BASE}/v2/search", params={"q": team_name},
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception as e:
        print(f"🚨 [발로e스포츠] 팀 로고 검색 실패 ({team_name}): {e}")
        return None

    teams = (((data.get("data") or {}).get("segments") or {}).get("results") or {}).get("teams") or []
    if not teams:
        return None

    exact = [t for t in teams if t.get("name", "").strip().lower() == team_name.strip().lower()]
    pick = (exact or teams)[0]
    img = pick.get("img")
    if not img or _PLACEHOLDER_IMG in img:
        return None
    return img


async def init_valorant_db():
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS valorant_posted
                     (match_id TEXT PRIMARY KEY, posted_at REAL)''')
        await db.commit()


async def is_posted(match_id: str) -> bool:
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        async with db.execute("SELECT 1 FROM valorant_posted WHERE match_id = ?", (match_id,)) as c:
            return await c.fetchone() is not None


async def mark_posted(match_id: str):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO valorant_posted (match_id, posted_at) VALUES (?, ?)",
            (match_id, time.time()))
        await db.commit()


def _parse_ts(ts: str) -> float:
    """vlr-api 의 'unix_timestamp' 필드는 실제로는 UTC 포맷 문자열이다 (이름과 다르게 epoch 아님)."""
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def _topic_for(label: str, team1: str, team2: str, start_epoch: float) -> str:
    kst = datetime.fromtimestamp(start_epoch, tz=KST)
    return f"[{label}] {team1} vs {team2} ({kst:%m/%d %H:%M})"


class ValorantEsportsCog(commands.Cog):
    """VCT 자동 예측 게시 (게시/마감 — 정산은 관리자가 /예측결과 로 수동)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.valorant_loop.change_interval(minutes=CHECK_INTERVAL_MIN)
        self.valorant_loop.start()

    def cog_unload(self):
        self.valorant_loop.cancel()

    async def _fetch_matches(self, session: aiohttp.ClientSession) -> list:
        url = f"{API_BASE}/match?q=upcoming_extended&num_pages={API_PAGES}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    print(f"🚨 [발로e스포츠] 일정 조회 실패 HTTP {r.status}")
                    return []
                data = await r.json()
        except Exception as e:
            print(f"🚨 [발로e스포츠] 일정 요청 오류: {e}")
            return []
        segments = (data.get("data") or {}).get("segments") or []
        return [s for s in segments if _is_target_event(s.get("match_event", ""))]

    async def _post_upcoming(self, matches: list) -> int:
        now = time.time()
        channel = self.bot.get_channel(ANNOUNCE_CH)
        if channel is None:
            print(f"🚨 [발로e스포츠] 게시 채널 {ANNOUNCE_CH} 을 찾을 수 없습니다")
            return 0

        posted = 0
        async with aiohttp.ClientSession() as logo_session:
            for m in matches:
                team1, team2 = m.get("team1"), m.get("team2")
                if not team1 or not team2 or "TBD" in (team1.upper(), team2.upper()):
                    continue
                match_id = (m.get("match_page") or "").lstrip("/")
                if not match_id or await is_posted(match_id):
                    continue
                try:
                    start = _parse_ts(m["unix_timestamp"])
                except Exception:
                    continue
                if start - now > POST_BEFORE_SEC or start <= now:
                    continue

                label = _short_label(m.get("match_event", ""))
                topic = _topic_for(label, team1, team2, start)
                if await local_get_bet_session(topic):
                    await mark_posted(match_id)
                    continue

                try:
                    logo_a = await _search_team_logo(logo_session, team1)
                    logo_b = await _search_team_logo(logo_session, team2)

                    embed, files = await generate_bet_embed(
                        topic, team1, team2, "active", logo_a=logo_a, logo_b=logo_b)
                    embed.set_footer(text=f"{m.get('match_event','VCT')} · 경기 시작 시각에 자동 마감됩니다")
                    view = BettingView(topic, team1, team2)
                    msg = await channel.send(embed=embed, view=view, files=files)

                    await local_create_bet_session(topic, team1, team2, msg.id, channel.id,
                                                   logo_a=logo_a, logo_b=logo_b)
                    await local_set_bet_close_time(topic, start)
                    await mark_posted(match_id)
                    posted += 1
                except Exception as e:
                    print(f"🚨 [발로e스포츠] 게시 실패 {topic}: {e}")
        return posted

    @tasks.loop(minutes=CHECK_INTERVAL_MIN)
    async def valorant_loop(self):
        try:
            async with aiohttp.ClientSession() as session:
                matches = await self._fetch_matches(session)
            if not matches:
                return
            n = await self._post_upcoming(matches)
            if n:
                print(f"📢 [발로e스포츠] 예측 {n}건 자동 게시")
        except Exception as e:
            print(f"🚨 [발로e스포츠] 루프 오류: {e}")

    @valorant_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
        await init_valorant_db()

    @app_commands.command(name="발로e스포츠확인",
                          description="[관리자] 지금 즉시 VCT 일정을 확인해 예측을 게시합니다.")
    @app_commands.default_permissions(manage_guild=True)
    async def force_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with aiohttp.ClientSession() as session:
            matches = await self._fetch_matches(session)
        if not matches:
            return await interaction.followup.send(
                "❌ 일정을 가져오지 못했습니다. (비공식 API 오류/변경 가능성)", ephemeral=True)
        n = await self._post_upcoming(matches)
        await interaction.followup.send(
            f"✅ 확인 완료\n대상: VCT 아메리카스/EMEA/퍼시픽/챔피언스\n"
            f"조회된 대상 경기 {len(matches)}건 · 신규 게시 {n}건\n"
            f"⚠️ 결과 확정은 자동화되어 있지 않습니다 — 경기가 끝나면 /예측결과 로 직접 처리해주세요.",
            ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ValorantEsportsCog(bot))
