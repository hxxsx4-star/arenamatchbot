"""내전 전적 조회 (/내전전적) — 웹 기록실 데이터를 디스코드에서 바로 확인."""
import os
import asyncio
from urllib.parse import quote

import discord
from discord.ext import commands
from discord import app_commands

from utils.stats import get_nickname
from arenasite import store as site_store

SITE_URL = os.environ.get("PUBLIC_BASE_URL", "https://arenamatch.p-e.kr").rstrip("/")


def _fmt(st: dict) -> str:
    if not st["games"]:
        return "기록 없음"
    return (f"**{st['games']}판 {st['wins']}승 {st['losses']}패** (승률 {st['winrate']}%)\n"
            f"KDA **{st['kda']}** ({st['avg_k']}/{st['avg_d']}/{st['avg_a']})")


class StatsLookupCog(commands.Cog):
    """내전 전적만 따로 조회하는 명령어."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="내전전적", description="내전 전적을 조회합니다. (등록된 롤 닉네임 기준)")
    @app_commands.describe(유저="조회할 유저 (미입력 시 본인)")
    async def scrim_stats(self, interaction: discord.Interaction, 유저: discord.Member = None):
        await interaction.response.defer()
        target = 유저 or interaction.user
        nick = await get_nickname(target.id, "lol")
        if not nick:
            return await interaction.followup.send(
                f"❌ {target.mention} 님은 등록된 롤 닉네임이 없습니다. "
                "온보딩 DM 또는 `/닉네임등록`으로 먼저 등록해주세요.")

        total = await asyncio.to_thread(site_store.summoner_stats, nick)
        if not total["games"]:
            return await interaction.followup.send(
                f"📭 `{nick}` 님의 내전 기록이 아직 없습니다. 내전에 참여하면 자동으로 기록돼요!")
        normal = await asyncio.to_thread(site_store.summoner_stats, nick, "normal")
        aram = await asyncio.to_thread(site_store.summoner_stats, nick, "aram")

        e = discord.Embed(
            title=f"⚔️ 내전 전적 — {nick}",
            description=_fmt(total)
            + (f"\n👑 MVP **{total['mvp']}회**" if total.get("mvp") else ""),
            color=discord.Color.blurple(),
        )
        e.set_thumbnail(url=target.display_avatar.url)
        if normal["games"]:
            e.add_field(name="🗺️ 일반 내전", value=_fmt(normal), inline=True)
        if aram["games"]:
            e.add_field(name="❄️ 칼바람 내전", value=_fmt(aram), inline=True)

        if total.get("champions"):
            most = total["champions"][:3]
            e.add_field(
                name="🏅 MOST 챔피언",
                value="\n".join(
                    f"- **{c['champion']}** · {c['games']}판 승률 {c['winrate']}% KDA {c['kda']}"
                    for c in most),
                inline=False)

        if total.get("recent"):
            lines = []
            for r in total["recent"][:5]:
                w = "🟦승" if r["win"] else "🟥패"
                champ = r["champion"] or "미상"
                lines.append(f"{w} · {champ} `{r['kills']}/{r['deaths']}/{r['assists']}`"
                             + (" 👑" if r.get("mvp") else "") + f" · {r['date']}")
            e.add_field(name="🕒 최근 경기", value="\n".join(lines), inline=False)

        e.add_field(name="🔗 상세 전적",
                    value=f"[웹사이트에서 보기]({SITE_URL}/search?q={quote(nick)})",
                    inline=False)
        e.set_footer(text="내전 기록실 기준 · 솔랭 전적과는 별개입니다")
        await interaction.followup.send(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsLookupCog(bot))
