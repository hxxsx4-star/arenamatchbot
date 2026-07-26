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
import json
import time
import asyncio

import discord
from discord.ext import commands
from discord import app_commands

from utils.database import (save_match_session, load_active_match_sessions,
                            purge_old_match_sessions)
from utils.stats import add_points, get_nickname, get_lanes, format_num
from utils.tiers import get_tier
from utils.logs import MATCH_LOG_CH, enqueue_embed

# 내전 결과 발표 채널 (결과 이미지 + 요약을 공개 게시)
RESULT_ANNOUNCE_CH = 1528716794469027930

# 결과 확정 후 라이엇 전적(커스텀 게임)을 탐색해 KDA/챔피언을 자동 수집하는 재시도 간격(초)
_RECORD_RETRY_DELAYS = [20, 60, 120, 180, 300]

FORUM_ID = 1527367803676655697
MANAGER_ROLE = 1526678410124988526
WIN_POINTS, LOSE_POINTS = 140, 60
# MVP 보상. 동점(공동 MVP)이면 이 금액을 인원수로 나눠 지급한다.
# (나누지 않으면 5명 동점 시 10,000P 가 한 판에서 풀려 경제가 무너진다)
MVP_POINTS = 2000

# 포럼 스캔 범위. 좁으면 초반에 참여 표현을 남긴 사람이 누락된다.
SCAN_ARCHIVED_THREADS = 30
SCAN_HISTORY_LIMIT = 1000

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


