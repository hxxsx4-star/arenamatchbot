"""내전 규칙 안내 + 반응 역할 — 규칙 임베드에 ✅ 반응을 달면 내전 역할을 지급한다.

봇이 재시작해도 같은 메시지를 계속 쓰도록 메시지 ID를 공유 상태 파일에 저장한다.
"""
import json
import os

import discord
from discord.ext import commands

from utils.logs import is_target_guild, send_log_embed, ROLE_LOG_CH

RULES_CH_ID = 1527365919242715368
MATCH_ROLE_ID = 1527384539972898996
CHECK_EMOJI = "✅"

SHARED_DIR = os.environ.get("ARENA_SHARED_DIR", "/home/hxxsx4/shared_data")
STATE_PATH = os.path.join(SHARED_DIR, "rules_gate_state.json")


def _rules_embed() -> discord.Embed:
    e = discord.Embed(
        title="⚔️ 내전 참가 규칙",
        description="내전에 참여하기 전에 아래 내용을 꼭 읽어주세요.",
        color=discord.Color.blurple(),
    )
    e.add_field(
        name="📋 참가 방법",
        value="`/내전 진행`으로 모집이 시작되면 실행한 사람이 방장이 됩니다. "
              "인원이 다 차면 방장/내전매니저/관리자가 팀을 편성합니다.",
        inline=False,
    )
    e.add_field(
        name="🏆 포인트 보상",
        value="승리팀 **+140P** · 패배팀 **+60P** · MVP **+2,000P**\n"
              "(공동 MVP면 인원수로 나눠 지급됩니다)",
        inline=False,
    )
    e.add_field(
        name="🔒 결과 확정",
        value="결과 확정(포인트 지급)은 **💎 내전매니저** 또는 **서버 관리 권한** 보유자만 할 수 있습니다. "
              "방장이라도 결과는 확정할 수 없습니다 — 아무나 판을 만들어 포인트를 찍어내는 걸 막기 위함입니다.",
        inline=False,
    )
    e.add_field(
        name="⚠️ 매너 / 제재",
        value="고의 트롤, 무단 노쇼·불참 등은 경고 대상입니다. **경고 3회 누적 시 자동 서버 차단**됩니다.",
        inline=False,
    )
    e.add_field(
        name="✅ 동의하기",
        value="이 메시지에 체크(✅) 반응을 누르면 자동으로 내전 역할이 지급되어 모집에 참여할 수 있습니다.",
        inline=False,
    )
    return e


class RulesGateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self):
        try:
            os.makedirs(SHARED_DIR, exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            print(f"🚨 [내전규칙] 상태 저장 실패: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            if not is_target_guild(guild):
                continue
            await self._ensure_rules_message(guild)

    async def _ensure_rules_message(self, guild: discord.Guild):
        channel = guild.get_channel(RULES_CH_ID)
        if not channel:
            print(f"🚨 [내전규칙] 채널 {RULES_CH_ID} 를 찾을 수 없습니다.")
            return

        msg_id = self.state.get("rules_msg_id")
        message = None
        if msg_id:
            try:
                message = await channel.fetch_message(msg_id)
            except (discord.NotFound, discord.Forbidden):
                message = None

        if message is None:
            try:
                message = await channel.send(embed=_rules_embed())
            except discord.Forbidden:
                return
            self.state["rules_msg_id"] = message.id
            self._save_state()

        try:
            await message.add_reaction(CHECK_EMOJI)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.message_id != self.state.get("rules_msg_id"):
            return
        if payload.emoji.name != CHECK_EMOJI:
            return

        member = payload.member
        if member is None or member.bot:
            return
        if not is_target_guild(member.guild):
            return

        role = member.guild.get_role(MATCH_ROLE_ID)
        if not role or role in member.roles:
            return
        try:
            await member.add_roles(role, reason="내전 규칙 동의(체크 반응)")
        except discord.Forbidden:
            return

        await send_log_embed(
            self.bot, ROLE_LOG_CH,
            "✅ 내전 규칙 동의", f"{member.mention} 님이 규칙에 동의해 내전 역할을 받았습니다.",
            member, discord.Color.green(), guild=member.guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(RulesGateCog(bot))
