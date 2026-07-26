import os

import discord
import time
import aiosqlite
from utils.stats import spend_points, add_points, get_points, format_num

# 승부예측 DB 경로.
# 예전엔 상대경로('predictions.db')라 도커에서 /app 안에 만들어졌고,
# 컨테이너를 다시 만들 때마다 통째로 사라졌다. 베팅 포인트는 공유 stats.json 에서
# 이미 차감된 뒤라 기록만 날아가면 정산도 환불도 불가능해진다.
# → 반드시 공유 볼륨에 둔다.
SHARED_DIR = os.environ.get("ARENA_SHARED_DIR", "/home/hxxsx4/shared_data")
PREDICT_DB_PATH = os.environ.get("ARENA_PREDICT_DB_PATH",
                                 os.path.join(SHARED_DIR, "predictions.db"))

# 💡 predictions.db 전용 독립 비동기 로컬 데이터 전송 함수군
async def local_get_bet_session(topic):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM betting_sessions WHERE topic = ?", (topic,)) as cursor:
            return await cursor.fetchone()

async def local_get_bet_session_by_message_id(message_id):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM betting_sessions WHERE message_id = ?", (message_id,)) as cursor:
            return await cursor.fetchone()

async def local_update_bet_status(topic, status):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        await db.execute("UPDATE betting_sessions SET status = ? WHERE topic = ?", (status, topic))
        await db.commit()

async def local_create_bet_session(topic, opt_a, opt_b, msg_id, ch_id):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        await db.execute("INSERT INTO betting_sessions (topic, option_a, option_b, status, message_id, channel_id) VALUES (?, ?, ?, 'active', ?, ?)",
                         (topic, opt_a, opt_b, msg_id, ch_id))
        await db.commit()

async def local_add_bet_record(topic, user_id, option, amount) -> bool:
    """베팅을 기록한다. 이미 반대쪽에 걸어둔 상태면 아무것도 하지 않고 False.

    양방향 베팅 차단을 '조회 후 차감'으로만 하면, 두 옵션을 동시에 누를 때
    둘 다 통과해 한쪽 기록이 반대 옵션 금액에 합쳐질 수 있다.
    WHERE 조건으로 옵션이 같을 때만 누적되게 해서 DB 수준에서 막는다.
    """
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        cur = await db.execute('''INSERT INTO betting_records (topic, user_id, option, amount)
                          VALUES (?, ?, ?, ?)
                          ON CONFLICT(topic, user_id) DO UPDATE SET amount = amount + excluded.amount
                          WHERE betting_records.option = excluded.option''',
                       (topic, user_id, option, amount))
        await db.commit()
        return cur.rowcount > 0

async def local_get_bet_totals(topic):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        async with db.execute("SELECT option, SUM(amount) FROM betting_records WHERE topic = ? GROUP BY option", (topic,)) as cursor:
            rows = await cursor.fetchall()
        totals = {'A': 0, 'B': 0}
        for row in rows:
            totals[row[0]] = row[1]
        return totals

async def local_get_bet_winners(topic, win_option):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        async with db.execute("SELECT user_id, amount FROM betting_records WHERE topic = ? AND option = ?", (topic, win_option)) as cursor:
            return await cursor.fetchall()

async def local_get_user_bet(topic, user_id):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        async with db.execute("SELECT option, amount FROM betting_records WHERE topic = ? AND user_id = ?", (topic, user_id)) as cursor:
            return await cursor.fetchone()

async def local_get_user_all_bets(user_id):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('''
            SELECT r.topic, r.option, r.amount, s.status, s.option_a, s.option_b
            FROM betting_records r
            JOIN betting_sessions s ON r.topic = s.topic
            WHERE r.user_id = ?
        ''', (user_id,)) as cursor:
            return await cursor.fetchall()

async def local_set_bet_close_time(topic, close_at):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        await db.execute("UPDATE betting_sessions SET close_at = ? WHERE topic = ?", (close_at, topic))
        await db.commit()

async def local_get_expired_bets():
    now = time.time()
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM betting_sessions WHERE status = 'active' AND close_at IS NOT NULL AND close_at <= ?", (now,)) as cursor:
            return await cursor.fetchall()

def generate_progress_bar(total_a, total_b):
    total = total_a + total_b
    if total == 0:
        return "⬛⬛⬛⬛⬛⬛⬛⬛⬛⬛"
    ratio_a = total_a / total
    blocks_a = round(ratio_a * 10)
    blocks_b = 10 - blocks_a
    return ("🟥" * blocks_a) + ("🟦" * blocks_b)

