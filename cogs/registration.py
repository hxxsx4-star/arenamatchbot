import re

import discord
from discord.ext import commands
from discord import app_commands

from utils.stats import set_nickname, get_nickname, set_lanes, get_lanes

# 티어 조정관 역할
LOL_TIER_MANAGER_ROLE = 1526678260182683668
VAL_TIER_MANAGER_ROLE = 1527320282824441856

GAME_CHOICES = [
    app_commands.Choice(name="리그오브레전드", value="lol"),
    app_commands.Choice(name="발로란트", value="val"),
]
LANE_CHOICES = [
    app_commands.Choice(name="탑", value="탑"),
    app_commands.Choice(name="정글", value="정글"),
    app_commands.Choice(name="미드", value="미드"),
    app_commands.Choice(name="원딜", value="원딜"),
    app_commands.Choice(name="서포터", value="서포터"),
]
_NICK_RE = re.compile(r"^.+#.+$")  # 닉네임#태그 형식


async def _add_site_roster(nick: str, member: discord.Member):
    """웹사이트 소환사 명단에 자동 등록 (티어는 라이엇 연동 시 자동, 실패해도 무시)."""
    try:
        import asyncio
        from arenasite import store as site_store
        tier = ""
        try:
            from arenasite import riot as site_riot
            if site_riot.enabled():
                info = await site_riot.lookup(nick)
                if info and info.get("tier"):
                    tier = f"{info.get('tier_ko', info['tier'])} {info.get('rank', '')}".strip()
        except Exception:
            pass
        await asyncio.to_thread(
            site_store.add_summoner, nick, tier, str(member.id), "",
            str(member.display_avatar.url))
    except Exception as e:
        print(f"[사이트 명단] 등록 실패 ({nick}): {e}")


def _can_register_nick(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    role_ids = {r.id for r in member.roles}
    return LOL_TIER_MANAGER_ROLE in role_ids or VAL_TIER_MANAGER_ROLE in role_ids


class RegistrationCog(commands.Cog):
    """닉네임/라인 등록 (내전용)."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    lol_group = app_commands.Group(name="롤", description="롤 관련 명령어")

    @app_commands.command(name="닉네임등록", description="[관리자/티어조정관] 유저의 게임 닉네임을 등록합니다.")
    @app_commands.describe(유저="대상 유저", 게임종류="리그오브레전드/발로란트", 닉네임="닉네임#태그 형식")
    @app_commands.choices(게임종류=GAME_CHOICES)
    async def register_nick(self, interaction: discord.Interaction, 유저: discord.Member,
                            게임종류: app_commands.Choice[str], 닉네임: str):
        if not _can_register_nick(interaction.user):
            return await interaction.response.send_message(
                "❌ 관리자 또는 티어 조정관만 사용할 수 있습니다.", ephemeral=True)
        nick = 닉네임.strip()
        if not _NICK_RE.match(nick):
            return await interaction.response.send_message(
                "❌ 닉네임 형식이 올바르지 않습니다. `닉네임#태그` 형식으로 입력하세요. (예: 홍길동#KR1)",
                ephemeral=True)
        await set_nickname(유저.id, 게임종류.value, nick)
        if 게임종류.value == "lol":
            await _add_site_roster(nick, 유저)
        game_name = "롤" if 게임종류.value == "lol" else "발로란트"
        await interaction.response.send_message(
            f"✅ {유저.mention} 님의 **{game_name}** 닉네임을 `{nick}` (으)로 등록했습니다.", ephemeral=True)

    @lol_group.command(name="라인등록", description="자신의 롤 주라인/부라인을 등록합니다.")
    @app_commands.describe(주라인="주 라인 (필수)", 부라인="부 라인 (필수)")
    @app_commands.choices(주라인=LANE_CHOICES, 부라인=LANE_CHOICES)
    async def register_lane(self, interaction: discord.Interaction,
                            주라인: app_commands.Choice[str], 부라인: app_commands.Choice[str]):
        await set_lanes(interaction.user.id, 주라인.value, 부라인.value)
        await interaction.response.send_message(
            f"✅ 라인을 등록했습니다. 주라인: **{주라인.value}** / 부라인: **{부라인.value}**", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RegistrationCog(bot))
