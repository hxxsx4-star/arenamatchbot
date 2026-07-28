import io
import os

import discord
import time
import aiosqlite
from PIL import Image, ImageDraw, ImageFont
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

async def local_get_all_bets(topic):
    """해당 주제의 모든 베팅 (환불용)."""
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, amount FROM betting_records WHERE topic = ?", (topic,)) as cursor:
            return await cursor.fetchall()


async def local_delete_bet_records(topic):
    """환불 완료 후 베팅 기록을 지운다. (중복 환불 방지)"""
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        await db.execute("DELETE FROM betting_records WHERE topic = ?", (topic,))
        await db.commit()


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

# 베팅 비율 게이지 이미지 (카테고리 색상: 옵션 A=레드, 옵션 B=블루 — 버튼 색과 동일)
_GAUGE_W, _GAUGE_H = 700, 76
_BAR_H = 30
_GAP = 3
_RED = (230, 103, 103, 255)
_BLUE = (57, 135, 229, 255)
_NEUTRAL = (255, 255, 255, 46)
_GAUGE_FILENAME = "gauge.png"


def _gauge_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("font_ui.ttf", size)


def generate_gauge_image(total_a: int, total_b: int) -> discord.File:
    """옵션 A/B 베팅 비율을 보여주는 둥근 막대 이미지를 만든다."""
    W, H, BAR_H = _GAUGE_W, _GAUGE_H, _BAR_H
    y0 = (H - BAR_H) // 2
    y1 = y0 + BAR_H
    radius = BAR_H // 2

    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, y0, W - 1, y1], radius=radius, fill=255)

    bar_fill = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(bar_fill)
    labels = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(labels)
    font = _gauge_font(18)
    small_font = _gauge_font(14)

    total = total_a + total_b
    if total == 0:
        bdraw.rounded_rectangle([0, y0, W - 1, y1], radius=radius, fill=_NEUTRAL)
        msg = "아직 베팅이 없어요"
        bbox = ldraw.textbbox((0, 0), msg, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        ldraw.text(((W - tw) / 2, y0 + (BAR_H - th) / 2 - bbox[1]), msg, font=font, fill=(255, 255, 255, 200))
    else:
        pct_a = round(total_a / total * 100)
        pct_b = 100 - pct_a
        min_w = BAR_H

        if pct_a <= 0:
            width_a = 0
        elif pct_b <= 0:
            width_a = W
        else:
            width_a = max(round(W * pct_a / 100), min_w)
            width_a = min(width_a, W - min_w - _GAP)

        if pct_a > 0:
            seg_end = max(0, width_a - (_GAP if pct_b > 0 else 0))
            bdraw.rounded_rectangle([0, y0, max(seg_end, 1), y1], radius=radius, fill=_RED)
        if pct_b > 0:
            seg_start = width_a + (_GAP if pct_a > 0 else 0)
            bdraw.rounded_rectangle([min(seg_start, W - 2), y0, W - 1, y1], radius=radius, fill=_BLUE)

        def label(pct, seg_left, seg_right):
            if pct <= 0:
                return
            txt = f"{pct}%"
            cx = (seg_left + seg_right) / 2
            seg_w = seg_right - seg_left
            bbox = ldraw.textbbox((0, 0), txt, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if tw + 16 <= seg_w:
                ty = y0 + (BAR_H - th) / 2 - bbox[1]
                ldraw.text((cx - tw / 2, ty), txt, font=font, fill=(255, 255, 255, 235))
            else:
                bbox_s = ldraw.textbbox((0, 0), txt, font=small_font)
                tw_s, th_s = bbox_s[2] - bbox_s[0], bbox_s[3] - bbox_s[1]
                ty = y0 - th_s - 6 - bbox_s[1]
                ldraw.text((cx - tw_s / 2, ty), txt, font=small_font, fill=(225, 225, 225, 235))

        a_right = width_a if pct_b > 0 else W
        label(pct_a, 0, a_right if pct_a > 0 else 0)
        label(pct_b, width_a, W)

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bar_masked = Image.composite(bar_fill, canvas, mask)
    final = Image.alpha_composite(bar_masked, labels)

    buf = io.BytesIO()
    final.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename=_GAUGE_FILENAME)


async def generate_bet_embed(topic, opt_a, opt_b, status="active"):
    """(embed, file) 튜플을 반환한다. file 은 send/edit 시 함께 첨부해야 게이지 이미지가 보인다."""
    totals = await local_get_bet_totals(topic)
    total_a = totals['A']
    total_b = totals['B']
    total_pool = total_a + total_b

    gauge_file = generate_gauge_image(total_a, total_b)

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

    embed.add_field(name="현재 베팅 비율", value=f"💰 총 상금 풀: {format_num(total_pool)}P (수수료 5% 제외 후 분배)", inline=False)
    embed.set_image(url=f"attachment://{_GAUGE_FILENAME}")

    return embed, gauge_file

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

        # 마감/취소된 뒤에 열어둔 모달로 제출하면 포인트만 빠져나가므로 여기서도 막는다.
        session = await local_get_bet_session(self.topic)
        if not session:
            return await interaction.response.send_message(
                "❌ 사라진 예측입니다.", ephemeral=True)
        if session['status'] != 'active':
            label = {"closed": "마감된", "cancelled": "취소된", "finished": "정산이 끝난"}.get(
                session['status'], "진행 중이 아닌")
            return await interaction.response.send_message(
                f"❌ 이미 {label} 예측이라 베팅할 수 없습니다.", ephemeral=True)

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
        new_embed, gauge_file = await generate_bet_embed(
            self.topic, session['option_a'], session['option_b'], session['status'])

        await interaction.message.edit(embed=new_embed, view=self.view, attachments=[gauge_file])
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