def make_draft_order(total_players: int) -> list[int]:
    """총 인원수에 맞는 스네이크 드래프트 순서(팀 번호 리스트)를 만든다.

    팀장 2명을 뺀 나머지를 1,2,2,1,1,2,2,1... 순으로 나눠 양 팀이 같은 수가 되게 한다.
    (10명이면 기존과 동일한 [1,2,2,1,1,2,2,1])
    """
    picks = total_players - 2
    return [1 if ((i + 1) // 2) % 2 == 0 else 2 for i in range(picks)]

_norm_re = re.compile(r"[^가-힣ㄱ-ㅎa-zA-Z]")


def is_trigger(content: str) -> bool:
    if not content:
        return False
    norm = _norm_re.sub("", content).strip().lower()
    return norm in TRIGGERS


def can_manage(member: discord.Member, host_id: int) -> bool:
    """방장 / 내전매니저 / 서버 관리 권한이면 True. (모집·팀짜기 운영용)"""
    if member.id == host_id:
        return True
    return can_finalize(member)


def can_finalize(member: discord.Member) -> bool:
    """내전매니저 / 서버 관리 권한이면 True. (결과 확정 = 포인트 지급용)

    승리 확정은 참가자 전원에게 포인트를 지급하므로 방장이라는 이유만으로는 허용하지 않는다.
    /내전 진행 은 누구나 칠 수 있고 실행자가 곧 방장이 되기 때문에,
    방장을 통과시키면 아무나 판을 만들어 포인트를 찍어낼 수 있다.
    """
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
    """내전 한 판의 상태. 봇 재시작에도 이어지도록 DB 에 통째로 저장/복원된다."""

    def __init__(self, guild: discord.Guild, host_id: int, game: str, capacity: int,
                 participant_ids: list[int]):
        self.guild = guild
        self.host_id = host_id
        self.game = game
        self.capacity = capacity
        self.participant_ids = list(participant_ids)   # Member 가 아니라 ID 로 보관(직렬화용)
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
        # 패널 메시지 위치 (저장 키 겸 복원 대상)
        self.message_id = 0
        self.channel_id = 0

    # ----- 멤버 조회 -----
    def member(self, uid: int):
        return self.guild.get_member(uid) if self.guild else None

    @property
    def participants(self) -> list[discord.Member]:
        """서버에 남아 있는 참가자만 Member 로 돌려준다."""
        out = []
        for uid in self.participant_ids:
            m = self.member(uid)
            if m is not None:
                out.append(m)
        return out

    def in_match(self, uid: int) -> bool:
        return uid in self.participant_ids

    def draft_order(self) -> list[int]:
        return make_draft_order(len(self.participant_ids))

    # ----- 직렬화 -----
    def to_row(self) -> dict:
        return {
            "message_id": self.message_id, "channel_id": self.channel_id,
            "guild_id": self.guild.id if self.guild else 0,
            "host_id": self.host_id, "game": self.game, "capacity": self.capacity,
            "phase": self.phase,
            "participants": json.dumps(self.participant_ids),
            "captains": json.dumps(self.captains),
            "team1": json.dumps(self.team1), "team2": json.dumps(self.team2),
            "pool": json.dumps(self.pool), "draft_idx": self.draft_idx,
            "mvp_votes": json.dumps({str(k): v for k, v in self.mvp_votes.items()}),
            "winner": self.winner or 0, "updated_at": time.time(),
        }

    @classmethod
    def from_row(cls, guild: discord.Guild, row: dict) -> "MatchSession":
        s = cls(guild, row["host_id"], row["game"], row["capacity"],
                json.loads(row["participants"]))
        s.phase = row["phase"]
        s.captains = json.loads(row["captains"])
        s.team1 = json.loads(row["team1"])
        s.team2 = json.loads(row["team2"])
        s.pool = json.loads(row["pool"])
        s.draft_idx = row["draft_idx"]
        s.mvp_votes = {int(k): v for k, v in json.loads(row["mvp_votes"]).items()}
        s.winner = row["winner"] or None
        s.message_id = row["message_id"]
        s.channel_id = row["channel_id"]
        return s

    # ----- 팀짜기 로직 (순수) -----
    def start_draft(self):
        self.pool = list(self.participant_ids)
        self.team1, self.team2, self.captains = [], [], [0, 0]
        self.draft_idx = 0
        self.phase = "captain1"

    def set_captain(self, team: int, uid: int):
        self.captains[team - 1] = uid
        self.pool.remove(uid)
        (self.team1 if team == 1 else self.team2).append(uid)
        self.phase = "captain2" if team == 1 else "draft"

    def current_team(self) -> int:
        order = self.draft_order()
        return order[self.draft_idx] if self.draft_idx < len(order) else 1

    def current_captain(self) -> int:
        return self.captains[self.current_team() - 1]

    def pick(self, uid: int):
        team = self.current_team()
        self.pool.remove(uid)
        (self.team1 if team == 1 else self.team2).append(uid)
        self.draft_idx += 1
        self._auto_finish()

    def _auto_finish(self):
        order = self.draft_order()
        # 마지막 1명은 선택지가 없으므로 자동 배정 (남은 인원이 1명일 때)
        if self.draft_idx < len(order) and len(self.pool) == 1:
            team = order[self.draft_idx]
            uid = self.pool.pop(0)
            (self.team1 if team == 1 else self.team2).append(uid)
            self.draft_idx += 1
        if self.draft_idx >= len(order):
            self.phase = "teams"

    def mvp_candidates(self, exclude_id: int | None = None) -> list[int]:
        """MVP 투표 후보. 본인은 제외한다(자기 자신에게 투표 불가)."""
        return [u for u in (self.team1 + self.team2) if u != exclude_id]

    def finalize(self, winner: int):
        self.winner = winner
        self.phase = "done"
        win_ids = self.team1 if winner == 1 else self.team2
        lose_ids = self.team2 if winner == 1 else self.team1
        return win_ids, lose_ids

    def mvp(self):
        """최다 득표자 1명. (하위호환용 — 동점이면 mvp_all() 을 쓰는 게 정확하다)"""
        winners = self.mvp_all()
        return winners[0] if winners else None

    def mvp_all(self) -> list[int]:
        """최다 득표자 전원. 동점이면 여러 명이 나온다(공동 MVP)."""
        if not self.mvp_votes:
            return []
        tally: dict[int, int] = {}
        for t in self.mvp_votes.values():
            tally[t] = tally.get(t, 0) + 1
        top = max(tally.values())
        return [uid for uid, c in tally.items() if c == top]


# ============ Views ============
class ListView(discord.ui.View):
    """리스트 화면. 롤/발로면 '내전 시작' 버튼 + 참여자 수동 추가/제외."""
    def __init__(self, cog: "MatchCog", session: MatchSession):
        super().__init__(timeout=None)
        self.cog = cog
        self.session = session
        if session.game not in TEAM_GAMES:
            self.remove_item(self.start_button)

    @discord.ui.button(label="내전 시작 (팀짜기)", style=discord.ButtonStyle.success, emoji="⚔️",
                       custom_id="match:start", row=0)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.session
        if not can_manage(interaction.user, s.host_id):
            return await interaction.response.send_message("❌ 방장/내전매니저/관리자만 시작할 수 있습니다.", ephemeral=True)
        async with s.lock:
            if s.phase != "list":
                return await interaction.response.send_message("이미 진행 중입니다.", ephemeral=True)
            n = len(s.participant_ids)
            if n < 4 or n % 2 != 0:
                return await interaction.response.send_message(
                    f"❌ 팀짜기는 4명 이상의 짝수 인원이 필요합니다. (현재 {n}명)\n"
                    "참여자 추가/제외 메뉴로 인원을 맞춰주세요.", ephemeral=True)
            s.start_draft()
        await self.cog.persist(s)
        await interaction.response.edit_message(
            embed=self.cog.captain_embed(s, 1), view=CaptainView(self.cog, s, 1))

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="➕ 참여자 추가 (포럼에 안 쓴 사람)",
                       min_values=1, max_values=10, custom_id="match:add", row=1)
    async def add_member(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        s = self.session
        if not can_manage(interaction.user, s.host_id):
            return await interaction.response.send_message("❌ 방장/내전매니저/관리자만 변경할 수 있습니다.", ephemeral=True)
        async with s.lock:
            if s.phase != "list":
                return await interaction.response.send_message("이미 팀짜기가 시작되어 변경할 수 없습니다.", ephemeral=True)
            added, dup = [], []
            for u in select.values:
                if u.id in s.participant_ids:
                    dup.append(u.display_name)
                else:
                    s.participant_ids.append(u.id)
                    added.append(u.display_name)
        await self.cog.persist(s)
        await interaction.response.edit_message(embed=await self.cog.list_embed(s), view=self)
        msg = []
        if added:
            msg.append(f"➕ 추가: {', '.join(added)}")
        if dup:
            msg.append(f"⏭️ 이미 있음: {', '.join(dup)}")
        await interaction.followup.send("\n".join(msg) or "변경 없음", ephemeral=True)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="➖ 참여자 제외",
                       min_values=1, max_values=10, custom_id="match:remove", row=2)
    async def remove_member(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        s = self.session
        if not can_manage(interaction.user, s.host_id):
            return await interaction.response.send_message("❌ 방장/내전매니저/관리자만 변경할 수 있습니다.", ephemeral=True)
        async with s.lock:
            if s.phase != "list":
                return await interaction.response.send_message("이미 팀짜기가 시작되어 변경할 수 없습니다.", ephemeral=True)
            removed, absent = [], []
            for u in select.values:
                if u.id in s.participant_ids:
                    s.participant_ids.remove(u.id)
                    removed.append(u.display_name)
                else:
                    absent.append(u.display_name)
        await self.cog.persist(s)
        await interaction.response.edit_message(embed=await self.cog.list_embed(s), view=self)
        msg = []
        if removed:
            msg.append(f"➖ 제외: {', '.join(removed)}")
        if absent:
            msg.append(f"⏭️ 명단에 없음: {', '.join(absent)}")
        await interaction.followup.send("\n".join(msg) or "변경 없음", ephemeral=True)


class CaptainView(discord.ui.View):
    def __init__(self, cog: "MatchCog", session: MatchSession, team: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.session = session
        self.team = team
        options = [discord.SelectOption(label=(session.member(uid).display_name if session.member(uid) else str(uid)),
                                        value=str(uid)) for uid in session.pool]
        self.select = discord.ui.Select(placeholder=f"{team}팀 팀장을 선택하세요", options=options[:25],
                                        custom_id=f"match:captain{team}")
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
        await self.cog.persist(s)
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
        self.select = discord.ui.Select(placeholder=f"{team}팀 팀장이 픽하세요", options=options[:25],
                                        custom_id="match:draft")
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
        await self.cog.persist(s)
        if s.phase == "teams":
            await self.cog.show_teams(interaction, s)
        else:
            await self.cog.show_draft(interaction, s)


class ResultView(discord.ui.View):
    def __init__(self, cog: "MatchCog", session: MatchSession):
        super().__init__(timeout=None)
        self.cog = cog
        self.session = session

    @discord.ui.button(label="MVP 투표", style=discord.ButtonStyle.primary, emoji="🏅",
                       custom_id="match:mvp")
    async def mvp_vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.session
        if not s.in_match(interaction.user.id):
            return await interaction.response.send_message("❌ 참가자만 투표할 수 있습니다.", ephemeral=True)
        if s.phase == "done":
            return await interaction.response.send_message(
                "❌ 이미 결과가 확정되어 투표가 마감되었습니다.", ephemeral=True)
        if not s.mvp_candidates(interaction.user.id):
            return await interaction.response.send_message("투표할 대상이 없습니다.", ephemeral=True)
        await interaction.response.send_message(
            "MVP를 선택하세요. (본인 제외)",
            view=MVPVoteView(self.cog, s, interaction.user.id), ephemeral=True)

    @discord.ui.button(label="1팀 승리", style=discord.ButtonStyle.secondary, emoji="🔵",
                       custom_id="match:win1")
    async def win1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.declare_winner(interaction, self.session, 1)

    @discord.ui.button(label="2팀 승리", style=discord.ButtonStyle.secondary, emoji="🔴",
                       custom_id="match:win2")
    async def win2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.declare_winner(interaction, self.session, 2)


class MVPVoteView(discord.ui.View):
    """개인에게 임시로 보내는 투표 창(에페메랄)이라 영구 뷰가 아니어도 된다."""
    def __init__(self, cog: "MatchCog", session: MatchSession, voter_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.session = session
        self.voter_id = voter_id
        # 본인은 후보에서 제외 (자기 자신에게 투표 불가)
        opts = [discord.SelectOption(label=(session.member(uid).display_name if session.member(uid) else str(uid)),
                                     value=str(uid)) for uid in session.mvp_candidates(voter_id)]
        self.select = discord.ui.Select(placeholder="MVP 선택", options=opts[:25])
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        s = self.session
        target = int(self.select.values[0])
        if target == self.voter_id:
            return await interaction.response.edit_message(
                content="❌ 자기 자신에게는 투표할 수 없습니다.", view=None)
        async with s.lock:
            if s.phase == "done":
                return await interaction.response.edit_message(
                    content="❌ 이미 결과가 확정되어 투표가 마감되었습니다.", view=None)
            s.mvp_votes[self.voter_id] = target
        await self.cog.persist(s)
        name = s.member(target).display_name if s.member(target) else str(target)
        await interaction.response.edit_message(
            content=f"✅ **{name}** 님에게 MVP 투표했습니다!", view=None)


class MatchCog(commands.Cog):
    """내전 진행 엔진."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    match_group = app_commands.Group(name="내전", description="내전 관련 명령어")

    # ----- 세션 영속화 -----
    async def persist(self, s: MatchSession):
        """세션 상태를 디스크에 저장한다. 상태가 바뀔 때마다 호출."""
        if not s.message_id:
            return
        try:
            await save_match_session(s.to_row())
        except Exception as e:
            print(f"[내전 세션 저장 실패] {e}")

    def view_for(self, s: MatchSession):
        """현재 단계에 맞는 뷰를 만든다. (재시작 복원 / 화면 갱신 공용)"""
        if s.phase == "list":
            return ListView(self, s)
        if s.phase == "captain1":
            return CaptainView(self, s, 1)
        if s.phase == "captain2":
            return CaptainView(self, s, 2)
        if s.phase == "draft":
            return DraftView(self, s)
        if s.phase == "teams":
            return ResultView(self, s)
        return None

    @commands.Cog.listener()
    async def on_ready(self):
        """재시작 전에 진행 중이던 내전의 버튼이 계속 동작하도록 뷰를 다시 붙인다.

        (뷰가 메모리에만 있어서, 예전에는 재배포할 때마다 진행 중인 내전이
         '상호작용 실패'가 되고 처음부터 다시 해야 했다.)
        """
        if getattr(self, "_restored", False):
            return
        self._restored = True
        try:
            rows = await load_active_match_sessions()
        except Exception as e:
            print(f"[내전 세션 복원 실패] {e}")
            return
        ok = 0
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            try:
                s = MatchSession.from_row(guild, row)
                view = self.view_for(s)
                if view is None:
                    continue
                self.bot.add_view(view, message_id=s.message_id)
                ok += 1
            except Exception as e:
                print(f"[내전 세션 복원 건너뜀] msg={row.get('message_id')}: {e}")
        if ok:
            print(f"♻️ 진행 중이던 내전 {ok}건의 버튼을 복원했습니다.")
        try:
            await purge_old_match_sessions()
        except Exception as e:
            print(f"[내전 세션 정리 실패] {e}")

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
                async for th in forum.archived_threads(limit=SCAN_ARCHIVED_THREADS):
                    threads.append(th)
            except Exception as e:
                print(f"[내전 스캔] 보관된 스레드 조회 실패: {e}")
        for th in threads:
            try:
                async for msg in th.history(limit=SCAN_HISTORY_LIMIT):
                    if msg.author.bot or msg.author.id in found:
                        continue
                    if is_trigger(msg.content):
                        m = guild.get_member(msg.author.id)
                        if m:
                            found[m.id] = m
            except Exception as e:
                print(f"[내전 스캔] 스레드 '{getattr(th, 'name', th)}' 읽기 실패: {e}")
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
        # 포인트가 지급되는 단계라 방장 특권으로는 통과시키지 않는다.
        if not can_finalize(interaction.user):
            return await interaction.response.send_message(
                "❌ 결과 확정은 내전매니저/관리자만 할 수 있습니다. (방장이어도 불가)", ephemeral=True)
        async with s.lock:
            if s.phase == "done":
                return await interaction.response.send_message("이미 결과가 확정되었습니다.", ephemeral=True)
            if s.phase != "teams":
                return await interaction.response.send_message("아직 팀 편성이 끝나지 않았습니다.", ephemeral=True)
            win_ids, lose_ids = s.finalize(winner)
        await self.persist(s)
        for uid in win_ids:
            await add_points(uid, WIN_POINTS)
        for uid in lose_ids:
            await add_points(uid, LOSE_POINTS)
        # 동점이면 공동 MVP 로 모두 표기한다. (예전엔 조용히 한 명만 뽑혔다)
        mvp_ids = s.mvp_all()
        mvp_names = [await short_name(s.member(u), s.game) for u in mvp_ids if s.member(u)]
        mvp_name = " / ".join(mvp_names) if mvp_names else None
        # MVP 보상 지급 (공동 MVP 면 균등 분배)
        mvp_reward = MVP_POINTS // len(mvp_ids) if mvp_ids else 0
        for uid in mvp_ids:
            await add_points(uid, mvp_reward)
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
            reward_text = f"승리팀 +{WIN_POINTS}P / 패배팀 +{LOSE_POINTS}P"
            if mvp_ids:
                reward_text += f"\nMVP +{format_num(mvp_reward)}P"
                if len(mvp_ids) > 1:
                    reward_text += f" (공동 {len(mvp_ids)}명 · {format_num(MVP_POINTS)}P 분배)"
            e.add_field(name="보상", value=reward_text, inline=False)
            if mvp_name:
                e.add_field(name=("🏅 공동 MVP" if len(mvp_names) > 1 else "🏅 MVP"),
                            value=mvp_name, inline=False)
            enqueue_embed(MATCH_LOG_CH, e.to_dict(), guild=s.guild)
        except Exception:
            pass
        # 웹사이트 기록실 자동 기록 (롤 내전: 라이엇 전적에서 KDA/챔피언 수집)
        if s.game == "lol":
            try:
                asyncio.create_task(self._auto_record(s, winner, mvp_name))
            except Exception as e3:
                print(f"[내전 자동기록] 시작 실패: {e3}")
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
                                 f"승리팀 +{WIN_POINTS}P / 패배팀 +{LOSE_POINTS}P"
                                 f"{f' · MVP +{format_num(mvp_reward)}P' if mvp_ids else ''}"),
                    color=discord.Color.blue() if winner == 1 else discord.Color.red(),
                    timestamp=discord.utils.utcnow())
                res.set_image(url="attachment://result.png")
                await ch.send(embed=res,
                              file=discord.File(io.BytesIO(img2), filename="result.png"))
        except Exception as e2:
            print(f"[내전 결과 발표] 전송 실패: {e2}")

    async def _auto_record(self, s: MatchSession, winner: int, mvp_name):
        """결과 확정 후 라이엇 전적(커스텀 게임)에서 KDA/챔피언을 찾아
        웹사이트 기록실에 자동 등록하고, 찾으면 상세 전적도 발표 채널에 올린다.
        라이엇 조회 실패 시 KDA 없이 팀 구성/승패/MVP 만 기록한다."""
        import datetime
        from arenasite import store as site_store
        try:
            from arenasite import riot as site_riot
            riot_on = site_riot.enabled()
        except Exception:
            site_riot, riot_on = None, False

        # 팀별 등록 롤닉 수집 (미등록자는 표시 이름)
        async def nicks(uids):
            out = []
            for u in uids:
                n = await get_nickname(u, "lol")
                m = s.member(u)
                out.append((n or (m.display_name if m else str(u))))
            return out

        team1_nicks = await nicks(s.team1)
        team2_nicks = await nicks(s.team2)
        all_nicks_l = {n.lower() for n in team1_nicks + team2_nicks if "#" in n}

        # 참가자 전적에서 방금 끝난 커스텀 게임 탐색 (지연 반영 대비 재시도)
        matched = None  # {nick_lower: {champion,kills,deaths,assists}}
        mode = "normal"
        if riot_on and all_nicks_l:
            probe_nick = next((n for n in team1_nicks + team2_nicks if "#" in n), None)
            puuid = await site_riot.get_puuid(probe_nick) if probe_nick else None
            now_ms = __import__("time").time() * 1000
            for delay in _RECORD_RETRY_DELAYS:
                await asyncio.sleep(delay)
                if not puuid:
                    break
                for mid in await site_riot.get_match_ids(puuid, count=3):
                    dto = await site_riot.get_match(mid)
                    info = (dto or {}).get("info", {})
                    if info.get("queueId") != 0:          # 0 = 커스텀 게임
                        continue
                    if now_ms - info.get("gameEndTimestamp", 0) > 3 * 3600 * 1000:
                        continue
                    parts = {}
                    for p in info.get("participants", []):
                        rid = f"{p.get('riotIdGameName','')}#{p.get('riotIdTagline','')}".lower()
                        parts[rid] = {
                            "champion": p.get("championName", ""),
                            "kills": p.get("kills", 0),
                            "deaths": p.get("deaths", 0),
                            "assists": p.get("assists", 0),
                        }
                    # 우리 내전 참가자와 3명 이상 일치하면 그 게임으로 확정
                    if len(all_nicks_l & set(parts)) >= min(3, len(all_nicks_l)):
                        matched = parts
                        if info.get("mapId") == 12:       # 칼바람 나락
                            mode = "aram"
                        break
                if matched:
                    break

        def team_payload(name, nicks_list, win):
            players = []
            for n in nicks_list:
                d = (matched or {}).get(n.lower(), {})
                players.append({
                    "summoner": n,
                    "champion": d.get("champion", ""),
                    "position": "",
                    "kills": d.get("kills", 0),
                    "deaths": d.get("deaths", 0),
                    "assists": d.get("assists", 0),
                })
            return {"name": name, "win": win, "players": players}

        payload = {
            "date": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d"),
            "mode": mode,
            "title": "내전 자동기록",
            "mvp": mvp_name or "",
            "teams": [
                team_payload("1팀", team1_nicks, winner == 1),
                team_payload("2팀", team2_nicks, winner == 2),
            ],
        }
        try:
            await asyncio.to_thread(site_store.add_match, payload)
            print(f"[내전 자동기록] 기록 완료 (KDA {'포함' if matched else '없음'})")
        except Exception as e:
            print(f"[내전 자동기록] 저장 실패: {e}")
            return

        # KDA 를 찾았으면 발표 채널에 상세 전적 임베드 추가
        if matched:
            try:
                ch = self.bot.get_channel(RESULT_ANNOUNCE_CH)
                if ch:
                    def lines(nicks_list):
                        out = []
                        for n in nicks_list:
                            d = (matched or {}).get(n.lower())
                            if d:
                                out.append(f"- **{n}** · {d['champion']} `{d['kills']}/{d['deaths']}/{d['assists']}`")
                            else:
                                out.append(f"- **{n}**")
                        return "\n".join(out) or "-"
                    e = discord.Embed(
                        title="📋 상세 전적 (자동 수집)",
                        color=discord.Color.gold(),
                        timestamp=discord.utils.utcnow())
                    e.add_field(name=f"🔵 1팀{' 👑' if winner == 1 else ''}",
                                value=lines(team1_nicks), inline=True)
                    e.add_field(name=f"🔴 2팀{' 👑' if winner == 2 else ''}",
                                value=lines(team2_nicks), inline=True)
                    e.set_footer(text="웹사이트 기록실에 자동 등록되었습니다.")
                    await ch.send(embed=e)
            except Exception as e:
                print(f"[내전 자동기록] 상세 전적 전송 실패: {e}")

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
        session = MatchSession(interaction.guild, interaction.user.id, game, capacity,
                               [m.id for m in participants])
        view = ListView(self, session)
        msg = await interaction.followup.send(embed=await self.list_embed(session), view=view,
                                              wait=True)
        # 메시지 ID 를 알아야 재시작 후 이 화면의 버튼을 되살릴 수 있다.
        session.message_id = msg.id
        session.channel_id = msg.channel.id
        await self.persist(session)
        if len(found) > capacity:
            await interaction.followup.send(
                f"⚠️ 참여 표현을 남긴 사람이 {len(found)}명인데 모집 인원이 {capacity}명이라 "
                f"{len(found) - capacity}명이 명단에서 잘렸습니다. "
                "필요하면 아래 참여자 추가/제외 메뉴로 조정하세요.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchCog(bot))
