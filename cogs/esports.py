"""e스포츠(롤) 공식 경기 자동 예측.

- 경기 시작 24시간 전에 승부예측을 자동 생성해 지정 채널에 게시
- 경기 시작 시각에 맞춰 자동 마감 (기존 predict 의 close_at 스케줄러가 처리)
- 경기가 끝나면 공식 결과로 자동 정산

일정 출처는 lolesports.com 이 내부적으로 쓰는 엔드포인트다. Riot 이 공식 문서로
제공하는 API 가 아니라 예고 없이 바뀔 수 있다. 실패하더라도 예측 기능 자체는
멀쩡하고 자동 게시만 멈춘다.
"""
import asyncio
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import aiosqlite

from .predict import settle_prediction
from .ui_predict import (PREDICT_DB_PATH, local_create_bet_session, local_get_bet_session,
                         local_set_bet_close_time, generate_bet_embed, BettingView)

# ───────── 설정 ─────────
ANNOUNCE_CH = 1530869657731338262        # e스포츠 예측 게시 채널

API_BASE = "https://esports-api.lolesports.com/persisted/gw"
API_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"   # lolesports.com 공개 키

# 게시 대상 리그 (이 목록에 없는 리그는 무시)
LEAGUES = {
    "98767991310872058": "LCK",
    "98767991325878492": "MSI",
    "98767975604431411": "월드 챔피언십",
}

POST_BEFORE_SEC = 24 * 3600     # 경기 시작 몇 초 전에 올릴지
CHECK_INTERVAL_MIN = 10         # 확인 주기(분)


