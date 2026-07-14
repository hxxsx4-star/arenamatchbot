import os
import json
import time
import sqlite3
from datetime import datetime

import discord

try:
    import aiosqlite
except ImportError:  # 로그 릴레이(로그봇)에서만 필요
    aiosqlite = None

# =====================================================================
# 종합게임 아레나 - 공유 로그 설정
# 4개 봇(펫봇/메인봇/내전봇/로그봇)이 동일하게 사용하는 파일입니다.
#
# ▸ 모든 로그는 "로그봇"이 최종적으로 채널에 기록합니다.
# ▸ 행동을 한 봇(펫/메인/내전)은 로그를 직접 올리지 않고 공유 큐에 적재하며,
#   로그봇의 릴레이(log_relay)가 큐를 비우면서 아래 채널로 전송합니다.
# =====================================================================

# ───────── 로그 채널 ID ─────────
JOIN_LOG_CH = 1526605563696251004        # 입장 로그
LEAVE_LOG_CH = 1526605585976393878       # 퇴장 로그
CHAT_LOG_CH = 1526614629395071057        # 채팅 로그 (수정/삭제)
VOICE_LOG_CH = 1526614654875340843       # 음성 로그 (입장/퇴장/이동/연결끊김)
MATCH_LOG_CH = 1526614728783171584       # 내전 로그
POINT_GIVE_LOG_CH = 1526614949747626138  # 포인트 지급 로그
POINT_TAKE_LOG_CH = 1526614982110871572  # 포인트 회수 로그
HATCH_LOG_CH = 1526637800995291207       # 펫 알까기 로그
EGG_GIVE_LOG_CH = 1526637877373571092    # 펫 알 지급 로그
EGG_TAKE_LOG_CH = 1526637912664309880    # 펫 알 회수 로그
ITEM_GIVE_LOG_CH = 1526638185860300870   # 아이템 지급 로그
ITEM_TAKE_LOG_CH = 1526638215090671687   # 아이템 회수 로그
SHOP_LOG_CH = 1526638410654289940        # 상점 구매 로그

# 채널 미지정(비활성). 나중에 채널 ID를 넣으면 자동으로 활성화됩니다.
WALK_LOG_CH = 0        # 산책 로그
ITEM_USE_LOG_CH = 0    # 아이템 사용 로그

# 하위호환 별칭 (기존 펫 코드가 쓰던 통합 상수)
ITEM_GIVE_TAKE_LOG_CH = ITEM_GIVE_LOG_CH
EGG_GIVE_TAKE_LOG_CH = EGG_GIVE_LOG_CH

# ───────── 공유 로그 큐 ─────────
# stats.json 과 동일하게 모든 봇이 공유하는 디렉터리에 큐 DB를 둡니다.
LOG_QUEUE_DIR = os.environ.get("ARENA_SHARED_DIR", "/home/hxxsx4/shared_data")
LOG_QUEUE_PATH = os.path.join(LOG_QUEUE_DIR, "log_queue.db")

_MAX_ATTEMPTS = 5
_table_ready = False


def _connect() -> sqlite3.Connection:
    os.makedirs(LOG_QUEUE_DIR, exist_ok=True)
    conn = sqlite3.connect(LOG_QUEUE_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_log_queue():
    """봇 시작 시 한 번 호출해 큐 테이블을 준비합니다. (모든 봇 공용)"""
    global _table_ready
    conn = _connect()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS log_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL,
                channel_id INTEGER,
                embed_json TEXT,
                posted INTEGER DEFAULT 0,
                attempts INTEGER DEFAULT 0
            )"""
        )
        conn.commit()
    finally:
        conn.close()
    _table_ready = True


def enqueue_embed(channel_id, embed_dict):
    """생산자(펫/메인/내전 봇)용: 임베드 dict 를 공유 큐에 적재합니다.

    channel_id 가 0/None 이면(비활성 로그) 조용히 무시합니다.
    """
    if not channel_id:
        return
    try:
        if not _table_ready:
            init_log_queue()
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO log_queue (created_at, channel_id, embed_json, posted, attempts) "
                "VALUES (?, ?, ?, 0, 0)",
                (time.time(), int(channel_id), json.dumps(embed_dict, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"🚨 [로그 큐 적재 실패] 채널 ID: {channel_id} | 에러: {e}")


async def send_log_embed(bot, channel_id, title, description, user, color, extra_footer=""):
    """기존 시그니처 호환용 헬퍼. 내부적으로는 큐에 적재합니다.

    (bot 인자는 하위호환을 위해 남겨두었고 사용하지 않습니다.)
    """
    try:
        embed = discord.Embed(
            title=title, description=description, color=color, timestamp=datetime.now()
        )
        footer_text = f"유저: {user.display_name} ({user.id})"
        if extra_footer:
            footer_text += f" | {extra_footer}"
        icon = user.display_avatar.url if getattr(user, "display_avatar", None) else None
        embed.set_footer(text=footer_text, icon_url=icon)
        enqueue_embed(channel_id, embed.to_dict())
    except Exception as e:
        print(f"🚨 [로그 전송 실패] 채널 ID: {channel_id} | 에러: {e}")


# ───────── 로그봇(릴레이) 전용 함수 ─────────
async def fetch_pending(limit: int = 30):
    """아직 전송되지 않은 로그 큐 항목을 가져옵니다. (로그봇 전용)"""
    if aiosqlite is None:
        raise RuntimeError("aiosqlite 가 필요합니다. (pip install aiosqlite)")
    async with aiosqlite.connect(LOG_QUEUE_PATH, timeout=10) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS log_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL, channel_id INTEGER, embed_json TEXT,
                posted INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0)"""
        )
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, channel_id, embed_json FROM log_queue "
            "WHERE posted = 0 AND attempts < ? ORDER BY id LIMIT ?",
            (_MAX_ATTEMPTS, limit),
        ) as cur:
            return await cur.fetchall()


async def mark_posted(ids):
    if not ids or aiosqlite is None:
        return
    async with aiosqlite.connect(LOG_QUEUE_PATH, timeout=10) as db:
        await db.executemany("UPDATE log_queue SET posted = 1 WHERE id = ?", [(i,) for i in ids])
        await db.commit()


async def mark_failed(ids):
    """전송 실패 항목의 재시도 횟수를 올립니다. (_MAX_ATTEMPTS 초과 시 자동 폐기)"""
    if not ids or aiosqlite is None:
        return
    async with aiosqlite.connect(LOG_QUEUE_PATH, timeout=10) as db:
        await db.executemany(
            "UPDATE log_queue SET attempts = attempts + 1 WHERE id = ?", [(i,) for i in ids]
        )
        await db.commit()
