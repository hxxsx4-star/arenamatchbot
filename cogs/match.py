"""내전 진행 엔진.

- 포럼(FORUM_ID)에 "ㅅ/손/저요/나" 등 참여 표현을 쓰면 봇이 ✅ 반응
- /내전 진행 [게임종류] [인원수] : ✅ 표시된(참여 표현) 사람들을 스캔해 리스트 임베드로 표시
  · 명령 실행자 = 방장
  · 롤/발로란트는 팀짜기(내전 시작) 기능 제공
  · 매번 포럼을 새로 스캔하므로, 임베드를 지우고 다시 실행해도 인원이 그대로 나옴("오래됨" 없음)
- 롤: 닉네임/롤티어/주라인,부라인 · 발로: 발로닉네임/발로티어 로 표기
- 팀짜기: 팀장 선택(1팀→2팀) → 스네이크 드래프트(1·2·2·1·2·2·1) → 블루/레드 팀 이미지
- MVP 투표 + 1팀/2팀 승리(내전매니저/관리자) → 승리 +140P, 패배 +60P
- 여러 명이 동시에 눌러도 상호작용 실패가 안 나도록 락 + 항상 응답 처리
"""
import io
import re
import asyncio

import discord
from discord.ext import commands
from discord import app_commands

from utils.stats import add_points, get_nickname, get_lanes, format_num
from utils.tiers import get_tier
from utils.logs import MATCH_LOG_CH, enqueue_embed

# 내전 결과 발표 채널 (결과 이미지 + 요약을 공개 게시)
RESULT_ANNOUNCE_CH = 1528716794469027930

FORUM_ID = 1527367803676655697
MANAGER_ROLE = 1526678410124988526
WIN_POINTS, LOSE_POINTS = 140, 60

# 참여 표현 (정규화 후 정확히 일치)
TRIGGERS = {"ㅅ", "ㅅㅅ", "손", "저요", "나", "저", "ㄱ", "ㄱㄱ", "참여", "콜", "고", "ㅇㅋ", "ㅇ"}

GAME_CHOICES = [
    app_commands.Choice(name="리그오브레전드", value="lol"),
    app_commands.Choice(name="발로란트", value="val"),
    app_commands.Choice(name="오버워치", value="ow"),
    app_commands.Choice(name="배틀그라운드", value="pubg"),
    app_commands.Choice(name="구스구스덕", value="goose"),
    app_commands.Choice(name="기타게임", value="etc"),
]
GAME_LABELS = {"lol": "리그오브레전드", "val": "발로란트", "ow": "오버워치",
               "pubg": "배틀그라운드", "goose": "구스구스덕", "etc": "기타게임"}
TEAM_GAMES = {"lol", "val"}                 # 팀짜기 지원
DRAFT_PICKS = [1, 2, 2, 1, 1, 2, 2, 1]      # 팀장 선택 후 픽 순서 (팀 번호) → 팀1:5, 팀2:5

_norm_re = re.compile(r"[^가-힣ㄱ-ㅎa-zA-Z]")


def is_trigger(content: str) -> bool:
    if not content:
        return False
    norm = _norm_re.sub("", content).strip().lower()
    return norm in TRIGGERS


def can_manage(member: discord.Member, host_id: int) -> bool:
    """방장 / 내전매니저 / 서버 관리 권한이면 True."""
    if member.id == host_id:
        return True
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    return any(r.id == MANAGER_ROLE for r in member.roles)


async def player_line(member: discord.Member, game: str, idx: int) -> str:
    """리스트 임베드 한 줄 (게임별 표기)."""
    if game == "lol":
        nick = await get_nickname(member.id, "lol") or member.display_name
        tier = get_tier(member, "lol") or "?"
        main, sub = await get_lanes(member.id)
        return f"`{idx}.` {nick} / {tier} / {main or '-'},{sub or '-'}"
    if game == "val":
        nick = await get_nickname(member.id, "val") or member.display_name
        tier = get_tier(member, "val") or "?"
        return f"`{idx}.` {nick} / {tier}"
    return f"`{idx}.` {member.display_name}"


