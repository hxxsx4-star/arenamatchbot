import discord
from discord.ext import commands
from discord import app_commands
from typing import Dict, List, Tuple
import configparser
import traceback
import re
import urllib.parse

from utils.stats import add_points, load_stats, save_stats, ensure_user
from utils.logs import MATCH_LOG_CH, enqueue_embed


def _fow_search_url(nickname: str) -> str:
    """롤 닉네임(롤닉#태그)으로 FOW 전적 검색 URL 생성. #은 -로 치환."""
    term = nickname.replace("#", "-").strip()
    return "https://ss.fow.kr/find/" + urllib.parse.quote(term, safe="")

FORUM_CHANNEL_ID = 1519676788543066293
EXTRA_ADMIN_ROLE_ID = 1517543242466463775
MATCH_CLOSE_ROLE_ID = 1522224219138691184

_cfg = configparser.ConfigParser()
try: _cfg.read("config.ini", encoding="utf-8")
except: pass

def _get_id(section: str, key: str) -> int:
    try:
        val = _cfg.get(section, key, fallback="0")
        return int(val) if val.isdigit() else 0
    except: return 0

MATCH_ADMIN_ROLE_ID: int = _get_id("Match", "match_admin_role_id")
JOIN_KEYWORDS = {"ㅅ", "손", "son", "저요", "나", "참여"}

TIERS = {
    1508940023582691328: (1, "C"), 1508939965520674926: (2, "GM"), 1508939943861293167: (3, "M"),
    1508939927008841938: (4, "D"), 1508939899116716042: (5, "E"), 1508939841604423882: (6, "P"),
    1508939788210802699: (7, "G"), 1508939767084093500: (8, "S"), 1508939527954239578: (9, "B"),
    1508941153259749466: (10, "I"), 1508939280129724546: (11, "U")
}

LINES = {
    1508947587292729507: (1, "탑"), 1508947614597513327: (2, "정글"),
    1508947632138096680: (3, "미드"), 1508947648328110211: (4, "원딜"),
    1508947663129673728: (5, "서폿")
}

class MVPSelectView(discord.ui.View):
    def __init__(self, main_view: "MatchResultView", participants: List[discord.Member], voter: discord.Member | discord.User):
        super().__init__(timeout=120)
        self.main_view = main_view
        options = [
            discord.SelectOption(label=member.display_name[:100], value=str(member.id))
            for member in participants if member.id != voter.id
        ]
        if not options:
            options = [discord.SelectOption(label="투표할 대상이 없습니다.", value="0")]
        self.sel = discord.ui.Select(placeholder="가장 활약한 선수를 선택하세요!", options=options[:25])
        self.sel.callback = self.vote_callback
        self.add_item(self.sel)

    async def vote_callback(self, interaction: discord.Interaction):
        cand_id = int(self.sel.values[0])
        if cand_id == 0:
            await interaction.response.edit_message(content="❌ 투표할 수 있는 다른 참가자가 없습니다.", view=None)
            return
        self.main_view.votes[interaction.user.id] = cand_id
        cand_member = interaction.guild.get_member(cand_id)
        name = cand_member.display_name if cand_member else f"유저({cand_id})"
        await interaction.response.edit_message(content=f"✅ {name} 님에게 MVP 투표를 완료했습니다!", view=None)

