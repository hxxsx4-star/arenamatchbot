import asyncio
import io
import os

import aiohttp
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

async def local_create_bet_session(topic, opt_a, opt_b, msg_id, ch_id, logo_a=None, logo_b=None):
    async with aiosqlite.connect(PREDICT_DB_PATH) as db:
        await db.execute(
            "INSERT INTO betting_sessions (topic, option_a, option_b, status, message_id, channel_id, logo_a, logo_b) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
            (topic, opt_a, opt_b, msg_id, ch_id, logo_a, logo_b))
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

# 예측 배너 이미지 (카테고리 색상: 옵션 A=레드, 옵션 B=블루 — 버튼 색과 동일)
# 위: 팀 로고(또는 기본 VS 패널) · 아래: 베팅 비율 게이지. 하나로 합쳐서 embed 이미지로 크게 띄운다.
_BANNER_W = 700
_TOP_H = 200
_GAUGE_H = 76
_V_GAP = 14
_BAR_H = 30
_GAP = 3
_RED = (230, 103, 103, 255)
_BLUE = (57, 135, 229, 255)
_NEUTRAL = (255, 255, 255, 46)
_BANNER_FILENAME = "predict_banner.png"


async def _fetch_logo(session: aiohttp.ClientSession, url: str) -> Image.Image:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
        r.raise_for_status()
        data = await r.read()
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _fit_center(logo: Image.Image, box_w: int, box_h: int, pad: int = 24) -> Image.Image:
    aw, ah = box_w - pad * 2, box_h - pad * 2
    lw, lh = logo.size
    scale = min(aw / lw, ah / lh)
    nw, nh = max(1, int(lw * scale)), max(1, int(lh * scale))
    resized = logo.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    out.paste(resized, ((box_w - nw) // 2, (box_h - nh) // 2), resized)
    return out


def _vs_badge(radius: int, font_size: int) -> Image.Image:
    badge = Image.new("RGBA", (radius * 2, radius * 2), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(badge)
    bdraw.ellipse([0, 0, radius * 2, radius * 2], fill=(30, 30, 34, 235),
                 outline=(255, 255, 255, 235), width=5)
    font = ImageFont.truetype("font.ttf", font_size)
    txt = "VS"
    bbox = bdraw.textbbox((0, 0), txt, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    bdraw.text((radius - tw / 2, radius - th / 2 - bbox[1]), txt, font=font, fill=(255, 255, 255, 255))
    return badge


async def _render_top_panel(logo_a_url: str | None, logo_b_url: str | None) -> Image.Image:
    """위쪽 패널(가로 전체 폭): 팀 로고 두 개를 크게 좌우로 배치하거나, 없으면 기본 레드/블루 VS 패널."""
    W, H = _BANNER_W, _TOP_H
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    logos_ok = False
    if logo_a_url and logo_b_url:
        try:
            async with aiohttp.ClientSession() as session:
                logo_a, logo_b = await asyncio.gather(
                    _fetch_logo(session, logo_a_url), _fetch_logo(session, logo_b_url))
            left_bg = Image.new("RGBA", (W // 2, H), (*_RED[:3], 40))
            right_bg = Image.new("RGBA", (W // 2, H), (*_BLUE[:3], 40))
            canvas.paste(left_bg, (0, 0), left_bg)
            canvas.paste(right_bg, (W // 2, 0), right_bg)
            canvas.alpha_composite(_fit_center(logo_a, W // 2, H), (0, 0))
            canvas.alpha_composite(_fit_center(logo_b, W // 2, H), (W // 2, 0))
            logos_ok = True
        except Exception as e:
            print(f"🚨 [승부예측] 팀 로고 다운로드 실패: {e}")

    if not logos_ok:
        left_bg = Image.new("RGBA", (W // 2, H), _RED)
        right_bg = Image.new("RGBA", (W // 2, H), _BLUE)
        canvas.paste(left_bg, (0, 0))
        canvas.paste(right_bg, (W // 2, 0))

    ImageDraw.Draw(canvas).line([(W // 2, 0), (W // 2, H)], fill=(255, 255, 255, 160), width=3)

    badge_r = 46
    badge = _vs_badge(badge_r, 40)
    canvas.alpha_composite(badge, (W // 2 - badge_r, H // 2 - badge_r))

    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=28, fill=255)
    final = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    final.paste(canvas, (0, 0), mask)
    return final


def _gauge_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("font_ui.ttf", size)


def _render_gauge_panel(total_a: int, total_b: int) -> Image.Image:
    """옵션 A/B 베팅 비율을 보여주는 둥근 막대 이미지를 만든다."""
    W, H, BAR_H = _BANNER_W, _GAUGE_H, _BAR_H
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
    return Image.alpha_composite(bar_masked, labels)


async def build_predict_banner(total_a: int, total_b: int, logo_a: str | None = None,
                               logo_b: str | None = None) -> discord.File:
    """팀 로고(또는 기본 VS 패널) + 베팅 비율 게이지를 세로로 합친 배너 이미지 하나를 만든다."""
    top = await _render_top_panel(logo_a, logo_b)
    gauge = _render_gauge_panel(total_a, total_b)

    W = _BANNER_W
    H = _TOP_H + _V_GAP + _GAUGE_H
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.alpha_composite(top, (0, 0))
    canvas.alpha_composite(gauge, (0, _TOP_H + _V_GAP))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename=_BANNER_FILENAME)


async def generate_bet_embed(topic, opt_a, opt_b, status="active", logo_a=None, logo_b=None):
    """(embed, files) 튜플을 반환한다. files 를 send/edit 시 함께 첨부해야 배너 이미지가 보인다.

    logo_a/logo_b 를 넘기면(e스포츠 자동생성 예측) 두 팀 로고를 크게 띄우고,
    없거나 다운로드 실패 시 기본 VS 패널로 대체한다.
    """
    totals = await local_get_bet_totals(topic)
    total_a = totals['A']
    total_b = totals['B']
    total_pool = total_a + total_b

    banner_file = await build_predict_banner(total_a, total_b, logo_a, logo_b)

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
    embed.set_image(url=f"attachment://{_BANNER_FILENAME}")

    return embed, [banner_file]

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
        # 팀 로고 다운로드 등으로 임베드 재생성이 3초를 넘길 수 있어 먼저 defer 한다.
        # (안 하면 "상호작용 실패"가 뜬다 — 베팅 자체는 처리돼도 응답을 못 보내서 실패로 보임)
        await interaction.response.defer(ephemeral=True)

        try:
            bet_amount = int(self.amount.value)
        except ValueError:
            return await interaction.followup.send("❌ 올바른 숫자를 입력해주세요.", ephemeral=True)

        if bet_amount <= 0:
            return await interaction.followup.send("❌ 1P 이상 베팅해야 합니다.", ephemeral=True)

        user_id = interaction.user.id

        # 마감/취소된 뒤에 열어둔 모달로 제출하면 포인트만 빠져나가므로 여기서도 막는다.
        session = await local_get_bet_session(self.topic)
        if not session:
            return await interaction.followup.send(
                "❌ 사라진 예측입니다.", ephemeral=True)
        if session['status'] != 'active':
            label = {"closed": "마감된", "cancelled": "취소된", "finished": "정산이 끝난"}.get(
                session['status'], "진행 중이 아닌")
            return await interaction.followup.send(
                f"❌ 이미 {label} 예측이라 베팅할 수 없습니다.", ephemeral=True)

        existing_bet = await local_get_user_bet(self.topic, user_id)
        if existing_bet and existing_bet[0] != self.option:
            return await interaction.followup.send("❌ 이미 반대쪽 옵션에 베팅하셨습니다! 양방향 베팅은 불가능합니다.", ephemeral=True)

        success = await spend_points(user_id, bet_amount)
        if not success:
            current = await get_points(user_id)
            return await interaction.followup.send(f"❌ 포인트가 부족합니다! (현재 보유: {format_num(current)}P)", ephemeral=True)

        ok = await local_add_bet_record(self.topic, user_id, self.option, bet_amount)
        if not ok:
            # 반대쪽에 이미 베팅된 상태(동시 클릭 등). 차감한 포인트를 되돌린다.
            await add_points(user_id, bet_amount)
            return await interaction.followup.send(
                "❌ 이미 반대쪽 옵션에 베팅되어 있습니다. 포인트는 돌려드렸습니다.", ephemeral=True)

        session = await local_get_bet_session(self.topic)
        new_embed, gauge_files = await generate_bet_embed(
            self.topic, session['option_a'], session['option_b'], session['status'],
            logo_a=session['logo_a'], logo_b=session['logo_b'])

        await interaction.message.edit(embed=new_embed, view=self.view, attachments=gauge_files)
        await interaction.followup.send(f"✅ 성공적으로 `{format_num(bet_amount)}P`를 베팅했습니다!", ephemeral=True)

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