async def short_name(member: discord.Member, game: str) -> str:
    if game == "lol":
        nick = await get_nickname(member.id, "lol") or member.display_name
        tier = get_tier(member, "lol") or "?"
        return f"{nick} ({tier})"
    if game == "val":
        nick = await get_nickname(member.id, "val") or member.display_name
        tier = get_tier(member, "val") or "?"
        return f"{nick} ({tier})"
    return member.display_name


# ============ 팀 결과 이미지 ============
def _font(size, bold=True):
    from PIL import ImageFont
    try:
        return ImageFont.truetype("font.ttf" if bold else "font_ui.ttf", size)
    except OSError:
        return ImageFont.load_default()


def render_team_image(title, blue, red, mvp_name=None, winner=None):
    """blue/red = [(name, sub)] 리스트. winner: 1|2|None."""
    from PIL import Image, ImageDraw
    W, H = 1000, 620
    img = Image.new("RGB", (W, H), (18, 14, 32))
    d = ImageDraw.Draw(img)
    f_title, f_team, f_row, f_small = _font(46), _font(34), _font(28, False), _font(22, False)

    d.text((W // 2, 46), title, font=f_title, fill=(255, 255, 255), anchor="mm")

    cols = [(40, (90, 140, 255), "1팀 (블루)", blue, 1),
            (520, (245, 90, 110), "2팀 (레드)", red, 2)]
    for x, color, label, members, tno in cols:
        win = winner == tno
        d.rounded_rectangle((x, 100, x + 440, H - 40), radius=22,
                            fill=(30, 40, 70) if tno == 1 else (60, 28, 38),
                            outline=(70, 150, 255) if win else color, width=6 if win else 3)
        head = label + ("   WIN" if win else "")
        d.text((x + 24, 122), head, font=f_team, fill=color if not win else (90, 160, 255))
        y = 190
        for i, (name, sub) in enumerate(members, 1):
            d.text((x + 28, y), f"{i}. {name}", font=f_row, fill=(240, 240, 250))
            if sub:
                d.text((x + 28, y + 32), sub, font=f_small, fill=(170, 170, 200))
            y += 66
    if mvp_name:
        d.text((W // 2, H - 22), f"MVP: {mvp_name}", font=_font(26), fill=(255, 205, 90), anchor="mm")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf.getvalue()


# ============ 세션 ============
class MatchSession:
    def __init__(self, host: discord.Member, game: str, capacity: int, participants: list[discord.Member]):
        self.host_id = host.id
        self.guild = host.guild
        self.game = game
        self.capacity = capacity
        self.participants = participants          # discord.Member 리스트
        self.lock = asyncio.Lock()
        # 팀짜기 상태
        self.phase = "list"                       # list/captain1/captain2/draft/teams/done
        self.captains: list[int] = [0, 0]
        self.team1: list[int] = []
        self.team2: list[int] = []
        self.pool: list[int] = []
        self.draft_idx = 0
        self.mvp_votes: dict[int, int] = {}
        self.winner = None

    def member(self, uid: int):
        return self.guild.get_member(uid) or next((m for m in self.participants if m.id == uid), None)

    def in_match(self, uid: int) -> bool:
        return any(m.id == uid for m in self.participants)

    # ----- 팀짜기 로직 (순수) -----
    def start_draft(self):
        self.pool = [m.id for m in self.participants]
        self.team1, self.team2, self.captains = [], [], [0, 0]
        self.draft_idx = 0
        self.phase = "captain1"

    def set_captain(self, team: int, uid: int):
        self.captains[team - 1] = uid
        self.pool.remove(uid)
        (self.team1 if team == 1 else self.team2).append(uid)
        self.phase = "captain2" if team == 1 else "draft"

    def current_team(self) -> int:
        return DRAFT_PICKS[self.draft_idx]

    def current_captain(self) -> int:
        return self.captains[self.current_team() - 1]

    def pick(self, uid: int):
        team = self.current_team()
        self.pool.remove(uid)
        (self.team1 if team == 1 else self.team2).append(uid)
        self.draft_idx += 1
        self._auto_finish()

    def _auto_finish(self):
        # 마지막 1명은 선택지가 없으므로 자동 배정 (남은 인원이 1명일 때)
        if self.draft_idx < len(DRAFT_PICKS) and len(self.pool) == 1:
            team = DRAFT_PICKS[self.draft_idx]
            uid = self.pool.pop(0)
            (self.team1 if team == 1 else self.team2).append(uid)
            self.draft_idx += 1
        if self.draft_idx >= len(DRAFT_PICKS):
            self.phase = "teams"

    def finalize(self, winner: int):
        self.winner = winner
        self.phase = "done"
        win_ids = self.team1 if winner == 1 else self.team2
        lose_ids = self.team2 if winner == 1 else self.team1
        return win_ids, lose_ids

    def mvp(self):
        if not self.mvp_votes:
            return None
        tally: dict[int, int] = {}
        for t in self.mvp_votes.values():
            tally[t] = tally.get(t, 0) + 1
        return max(tally, key=tally.get)


# ============ Views ============
class ListView(discord.ui.View):
    """리스트 화면. 롤/발로면 '내전 시작' 버튼."""
    def __init__(self, cog: "MatchCog", session: MatchSession):
        super().__init__(timeout=None)
        self.cog = cog
        self.session = session
        if session.game not in TEAM_GAMES:
            self.remove_item(self.start_button)

    @discord.ui.button(label="내전 시작 (팀짜기)", style=discord.ButtonStyle.success, emoji="⚔️")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.session
        if not can_manage(interaction.user, s.host_id):
            return await interaction.response.send_message("❌ 방장/내전매니저/관리자만 시작할 수 있습니다.", ephemeral=True)
        async with s.lock:
            if s.phase != "list":
                return await interaction.response.send_message("이미 진행 중입니다.", ephemeral=True)
            if len(s.participants) != 10:
                return await interaction.response.send_message(
                    f"❌ 팀짜기는 정확히 10명이 필요합니다. (현재 {len(s.participants)}명)", ephemeral=True)
            s.start_draft()
        await interaction.response.edit_message(
            embed=self.cog.captain_embed(s, 1), view=CaptainView(self.cog, s, 1))


class CaptainView(discord.ui.View):
    def __init__(self, cog: "MatchCog", session: MatchSession, team: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.session = session
        self.team = team
        options = [discord.SelectOption(label=(session.member(uid).display_name if session.member(uid) else str(uid)),
                                        value=str(uid)) for uid in session.pool]
        self.select = discord.ui.Select(placeholder=f"{team}팀 팀장을 선택하세요", options=options[:25])
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        s = self.session
        if not can_manage(interaction.user, s.host_id):
            return await interaction.response.send_message("❌ 방장/내전매니저/관리자만 선택할 수 있습니다.", ephemeral=True)
        async with s.lock:
            expected = "captain1" if self.team == 1 else "captain2"
            if s.phase != expected:
                return await interaction.response.send_message("이미 처리되었습니다.", ephemeral=True)
            uid = int(self.select.values[0])
            if uid not in s.pool:
                return await interaction.response.send_message("이미 선택된 인원입니다.", ephemeral=True)
            s.set_captain(self.team, uid)
        if s.phase == "captain2":
            await interaction.response.edit_message(embed=self.cog.captain_embed(s, 2), view=CaptainView(self.cog, s, 2))
        else:
            await self.cog.show_draft(interaction, s)


class DraftView(discord.ui.View):
    def __init__(self, cog: "MatchCog", session: MatchSession):
        super().__init__(timeout=None)
        self.cog = cog
        self.session = session
        options = [discord.SelectOption(label=(session.member(uid).display_name if session.member(uid) else str(uid)),
                                        value=str(uid)) for uid in session.pool]
        team = session.current_team()
        self.select = discord.ui.Select(placeholder=f"{team}팀 팀장이 픽하세요", options=options[:25])
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        s = self.session
        allowed = interaction.user.id == s.current_captain() or can_manage(interaction.user, s.host_id)
        if not allowed:
            return await interaction.response.send_message("❌ 지금 픽할 차례의 팀장만 선택할 수 있습니다.", ephemeral=True)
        async with s.lock:
            if s.phase != "draft":
                return await interaction.response.send_message("드래프트가 이미 끝났습니다.", ephemeral=True)
            uid = int(self.select.values[0])
            if uid not in s.pool:
                return await interaction.response.send_message("이미 선택된 인원입니다.", ephemeral=True)
            s.pick(uid)
        if s.phase == "teams":
            await self.cog.show_teams(interaction, s)
        else:
            await self.cog.show_draft(interaction, s)


class ResultView(discord.ui.View):
    def __init__(self, cog: "MatchCog", session: MatchSession):
        super().__init__(timeout=None)
        self.cog = cog
        self.session = session

    @discord.ui.button(label="MVP 투표", style=discord.ButtonStyle.primary, emoji="🏅")
    async def mvp_vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.session
        if not s.in_match(interaction.user.id):
            return await interaction.response.send_message("❌ 참가자만 투표할 수 있습니다.", ephemeral=True)
        await interaction.response.send_message("MVP를 선택하세요.", view=MVPVoteView(self.cog, s, interaction.user.id), ephemeral=True)

    @discord.ui.button(label="1팀 승리", style=discord.ButtonStyle.secondary, emoji="🔵")
    async def win1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.declare_winner(interaction, self.session, 1)

    @discord.ui.button(label="2팀 승리", style=discord.ButtonStyle.secondary, emoji="🔴")
    async def win2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.declare_winner(interaction, self.session, 2)


class MVPVoteView(discord.ui.View):
    def __init__(self, cog: "MatchCog", session: MatchSession, voter_id: int):
        super().__init__(timeout=60)
        self.session = session
        self.voter_id = voter_id
        opts = [discord.SelectOption(label=(session.member(uid).display_name if session.member(uid) else str(uid)),
                                     value=str(uid)) for uid in (session.team1 + session.team2)]
        self.select = discord.ui.Select(placeholder="MVP 선택", options=opts[:25])
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        async with self.session.lock:
            self.session.mvp_votes[self.voter_id] = int(self.select.values[0])
        await interaction.response.edit_message(content="✅ MVP 투표가 반영되었습니다!", view=None)


class MatchCog(commands.Cog):
    """내전 진행 엔진."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    match_group = app_commands.Group(name="내전", description="내전 관련 명령어")

    # ----- 포럼 반응 -----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        ch = message.channel
        parent_id = getattr(ch, "parent_id", None)
        if parent_id != FORUM_ID and ch.id != FORUM_ID:
            return
        if is_trigger(message.content):
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass

    async def scan_participants(self, guild: discord.Guild) -> list[discord.Member]:
        """포럼의 활성 스레드에서 참여 표현을 남긴 사람들을 스캔(최신 실시간)."""
        forum = guild.get_channel(FORUM_ID)
        found: dict[int, discord.Member] = {}
        threads = []
        if forum is not None:
            threads = list(getattr(forum, "threads", []))
            try:
                async for th in forum.archived_threads(limit=10):
                    threads.append(th)
            except Exception:
                pass
        for th in threads:
            try:
                async for msg in th.history(limit=300):
                    if msg.author.bot or msg.author.id in found:
                        continue
                    if is_trigger(msg.content):
                        m = guild.get_member(msg.author.id)
                        if m:
                            found[m.id] = m
            except Exception:
                continue
        return list(found.values())

    # ----- 임베드 빌더 -----
    async def list_embed(self, s: MatchSession) -> discord.Embed:
        host = s.member(s.host_id)
        embed = discord.Embed(
            title=f"⚔️ 내전 모집 · {GAME_LABELS.get(s.game, s.game)}",
            description=f"방장: {host.mention if host else '?'} · 모집 인원: {s.capacity}명\n현재 참여: **{len(s.participants)}명**",
            color=discord.Color.blue() if s.game == "lol" else discord.Color.red() if s.game == "val" else discord.Color.blurple(),
        )
        lines = [await player_line(m, s.game, i) for i, m in enumerate(s.participants, 1)]
        embed.add_field(name="참여자", value="\n".join(lines) if lines else "아직 참여자가 없습니다.", inline=False)
        if s.game in TEAM_GAMES:
            embed.set_footer(text="정확히 10명이 되면 '내전 시작'으로 팀짜기를 진행하세요.")
        return embed

    def captain_embed(self, s: MatchSession, team: int) -> discord.Embed:
        return discord.Embed(
            title=f"👑 {team}팀 팀장 선택",
            description=f"{team}팀 팀장을 선택하세요. (방장/내전매니저/관리자)",
            color=discord.Color.gold())

    async def draft_embed(self, s: MatchSession) -> discord.Embed:
        team = s.current_team()
        cap = s.member(s.current_captain())
        e = discord.Embed(title="🧩 팀 드래프트",
                          description=f"이번 차례: **{team}팀** — 팀장 {cap.mention if cap else '?'}",
                          color=discord.Color.blue() if team == 1 else discord.Color.red())
        e.add_field(name="🔵 1팀", value="\n".join(f"- {(await short_name(s.member(u), s.game))}" for u in s.team1) or "-", inline=True)
        e.add_field(name="🔴 2팀", value="\n".join(f"- {(await short_name(s.member(u), s.game))}" for u in s.team2) or "-", inline=True)
        pool_names = ", ".join((s.member(u).display_name if s.member(u) else str(u)) for u in s.pool)
        e.add_field(name="남은 인원", value=pool_names or "-", inline=False)
        return e

    async def show_draft(self, interaction: discord.Interaction, s: MatchSession):
        await interaction.response.edit_message(embed=await self.draft_embed(s), view=DraftView(self, s))

    async def _team_rows(self, s: MatchSession, ids):
        rows = []
        for u in ids:
            m = s.member(u)
            if s.game == "lol":
                nick = await get_nickname(u, "lol") or (m.display_name if m else str(u))
                tier = get_tier(m, "lol") or "?"
                main, sub = await get_lanes(u)
                rows.append((f"{nick}", f"{tier} · {main or '-'},{sub or '-'}"))
            elif s.game == "val":
                nick = await get_nickname(u, "val") or (m.display_name if m else str(u))
                rows.append((f"{nick}", get_tier(m, "val") or "?"))
            else:
                rows.append(((m.display_name if m else str(u)), ""))
        return rows

    async def show_teams(self, interaction: discord.Interaction, s: MatchSession, winner=None, mvp_name=None):
        blue = await self._team_rows(s, s.team1)
        red = await self._team_rows(s, s.team2)
        title = f"내전 결과 · {GAME_LABELS.get(s.game, s.game)}" if winner else f"팀 편성 완료 · {GAME_LABELS.get(s.game, s.game)}"
        img = await asyncio.to_thread(render_team_image, title, blue, red, mvp_name, winner)
        file = discord.File(io.BytesIO(img), filename="teams.png")
        content = None
        if winner:
            content = f"🏆 **{winner}팀 승리!** 승리팀 +{WIN_POINTS}P / 패배팀 +{LOSE_POINTS}P"
        view = None if winner else ResultView(self, s)
        if interaction.response.is_done():
            await interaction.followup.send(content=content, file=file, view=view)
        else:
            await interaction.response.edit_message(content=content, embed=None, attachments=[file], view=view)

    async def declare_winner(self, interaction: discord.Interaction, s: MatchSession, winner: int):
        if not can_manage(interaction.user, s.host_id):
            return await interaction.response.send_message("❌ 내전매니저/관리자만 결과를 확정할 수 있습니다.", ephemeral=True)
        async with s.lock:
            if s.phase == "done":
                return await interaction.response.send_message("이미 결과가 확정되었습니다.", ephemeral=True)
            if s.phase != "teams":
                return await interaction.response.send_message("아직 팀 편성이 끝나지 않았습니다.", ephemeral=True)
            win_ids, lose_ids = s.finalize(winner)
        for uid in win_ids:
            await add_points(uid, WIN_POINTS)
        for uid in lose_ids:
            await add_points(uid, LOSE_POINTS)
        mvp_id = s.mvp()
        mvp_member = s.member(mvp_id) if mvp_id else None
        mvp_name = (await short_name(mvp_member, s.game)) if mvp_member else None
        await interaction.response.defer()
        await self.show_teams(interaction, s, winner=winner, mvp_name=mvp_name)
        # 내전 종료 로그 (내전로그 채널) — 게임/승패/팀 편성/MVP
        try:
            host = s.member(s.host_id)
            blue_names = [await short_name(s.member(u), s.game) for u in s.team1]
            red_names = [await short_name(s.member(u), s.game) for u in s.team2]
            e = discord.Embed(
                title="🏆 내전 종료",
                description=f"게임: **{GAME_LABELS.get(s.game, s.game)}** · 방장: {host.mention if host else '?'}",
                color=discord.Color.gold())
            e.add_field(name=f"🔵 1팀{' 👑 WIN' if winner == 1 else ''}",
                        value="\n".join(f"- {n}" for n in blue_names) or "-", inline=True)
            e.add_field(name=f"🔴 2팀{' 👑 WIN' if winner == 2 else ''}",
                        value="\n".join(f"- {n}" for n in red_names) or "-", inline=True)
            e.add_field(name="보상", value=f"승리팀 +{WIN_POINTS}P / 패배팀 +{LOSE_POINTS}P", inline=False)
            if mvp_name:
                e.add_field(name="🏅 MVP", value=mvp_name, inline=False)
            enqueue_embed(MATCH_LOG_CH, e.to_dict(), guild=s.guild)
        except Exception:
            pass
        # 내전 결과 발표 채널 — 결과 이미지와 함께 공개 게시
        try:
            ch = self.bot.get_channel(RESULT_ANNOUNCE_CH)
            if ch:
                blue = await self._team_rows(s, s.team1)
                red = await self._team_rows(s, s.team2)
                img2 = await asyncio.to_thread(
                    render_team_image,
                    f"내전 결과 · {GAME_LABELS.get(s.game, s.game)}",
                    blue, red, mvp_name, winner)
                res = discord.Embed(
                    title=f"🏆 내전 결과 · {GAME_LABELS.get(s.game, s.game)}",
                    description=(f"**{winner}팀 승리!**"
                                 f"{f' · 🏅 MVP: **{mvp_name}**' if mvp_name else ''}\n"
                                 f"승리팀 +{WIN_POINTS}P / 패배팀 +{LOSE_POINTS}P"),
                    color=discord.Color.blue() if winner == 1 else discord.Color.red(),
                    timestamp=discord.utils.utcnow())
                res.set_image(url="attachment://result.png")
                await ch.send(embed=res,
                              file=discord.File(io.BytesIO(img2), filename="result.png"))
        except Exception as e2:
            print(f"[내전 결과 발표] 전송 실패: {e2}")

    # ----- 명령어 -----
    @match_group.command(name="진행", description="포럼 참여자들로 내전을 진행합니다.")
    @app_commands.describe(게임종류="내전 게임", 인원수="모집 인원 (미입력 시 롤/발로 10명, 그 외 20명)")
    @app_commands.choices(게임종류=GAME_CHOICES)
    async def progress(self, interaction: discord.Interaction,
                       게임종류: app_commands.Choice[str], 인원수: int = None):
        await interaction.response.defer()
        game = 게임종류.value
        capacity = 인원수 or (10 if game in TEAM_GAMES else 20)
        found = await self.scan_participants(interaction.guild)
        participants = found[:capacity]
        session = MatchSession(interaction.user, game, capacity, participants)
        await interaction.followup.send(embed=await self.list_embed(session), view=ListView(self, session))


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchCog(bot))