class MatchResultView(discord.ui.View):
    def __init__(self, cog: "MatchCog", participants: List[discord.Member]):
        super().__init__(timeout=None)
        self.cog = cog
        self.participants = participants
        self.votes: Dict[int, int] = {}

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        traceback.print_exc()
        if not interaction.response.is_done():
            await interaction.response.send_message("오류가 발생했습니다.", ephemeral=True)

    @discord.ui.button(label="MVP 투표", style=discord.ButtonStyle.primary, emoji="👑")
    async def vote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        participant_ids = [m.id for m in self.participants]
        if interaction.user.id not in participant_ids and not self.cog._is_admin(interaction.user):
             await interaction.response.send_message("이번 내전 참여자만 투표할 수 있습니다.", ephemeral=True)
             return
        view = MVPSelectView(self, self.participants, interaction.user)
        await interaction.response.send_message("오늘의 MVP를 선택해주세요!", view=view, ephemeral=True)

    @discord.ui.button(label="투표 종료 및 지급 (관리자)", style=discord.ButtonStyle.success)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = self.cog._is_admin(interaction.user)
        has_special_role = isinstance(interaction.user, discord.Member) and any(r.id == MATCH_CLOSE_ROLE_ID for r in interaction.user.roles)

        if not (is_admin or has_special_role):
            await interaction.response.send_message("관리자 또는 지정된 역할만 투표를 종료할 수 있습니다.", ephemeral=True)
            return
        if not self.votes:
            await interaction.response.send_message("아직 아무도 투표하지 않았습니다.", ephemeral=True)
            return

        await interaction.response.defer()
        tally = {}
        for cand_id in self.votes.values():
            tally[cand_id] = tally.get(cand_id, 0) + 1

        max_votes = max(tally.values())
        mvp_ids = [uid for uid, v in tally.items() if v == max_votes]
        mentions = []

        participant_count = len(self.participants)
        reward_points = 200 if participant_count >= 20 else 50
        stats = await load_stats()

        for mvp_id in mvp_ids:
            await add_points(mvp_id, reward_points)
            rec = ensure_user(stats, str(mvp_id))
            rec["mvp_count"] = rec.get("mvp_count", 0) + 1
            mem = interaction.guild.get_member(mvp_id)
            mentions.append(mem.mention if mem else f"<@{mvp_id}>")

        await save_stats(stats)

        for child in self.children:
            child.disabled = True

        await interaction.edit_original_response(view=self)
        result_text = f"🎉 내전 MVP 투표 종료! 🎉\n최다 득표자: {', '.join(mentions)} ({max_votes}표)\n선정되신 분들께 `{reward_points} Point`가 성공적으로 지급되었습니다!"
        await interaction.channel.send(result_text)