async def init_esports_db():
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS esports_matches
                     (match_id TEXT PRIMARY KEY, topic TEXT, league TEXT,
                      start_time TEXT, team_a TEXT, team_b TEXT,
                      posted_at REAL, settled INTEGER DEFAULT 0)''')
        await db.commit()


async def is_posted(match_id: str) -> bool:
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        async with db.execute("SELECT 1 FROM esports_matches WHERE match_id = ?",
                              (match_id,)) as c:
            return await c.fetchone() is not None


async def mark_posted(match_id, topic, league, start_time, team_a, team_b):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO esports_matches
               (match_id, topic, league, start_time, team_a, team_b, posted_at, settled)
               VALUES (?,?,?,?,?,?,?,0)""",
            (match_id, topic, league, start_time, team_a, team_b, time.time()))
        await db.commit()


async def unsettled_matches():
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM esports_matches WHERE settled = 0") as c:
            return [dict(r) for r in await c.fetchall()]


async def mark_settled(match_id: str):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        await db.execute("UPDATE esports_matches SET settled = 1 WHERE match_id = ?",
                         (match_id,))
        await db.commit()


def _parse_iso(s: str) -> float:
    """API 의 ISO8601(Z) 시각을 epoch 초로."""
    from datetime import datetime, timezone
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(
        tzinfo=timezone.utc).timestamp()


def _topic_for(league: str, a: str, b: str, start_epoch: float) -> str:
    """예측 주제. betting_sessions 의 PRIMARY KEY 라 겹치지 않게 시각을 넣는다."""
    from datetime import datetime, timedelta, timezone
    kst = datetime.fromtimestamp(start_epoch, tz=timezone(timedelta(hours=9)))
    return f"[{league}] {a} vs {b} ({kst:%m/%d %H:%M})"


class EsportsCog(commands.Cog):
    """공식 e스포츠 경기 자동 예측 (게시/마감/정산)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.esports_loop.change_interval(minutes=CHECK_INTERVAL_MIN)
        self.esports_loop.start()

    def cog_unload(self):
        self.esports_loop.cancel()

    async def _fetch_schedule(self, session: aiohttp.ClientSession) -> list:
        """대상 리그의 일정을 가져온다."""
        events = []
        for league_id in LEAGUES:
            url = f"{API_BASE}/getSchedule?hl=ko-KR&leagueId={league_id}"
            try:
                async with session.get(url, headers={"x-api-key": API_KEY},
                                       timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        print(f"🚨 [e스포츠] 일정 조회 실패 league={league_id} HTTP {r.status}")
                        continue
                    data = await r.json()
                events += (data.get("data", {}).get("schedule", {}).get("events") or [])
            except Exception as e:
                print(f"🚨 [e스포츠] 일정 요청 오류 league={league_id}: {e}")
        return events

    @staticmethod
    def _teams(ev) -> tuple:
        t = (ev.get("match") or {}).get("teams") or []
        if len(t) != 2:
            return None, None
        return t[0], t[1]

    # ----- 1) 24시간 전 자동 게시 -----
    async def _post_upcoming(self, events) -> int:
        now = time.time()
        posted = 0
        channel = self.bot.get_channel(ANNOUNCE_CH)
        if channel is None:
            print(f"🚨 [e스포츠] 게시 채널 {ANNOUNCE_CH} 을 찾을 수 없습니다")
            return 0

        for ev in events:
            if ev.get("state") != "unstarted":
                continue
            match = ev.get("match") or {}
            match_id = str(match.get("id") or "")
            if not match_id:
                continue
            a, b = self._teams(ev)
            if not a or not b:
                continue
            # 팀 미정(TBD)은 건너뛴다
            if "TBD" in (a["name"].upper(), b["name"].upper()):
                continue
            try:
                start = _parse_iso(ev["startTime"])
            except Exception:
                continue
            # 아직 24시간 전이 아니거나, 이미 시작한 경기는 제외
            if start - now > POST_BEFORE_SEC or start <= now:
                continue
            if await is_posted(match_id):
                continue

            league = LEAGUES.get(str((ev.get("league") or {}).get("id")),
                                 (ev.get("league") or {}).get("name", "e스포츠"))
            topic = _topic_for(league, a["name"], b["name"], start)
            if await local_get_bet_session(topic):
                await mark_posted(match_id, topic, league, ev["startTime"], a["name"], b["name"])
                continue

            try:
                logo_a, logo_b = a.get("image"), b.get("image")
                embed, gauge_files = await generate_bet_embed(
                    topic, a["name"], b["name"], "active", logo_a=logo_a, logo_b=logo_b)
                embed.set_footer(text=f"{league} · 경기 시작 시각에 자동 마감됩니다")
                view = BettingView(topic, a["name"], b["name"])
                msg = await channel.send(embed=embed, view=view, files=gauge_files)

                await local_create_bet_session(topic, a["name"], b["name"], msg.id, channel.id,
                                               logo_a=logo_a, logo_b=logo_b)
                # 경기 시작 시각에 기존 마감 스케줄러가 자동으로 닫아준다.
                await local_set_bet_close_time(topic, start)
                await mark_posted(match_id, topic, league, ev["startTime"],
                                  a["name"], b["name"])
                posted += 1
                await asyncio.sleep(1)
            except Exception as e:
                print(f"🚨 [e스포츠] 게시 실패 {topic}: {e}")
        return posted

    # ----- 2) 종료된 경기 자동 정산 -----
    async def _settle_finished(self, events) -> int:
        pending = {m["match_id"]: m for m in await unsettled_matches()}
        if not pending:
            return 0
        done = 0
        for ev in events:
            if ev.get("state") != "completed":
                continue
            match = ev.get("match") or {}
            match_id = str(match.get("id") or "")
            row = pending.get(match_id)
            if not row:
                continue
            a, b = self._teams(ev)
            if not a or not b:
                continue
            outcome_a = (a.get("result") or {}).get("outcome")
            outcome_b = (b.get("result") or {}).get("outcome")
            if outcome_a not in ("win", "loss") or outcome_b not in ("win", "loss"):
                continue    # 아직 결과가 확정되지 않음

            topic = row["topic"]
            session = await local_get_bet_session(topic)
            if not session:
                await mark_settled(match_id)
                continue
            if session["status"] in ("finished", "cancelled"):
                await mark_settled(match_id)
                continue

            # 예측 생성 시 A=첫 팀 이었으므로 이름으로 승리 옵션을 판별한다.
            win_name = a["name"] if outcome_a == "win" else b["name"]
            win_opt = "A" if session["option_a"] == win_name else "B"

            try:
                res = await settle_prediction(topic, win_opt)
                await mark_settled(match_id)
                done += 1
                await self._announce_result(row, session, win_name, res)
            except Exception as e:
                print(f"🚨 [e스포츠] 정산 실패 {topic}: {e}")
        return done

    async def _announce_result(self, row, session, win_name, res):
        channel = self.bot.get_channel(session["channel_id"]) or self.bot.get_channel(ANNOUNCE_CH)
        if channel is None:
            return
        from utils.stats import format_num
        if res["win_pool"] == 0:
            desc = "승리 팀에 베팅한 유저가 없어 배당금이 환수되었습니다."
        else:
            logs = [f"<@{uid}>: {format_num(r)}P (원금 {format_num(amt)}P)"
                    for uid, amt, r in res["payouts"][:20]]
            desc = (f"총 베팅 풀 `{format_num(res['total_pool'])}P` 중 5% 수수료를 제외한 "
                    f"`{format_num(res['distributable'])}P` 분배\n\n🎉 당첨자\n" +
                    ("\n".join(logs) if logs else "상금을 받은 유저가 없습니다."))
        embed = discord.Embed(title=f"🎊 [{row['league']}] 결과: {win_name} 승리!",
                              description=desc, color=discord.Color.gold())
        embed.set_footer(text=row["topic"])
        try:
            await channel.send(embed=embed)
            # 원본 예측 메시지도 정리
            msg = await channel.fetch_message(session["message_id"])
            closed, gauge_files = await generate_bet_embed(
                row["topic"], session["option_a"], session["option_b"], "closed",
                logo_a=session["logo_a"], logo_b=session["logo_b"])
            await msg.edit(embed=closed, view=None, attachments=gauge_files)
        except Exception as e:
            print(f"🚨 [e스포츠] 결과 안내 실패: {e}")

    @tasks.loop(minutes=CHECK_INTERVAL_MIN)
    async def esports_loop(self):
        try:
            async with aiohttp.ClientSession() as session:
                events = await self._fetch_schedule(session)
            if not events:
                return
            n = await self._post_upcoming(events)
            m = await self._settle_finished(events)
            if n:
                print(f"📢 [e스포츠] 예측 {n}건 자동 게시")
            if m:
                print(f"💰 [e스포츠] {m}건 자동 정산")
        except Exception as e:
            print(f"🚨 [e스포츠] 루프 오류: {e}")

    @esports_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
        await init_esports_db()

    # ----- 수동 확인 -----
    @app_commands.command(name="e스포츠확인",
                          description="[관리자] 지금 즉시 e스포츠 일정을 확인해 게시/정산합니다.")
    @app_commands.default_permissions(manage_guild=True)
    async def force_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with aiohttp.ClientSession() as session:
            events = await self._fetch_schedule(session)
        if not events:
            return await interaction.followup.send(
                "❌ 일정을 가져오지 못했습니다. (API 변경 가능성)", ephemeral=True)
        n = await self._post_upcoming(events)
        m = await self._settle_finished(events)
        upcoming = sum(1 for e in events if e.get("state") == "unstarted")
        await interaction.followup.send(
            f"✅ 확인 완료\n대상 리그: {', '.join(LEAGUES.values())}\n"
            f"예정 경기 {upcoming}건 · 신규 게시 {n}건 · 자동 정산 {m}건", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EsportsCog(bot))
