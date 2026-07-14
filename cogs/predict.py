import discord
from discord.ext import commands, tasks
from discord import app_commands
import math
import time
import datetime
import aiosqlite

from .ui_predict import (
    local_create_bet_session, local_get_bet_session, local_update_bet_status,
    local_get_bet_session_by_message_id, local_get_bet_totals, local_get_bet_winners,
    local_get_user_all_bets, local_set_bet_close_time, local_get_expired_bets,
    generate_bet_embed, BettingView
)
from utils.database import add_item

class PredictCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.auto_close_loop.start()

    def cog_unload(self):
        self.auto_close_loop.cancel()

    @tasks.loop(seconds=30)
    async def auto_close_loop(self):
        """예약된 마감 시간이 지나면 자동으로 예측을 차단하는 마감 스케줄러 루프"""
        expired_bets = await local_get_expired_bets()
        for bet in expired_bets:
            topic = bet['topic']
            await local_update_bet_status(topic, 'closed')

            try:
                channel = self.bot.get_channel(bet['channel_id'])
                if not channel:
                    channel = await self.bot.fetch_channel(bet['channel_id'])

                msg = await channel.fetch_message(bet['message_id'])

                view = BettingView(topic, bet['option_a'], bet['option_b'], disabled=True)
                embed = await generate_bet_embed(topic, bet['option_a'], bet['option_b'], "closed")

                await msg.edit(embed=embed, view=view)
                await channel.send(f"⏰ 예약된 시간이 되어 [{topic}] 예측이 자동으로 마감되었습니다!")
            except Exception as e:
                print(f"🚨 [자동 마감 에러] {topic} 처리 중 문제 발생: {e}")

    @auto_close_loop.before_loop
    async def before_auto_close_loop(self):
        await self.bot.wait_until_ready()
        # predictions.db 전용 betting 테이블 초기화 및 보장
        async with aiosqlite.connect('predictions.db') as db:
            await db.execute('''CREATE TABLE IF NOT EXISTS betting_sessions
                         (topic TEXT PRIMARY KEY, option_a TEXT, option_b TEXT, status TEXT, message_id INTEGER, channel_id INTEGER, close_at REAL)''')
            await db.execute('''CREATE TABLE IF NOT EXISTS betting_records
                         (topic TEXT, user_id INTEGER, option TEXT, amount INTEGER, PRIMARY KEY (topic, user_id))''')
            await db.commit()

    @app_commands.command(name="예측생성", description="새로운 예측을 생성합니다. (관리자 전용)")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_bet(self, interaction: discord.Interaction, 주제: str, 옵션a: str, 옵션b: str):
        existing = await local_get_bet_session(주제)
        if existing:
            return await interaction.response.send_message("❌ 이미 동일한 이름의 주제가 존재합니다.", ephemeral=True)

        embed = await generate_bet_embed(주제, 옵션a, 옵션b, "active")
        view = BettingView(주제, 옵션a, 옵션b)

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()

        await local_create_bet_session(주제, 옵션a, 옵션b, msg.id, interaction.channel.id)

    @app_commands.command(name="예측마감", description="특정 예측의 베팅을 더 이상 받지 않도록 마감합니다. (버튼 비활성화)")
    @app_commands.checks.has_permissions(administrator=True)
    async def close_bet(self, interaction: discord.Interaction, 주제: str):
        session = await local_get_bet_session(주제)
        if not session:
            return await interaction.response.send_message("❌ 존재하지 않는 주제입니다.", ephemeral=True)
        if session['status'] != 'active':
            return await interaction.response.send_message("❌ 이미 마감되었거나 종료된 예측입니다.", ephemeral=True)

        await local_update_bet_status(주제, 'closed')

        try:
            channel = self.bot.get_channel(session['channel_id'])
            if not channel:
                channel = await self.bot.fetch_channel(session['channel_id'])
            msg = await channel.fetch_message(session['message_id'])

            view = BettingView(주제, session['option_a'], session['option_b'], disabled=True)
            embed = await generate_bet_embed(주제, session['option_a'], session['option_b'], "closed")

            await msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"Message edit failed: {e}")

        await interaction.response.send_message(f"✅ `{주제}` 예측의 베팅이 성공적으로 마감(버튼 비활성화)되었습니다.")

    @app_commands.command(name="예측결과", description="마감된 예측의 결과를 확정하고 상금을 분배합니다.")
    @app_commands.describe(메시지id="결과를 발표할 예측 메시지의 ID", 승리옵션="승리한 옵션을 선택하세요")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(승리옵션=[
        app_commands.Choice(name="옵션 A", value="A"),
        app_commands.Choice(name="옵션 B", value="B")
    ])
    async def result_bet(self, interaction: discord.Interaction, 메시지id: str, 승리옵션: app_commands.Choice[str]):
        try:
            msg_id_int = int(메시지id)
        except ValueError:
            return await interaction.response.send_message("❌ 메시지 ID는 숫자여야 합니다.", ephemeral=True)

        session = await local_get_bet_session_by_message_id(msg_id_int)
        if not session:
            return await interaction.response.send_message("❌ 해당 메시지 ID로 등록된 예측을 찾을 수 없습니다.", ephemeral=True)

        주제 = session['topic']

        if session['status'] == 'finished':
            return await interaction.response.send_message("❌ 이미 정산이 완료된 예측입니다.", ephemeral=True)
        if session['status'] == 'active':
            return await interaction.response.send_message("❌ 아직 베팅이 진행 중입니다. `/예측마감`을 먼저 사용해주세요.", ephemeral=True)

        win_opt = 승리옵션.value
        win_name = session['option_a'] if win_opt == 'A' else session['option_b']

        totals = await local_get_bet_totals(주제)
        total_a, total_b = totals['A'], totals['B']
        total_pool = total_a + total_b

        system_fee = int(total_pool * 0.05)
        distributable_pool = total_pool - system_fee
        win_pool = total_a if win_opt == 'A' else total_b

        await local_update_bet_status(주제, 'finished')

        if win_pool == 0:
            return await interaction.response.send_message(f"🏆 `{주제}`의 결과는 {win_name} 입니다!\n승리 옵션에 베팅한 유저가 없어 배당금이 시스템으로 환수되었습니다.")

        winners = await local_get_bet_winners(주제, win_opt)
        payout_logs = []

        for user_id, amount in winners:
            share_ratio = amount / win_pool
            reward = int(math.floor(share_ratio * distributable_pool))

            if reward > 0:
                await add_item(user_id, "서사급 알", reward)
                payout_logs.append(f"<@{user_id}>: {reward}개 (원금 {amount}개)")

        result_desc = f"총 베팅 풀 `{total_pool}개` 중 5%(`{system_fee}개`) 수수료를 제외한 `{distributable_pool}개`가 분배되었습니다.\n\n🎉 당첨자 목록\n"
        result_desc += "\n".join(payout_logs) if payout_logs else "상금을 받은 유저가 없습니다."

        embed = discord.Embed(title=f"🎊 예측 결과 발표: {주제}", description=result_desc, color=discord.Color.gold())
        embed.add_field(name="승리", value=f"👑 {win_name}", inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="마감예약", description="예측 마감 시간을 예약합니다. (관리자 전용)")
    @app_commands.describe(주제="예약할 예측 주제", 날짜="형식: YYYY-MM-DD (예: 2026-07-08)", 시간="형식: HH:MM (예: 18:30)")
    @app_commands.checks.has_permissions(administrator=True)
    async def schedule_close(self, interaction: discord.Interaction, 주제: str, 날짜: str, 시간: str):
        session = await local_get_bet_session(주제)
        if not session:
            return await interaction.response.send_message("❌ 존재하지 않는 주제입니다.", ephemeral=True)
        if session['status'] != 'active':
            return await interaction.response.send_message("❌ 이미 마감되었거나 종료된 예측입니다.", ephemeral=True)

        try:
            dt_str = f"{날짜} {시간}"
            target_dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            target_timestamp = target_dt.timestamp()

            if target_timestamp <= time.time():
                return await interaction.response.send_message("❌ 현재 시간보다 과거의 시간으로 예약할 수 없습니다.", ephemeral=True)

        except ValueError:
            return await interaction.response.send_message("❌ 날짜나 시간 형식이 잘못되었습니다.\n👉 올바른 예시: 날짜 `2026-07-08`, 시간 `18:30`", ephemeral=True)

        await local_set_bet_close_time(주제, target_timestamp)
        await interaction.response.send_message(f"✅ [{주제}] 예측이 {날짜} {시간}에 자동 마감되도록 예약되었습니다!", ephemeral=False)

    @app_commands.command(name="내베팅", description="내가 참여한 예측 내역과 베팅한 알의 개수를 확인합니다.")
    async def my_bets(self, interaction: discord.Interaction):
        records = await local_get_user_all_bets(interaction.user.id)

        if not records:
            return await interaction.response.send_message("🎒 아직 참여하신 예측 내역이 없습니다.", ephemeral=True)

        embed = discord.Embed(title=f"📊 {interaction.user.display_name}님의 베팅 내역", color=discord.Color.blurple())

        active_bets = []
        closed_bets = []
        finished_bets = []

        for row in records:
            topic = row['topic']
            opt_letter = row['option']
            amount = row['amount']
            status = row['status']

            opt_name = row['option_a'] if opt_letter == 'A' else row['option_b']
            text = f"{topic}\n👉 `{opt_name}`에 {amount}개 베팅"

            if status == 'active': active_bets.append(text)
            elif status == 'closed': closed_bets.append(text)
            elif status == 'finished': finished_bets.append(text)

        if active_bets:
            embed.add_field(name="🟢 진행 중인 베팅", value="\n\n".join(active_bets), inline=False)
        if closed_bets:
            embed.add_field(name="🔴 결과 대기 중 (마감됨)", value="\n\n".join(closed_bets), inline=False)
        if finished_bets:
            embed.add_field(name="🏁 정산 완료", value="\n\n".join(finished_bets), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(PredictCog(bot))