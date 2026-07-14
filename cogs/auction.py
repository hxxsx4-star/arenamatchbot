import discord
from discord import app_commands
import random
from discord.ext import commands

# utils에서 stats 함수 임포트 (우승 포인트 지급용)
from utils.stats import add_points

# ✨ 40인(다수 인원) 지원을 위해 동적으로 팀 데이터를 구성하도록 구조 변경
auction_data = {
    "captains": {},      # 팀번호: 멤버 객체
    "points": {},        # 팀번호: 포인트
    "rosters": {},       # 팀번호: [낙찰된 멤버, ...] 명단 관리
    "is_active": False,
    "current_target": None,
    "current_price": 0,
    "highest_bidder_team": None
}

@app_commands.guild_only()
class AuctionCog(commands.Cog):
    """다인원 내전 지원 경매 시스템을 담당하는 Cog"""

    auction_group = app_commands.Group(name="경매", description="다인원 내전 경매 시스템")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @auction_group.command(name="팀장", description="[관리자 전용] 지정한 팀 번호의 팀장을 설정합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_captain(self, interaction: discord.Interaction, user: discord.Member, team: int):
        if team < 1:
            await interaction.response.send_message("팀 번호는 1 이상으로 입력해 주세요.", ephemeral=True)
            return

        auction_data["captains"][team] = user
        if team not in auction_data["rosters"]:
            auction_data["rosters"][team] = []

        await interaction.response.send_message(f"✅ {team}팀 팀장이 {user.mention}님으로 설정되었습니다!")

    @auction_group.command(name="시작", description="[관리자 전용] 팀장이 설정된 팀들을 바탕으로 경매를 시작합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def start_auction(self, interaction: discord.Interaction, point: int):
        active_teams = [t for t, c in auction_data["captains"].items() if c is not None]

        if not active_teams:
            await interaction.response.send_message("⚠️ 팀장이 아직 한 명도 없습니다! `/경매 팀장`을 먼저 설정해 주세요.", ephemeral=True)
            return

        for t in active_teams:
            auction_data["points"][t] = point
            if t not in auction_data["rosters"]:
                auction_data["rosters"][t] = []

        auction_data["is_active"] = True

        embed = discord.Embed(title="🎉 다인원 내전 경매 시작!", description=f"총 {len(active_teams)}개의 팀으로 경매를 진행합니다.\n각 팀장에게 {point}p가 지급되었습니다.", color=discord.Color.gold())
        for t in sorted(active_teams):
            embed.add_field(name=f"{t}팀 팀장", value=f"{auction_data['captains'][t].mention} ({point}p)", inline=False)

        await interaction.response.send_message(embed=embed)

    @auction_group.command(name="대상", description="[관리자 전용] 특정 유저의 경매를 시작합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def target_user_auction(self, interaction: discord.Interaction, user: discord.Member, start_point: int):
        if not auction_data["is_active"]:
            await interaction.response.send_message("아직 경매가 시작되지 않았습니다.", ephemeral=True)
            return

        auction_data["current_target"] = user
        auction_data["current_price"] = start_point
        auction_data["highest_bidder_team"] = None

        active_teams = [t for t in auction_data["captains"].keys()]
        first_turn_team = random.choice(active_teams)
        first_turn_captain = auction_data["captains"][first_turn_team]

        msg = (
            f"📢 {user.mention} 님의 경매를 시작합니다! (시작가: {start_point}p)\n\n"
            f"🎲 랜덤 추첨 결과, 첫 입찰권은 {first_turn_team}팀 {first_turn_captain.mention}님에게 주어집니다!\n"
            f"*(참여 방법: 팀장님들은 채팅창에 숫자만 입력해 주세요!)*"
        )
        await interaction.response.send_message(msg)

    @auction_group.command(name="낙찰", description="[관리자 전용] 현재 대상을 최고 입찰 팀에게 낙찰시킵니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def sold_auction(self, interaction: discord.Interaction):
        target = auction_data["current_target"]
        winner_team = auction_data["highest_bidder_team"]
        final_price = auction_data["current_price"]

        if target is None:
            await interaction.response.send_message("현재 진행 중인 경매가 없습니다.", ephemeral=True)
            return
        if winner_team is None:
            await interaction.response.send_message("입찰자가 없어 유찰되었습니다.")
            auction_data["current_target"] = None
            return

        auction_data["points"][winner_team] -= final_price

        # ✨ 해당 팀 명단에 멤버 추가
        if winner_team not in auction_data["rosters"]:
            auction_data["rosters"][winner_team] = []
        auction_data["rosters"][winner_team].append(target)

        msg = (
            f"🎉 낙찰!!\n"
            f"{target.mention} 님이 {winner_team}팀에 {final_price}p로 낙찰되었습니다!\n"
            f"💰 {winner_team}팀 남은 포인트: {auction_data['points'][winner_team]}p"
        )
        auction_data["current_target"] = None
        await interaction.response.send_message(msg)

    # ✨ 팀 우승 시 포인트를 지급하는 명령어
    @auction_group.command(name="우승", description="[관리자 전용] 경매 내전 우승 팀원 전원에게 500P씩 지급합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def auction_winner(self, interaction: discord.Interaction, 팀넘버: int):
        if 팀넘버 not in auction_data["captains"] or auction_data["captains"][팀넘버] is None:
            await interaction.response.send_message(f"⚠️ {팀넘버}팀 정보가 없습니다. (경매가 진행되지 않았거나 잘못된 번호입니다)", ephemeral=True)
            return

        captain = auction_data["captains"][팀넘버]
        members = auction_data.get("rosters", {}).get(팀넘버, [])
        all_winners = [captain] + members

        await interaction.response.defer()

        mentions = []
        for member in all_winners:
            await add_points(member.id, 500)
            mentions.append(member.mention)

        embed = discord.Embed(
            title="🏆 경매 내전 우승 축하합니다!",
            description=f"{팀넘버}팀이 우승했습니다!\n팀장 및 낙찰된 팀원 전원에게 각각 500 Point가 지급되었습니다.\n\n[우승자 명단]\n{', '.join(mentions)}",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)

    @auction_group.command(name="종료", description="[관리자 전용] 현재 진행중인 경매를 강제로 종료하고 데이터를 초기화합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def end_auction(self, interaction: discord.Interaction):
        if not auction_data["is_active"]:
            await interaction.response.send_message("⚠️ 이미 종료되었거나 시작되지 않은 경매입니다.", ephemeral=True)
            return

        # 데이터 초기화
        auction_data["captains"].clear()
        auction_data["points"].clear()
        auction_data["rosters"].clear()
        auction_data["is_active"] = False
        auction_data["current_target"] = None
        auction_data["current_price"] = 0
        auction_data["highest_bidder_team"] = None

        embed = discord.Embed(
            title="🛑 경매 강제 종료",
            description="관리자에 의해 경매가 강제 종료되었습니다.\n모든 팀장, 포인트, 로스터 데이터가 초기화되었습니다.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or auction_data["current_target"] is None:
            return

        author_team = None
        for team, captain in auction_data["captains"].items():
            if captain and message.author.id == captain.id:
                author_team = team
                break

        if author_team is None:
            return
        if not message.content.isdigit():
            return

        new_bid = int(message.content)
        current_price = auction_data["current_price"]

        if new_bid > auction_data["points"][author_team]:
            await message.reply(f"⚠️ 포인트가 부족합니다! (현재 보유: {auction_data['points'][author_team]}p)", delete_after=3)
            return

        min_increment = 50 if current_price >= 200 else 10
        if auction_data["highest_bidder_team"] is None:
            if new_bid < current_price:
                 await message.reply(f"⚠️ 시작가({current_price}p) 이상으로 입찰해야 합니다!", delete_after=3)
                 return
        else:
            if new_bid < current_price + min_increment:
                await message.reply(f"⚠️ 최소 {min_increment}p 단위로 올려야 합니다! (최소 입찰가: {current_price + min_increment}p)", delete_after=3)
                return

        auction_data["current_price"] = new_bid
        auction_data["highest_bidder_team"] = author_team
        await message.channel.send(f"🪙 {author_team}팀({message.author.display_name}) 입찰! 👉 {new_bid}p")

async def setup(bot: commands.Bot):
    cog = AuctionCog(bot)
    await bot.add_cog(cog)