class MatchCog(commands.Cog):
    match_group = app_commands.Group(name="내전", description="포럼 기반 내전 진행 명령어")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_admin(self, user: discord.Member | discord.User) -> bool:
        if isinstance(user, discord.Member):
            if user.guild_permissions.manage_guild: return True
            if MATCH_ADMIN_ROLE_ID and any(role.id == MATCH_ADMIN_ROLE_ID for role in user.roles): return True
            if EXTRA_ADMIN_ROLE_ID and any(role.id == EXTRA_ADMIN_ROLE_ID for role in user.roles): return True
        return False

    def _get_user_tier(self, member: discord.Member) -> Tuple[int, str]:
        highest_order = 99
        tier_name = "U"
        for role in member.roles:
            if role.id in TIERS:
                order, name = TIERS[role.id]
                if order < highest_order:
                    highest_order, tier_name = order, name
        return highest_order, tier_name

    def _get_user_lines(self, member: discord.Member) -> str:
        user_lines = []
        for role in member.roles:
            if role.id in LINES:
                user_lines.append(LINES[role.id])
        user_lines.sort(key=lambda x: x[0])
        if user_lines:
            line_names = ", ".join([name for _, name in user_lines])
            return line_names
        return "라인 없음"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if not isinstance(message.channel, discord.Thread) or message.channel.parent_id != FORUM_CHANNEL_ID: return

        if message.content.strip().lower() in JOIN_KEYWORDS:
            try: await message.add_reaction("✅")
            except: pass

    @match_group.command(name="진행", description="스레드 맨 위에서부터 참여한 선착순 n명을 티어순으로 보여주고 투표를 시작합니다.")
    @app_commands.describe(member_count="명단에 띄울 선착순 인원수를 입력하세요.")
    @app_commands.rename(member_count="인원수")
    async def start_match(self, interaction: discord.Interaction, member_count: int):
        if member_count <= 0:
            await interaction.response.send_message("인원수는 1명 이상이어야 합니다.", ephemeral=True)
            return

        await interaction.response.defer()

        if not isinstance(interaction.channel, discord.Thread) or interaction.channel.parent_id != FORUM_CHANNEL_ID:
            await interaction.followup.send("이 명령어는 지정된 내전 모집 게시글(스레드) 안에서만 사용할 수 있습니다.", ephemeral=True)
            return

        participants = []
        async for msg in interaction.channel.history(limit=1000, oldest_first=True):
            if msg.content.strip().lower() in JOIN_KEYWORDS:
                if msg.author.id not in participants:
                    participants.append(msg.author.id)
                    if len(participants) >= member_count:
                        break

        if not participants:
            await interaction.followup.send("현재 등록된 내전 참여자가 없습니다.", ephemeral=True)
            return

        member_list = []
        for uid in participants:
            member = interaction.guild.get_member(uid)
            if member:
                tier_order, tier_name = self._get_user_tier(member)
                member_list.append({"member": member, "order": tier_order, "tier_name": tier_name})

        member_list.sort(key=lambda x: x["order"])

        stats = await load_stats()
        for data in member_list:
            uid_str = str(data["member"].id)
            rec = ensure_user(stats, uid_str)
            rec["match_count"] = rec.get("match_count", 0) + 1
        await save_stats(stats)

        output_lines = []
        participant_members = []

        for data in member_list:
            member = data["member"]
            participant_members.append(member)

            raw_name = member.display_name
            match = re.match(r'^(\d{2})\s+(.+)\s+(남|여)\s*(.*)$', raw_name.strip())

            if match:
                nickname = match.group(2).strip()
            else:
                temp = raw_name[2:].lstrip() if len(raw_name) >= 2 and raw_name[:2].isdigit() else raw_name
                nickname = re.sub(r'\s+(남|여)[^#]*$', '', temp).strip()
                if not nickname:
                    nickname = raw_name

            tier_name = data['tier_name']
            uid_str = str(member.id)
            rec = ensure_user(stats, uid_str)

            if "registered_lines" in rec and rec["registered_lines"]:
                line_text = ", ".join(rec["registered_lines"])
            else:
                line_text = self._get_user_lines(member)

            fow_url = _fow_search_url(nickname)
            output_lines.append(f"[{tier_name}] {nickname} / {line_text} ([전적]({fow_url}))")

        description_text = "\n".join(output_lines)
        if len(description_text) > 4000:
            description_text = description_text[:4000] + "\n\n... (텍스트 길이 제한으로 생략됨)"

        embed = discord.Embed(title=f"🎮 내전 참가자 명단 (선착순 {len(member_list)}명 / 티어순)", description=description_text, color=discord.Color.blue())
        embed.set_footer(text=f"요청 인원: {member_count}명 | 실제 확인된 인원: {len(member_list)}명")

        if len(participants) >= 20:
            auction_msg = (
                "🚨 내전 참가자가 20인 이상입니다!\n"
                "인원이 많아 경매 내전 모드로 진행하는 것을 권장합니다.\n"
                "👉 관리자님은 `/경매 팀장` 명령어로 팀장을 뽑은 뒤 `/경매 시작`을 진행해주세요!"
            )
            embed.add_field(name="💡 다인원 경매 진행 안내", value=auction_msg, inline=False)

        view = MatchResultView(self, participant_members)
        await interaction.followup.send(embed=embed, view=view)

        log_embed = discord.Embed(
            title="📝 내전 시작 로그",
            description=f"진행 스레드: {interaction.channel.mention}\n\n[참가자 명단]\n" + description_text,
            color=discord.Color.green()
        )
        log_embed.set_footer(text=f"선착순 {len(member_list)}명 참여")

        # 로그는 직접 올리지 않고 공유 큐에 적재 → 로그봇이 내전 로그 채널에 기록 (대상 서버만)
        enqueue_embed(MATCH_LOG_CH, log_embed.to_dict(), guild=interaction.guild)

async def setup(bot: commands.Bot):
    await bot.add_cog(MatchCog(bot))