async def generate_bet_embed(topic, opt_a, opt_b, status="active"):
    totals = await local_get_bet_totals(topic)
    total_a = totals['A']
    total_b = totals['B']
    total_pool = total_a + total_b

    gauge = generate_progress_bar(total_a, total_b)

    dist_pool = total_pool * 0.95
    odds_a = round(dist_pool / total_a, 2) if total_a > 0 else 1.00
    odds_b = round(dist_pool / total_b, 2) if total_b > 0 else 1.00

    color = discord.Color.green() if status == "active" else discord.Color.dark_gray()
    title_prefix = "🟢 [진행중]" if status == "active" else "🔴 [마감됨]"

    if status == "active":
        description = "👇 아래 버튼을 누른 후 베팅할 **포인트**를 직접 입력하세요!"
    else:
        description = "🛑 이 예측은 베팅이 마감되었습니다."

    embed = discord.Embed(title=f"{title_prefix} 예측: {topic}", description=description, color=color)

    embed.add_field(name=f"🟥 옵션 A: {opt_a}", value=f"📊 배당률: {odds_a}배\n(누적: {format_num(total_a)}P)", inline=True)
    embed.add_field(name=f"🟦 옵션 B: {opt_b}", value=f"📊 배당률: {odds_b}배\n(누적: {format_num(total_b)}P)", inline=True)

    embed.add_field(name="현재 베팅 비율", value=f"{gauge}\n💰 총 상금 풀: {format_num(total_pool)}P (수수료 5% 제외 후 분배)", inline=False)

    return embed

class BetInputModal(discord.ui.Modal):
    def __init__(self, topic: str, option: str, opt_name: str, view: discord.ui.View):
        super().__init__(title=f"{opt_name}에 베팅하기")
        self.topic = topic
        self.option = option  # 'A' or 'B'
        self.view = view

        self.amount = discord.ui.TextInput(
            label="베팅할 포인트를 입력하세요",
            placeholder="숫자만 입력 (예: 100)",
            min_length=1,
            max_length=9,
            required=True
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet_amount = int(self.amount.value)
        except ValueError:
            return await interaction.response.send_message("❌ 올바른 숫자를 입력해주세요.", ephemeral=True)

        if bet_amount <= 0:
            return await interaction.response.send_message("❌ 1P 이상 베팅해야 합니다.", ephemeral=True)

        user_id = interaction.user.id

        existing_bet = await local_get_user_bet(self.topic, user_id)
        if existing_bet and existing_bet[0] != self.option:
            return await interaction.response.send_message("❌ 이미 반대쪽 옵션에 베팅하셨습니다! 양방향 베팅은 불가능합니다.", ephemeral=True)

        success = await spend_points(user_id, bet_amount)
        if not success:
            current = await get_points(user_id)
            return await interaction.response.send_message(f"❌ 포인트가 부족합니다! (현재 보유: {format_num(current)}P)", ephemeral=True)

        ok = await local_add_bet_record(self.topic, user_id, self.option, bet_amount)
        if not ok:
            # 반대쪽에 이미 베팅된 상태(동시 클릭 등). 차감한 포인트를 되돌린다.
            await add_points(user_id, bet_amount)
            return await interaction.response.send_message(
                "❌ 이미 반대쪽 옵션에 베팅되어 있습니다. 포인트는 돌려드렸습니다.", ephemeral=True)

        session = await local_get_bet_session(self.topic)
        new_embed = await generate_bet_embed(self.topic, session['option_a'], session['option_b'], session['status'])

        await interaction.message.edit(embed=new_embed, view=self.view)
        await interaction.response.send_message(f"✅ 성공적으로 `{format_num(bet_amount)}P`를 베팅했습니다!", ephemeral=True)

class BettingView(discord.ui.View):
    def __init__(self, topic: str, opt_a_name: str, opt_b_name: str, disabled: bool = False):
        super().__init__(timeout=None)
        self.topic = topic
        self.opt_a_name = opt_a_name
        self.opt_b_name = opt_b_name

        self.bet_a_button.disabled = disabled
        self.bet_b_button.disabled = disabled

    @discord.ui.button(label="옵션 A 베팅", style=discord.ButtonStyle.danger, emoji="🟥", custom_id="bet_a")
    async def bet_a_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = await local_get_bet_session(self.topic)
        if session['status'] != 'active':
            return await interaction.response.send_message("❌ 이미 마감된 예측입니다.", ephemeral=True)
        await interaction.response.send_modal(BetInputModal(self.topic, 'A', self.opt_a_name, self))

    @discord.ui.button(label="옵션 B 베팅", style=discord.ButtonStyle.primary, emoji="🟦", custom_id="bet_b")
    async def bet_b_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = await local_get_bet_session(self.topic)
        if session['status'] != 'active':
            return await interaction.response.send_message("❌ 이미 마감된 예측입니다.", ephemeral=True)
        await interaction.response.send_modal(BetInputModal(self.topic, 'B', self.opt_b_name, self))
