import os
import aiosqlite
import time
import json

# 기본은 작업 디렉터리의 legends.db. ARENA_DB_PATH 로 재정의 가능(도커 볼륨 등).
DB_PATH = os.environ.get("ARENA_DB_PATH", 'legends.db')

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # 💡 기존 전설이/유저 관련 테이블
        await db.execute('''CREATE TABLE IF NOT EXISTS users
                     (user_id INTEGER PRIMARY KEY, points INTEGER, max_star_reached INTEGER,
                     egg_legendary INTEGER, egg_mythic INTEGER, egg_prestige INTEGER)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS user_legends
                     (user_id INTEGER PRIMARY KEY, name TEXT, rarity TEXT,
                     level INTEGER, exp INTEGER, fullness INTEGER, intimacy INTEGER, fatigue INTEGER,
                     cleanliness INTEGER, low_clean_since REAL, low_full_since REAL)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS user_multi_pets
                     (user_id INTEGER PRIMARY KEY, pet_data TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS user_items
                     (user_id INTEGER, item_name TEXT, amount INTEGER,
                     PRIMARY KEY (user_id, item_name))''')
        await db.execute('''CREATE TABLE IF NOT EXISTS active_buffs
                     (user_id INTEGER, buff_name TEXT, expires_at REAL, vc_seconds_left REAL,
                     PRIMARY KEY (user_id, buff_name))''')
        await db.execute('''CREATE TABLE IF NOT EXISTS user_achievements
                     (user_id INTEGER PRIMARY KEY, synth_count INTEGER DEFAULT 0)''')

        # 💡 승부예측 시스템 관련 테이블
        await db.execute('''CREATE TABLE IF NOT EXISTS betting_sessions
                     (topic TEXT PRIMARY KEY, option_a TEXT, option_b TEXT, status TEXT, message_id INTEGER, channel_id INTEGER)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS betting_records
                     (topic TEXT, user_id INTEGER, option TEXT, amount INTEGER, PRIMARY KEY (topic, user_id))''')

        # 💡 예약 마감 업데이트 (기존 테이블에 마감 시간 칼럼 추가)
        try:
            await db.execute("ALTER TABLE betting_sessions ADD COLUMN close_at REAL")
        except Exception:
            pass # 이미 칼럼이 추가되어 있으면 오류를 무시하고 넘어갑니다.

        # 💡 내전 세션 (봇 재시작에도 진행 중인 내전이 이어지도록 디스크에 보관)
        await db.execute('''CREATE TABLE IF NOT EXISTS match_sessions
                     (message_id INTEGER PRIMARY KEY, channel_id INTEGER, guild_id INTEGER,
                      host_id INTEGER, game TEXT, capacity INTEGER, phase TEXT,
                      participants TEXT, captains TEXT, team1 TEXT, team2 TEXT, pool TEXT,
                      draft_idx INTEGER, mvp_votes TEXT, winner INTEGER, updated_at REAL)''')

        await db.commit()


# ==========================================
# 💡 내전 세션 저장/복원
# ==========================================

async def save_match_session(row: dict):
    """내전 세션 상태를 통째로 저장(upsert)합니다. 상태가 바뀔 때마다 호출."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            '''INSERT INTO match_sessions
                 (message_id, channel_id, guild_id, host_id, game, capacity, phase,
                  participants, captains, team1, team2, pool, draft_idx, mvp_votes, winner, updated_at)
               VALUES (:message_id, :channel_id, :guild_id, :host_id, :game, :capacity, :phase,
                       :participants, :captains, :team1, :team2, :pool, :draft_idx, :mvp_votes,
                       :winner, :updated_at)
               ON CONFLICT(message_id) DO UPDATE SET
                 phase=excluded.phase, participants=excluded.participants,
                 captains=excluded.captains, team1=excluded.team1, team2=excluded.team2,
                 pool=excluded.pool, draft_idx=excluded.draft_idx, mvp_votes=excluded.mvp_votes,
                 winner=excluded.winner, updated_at=excluded.updated_at''',
            row,
        )
        await db.commit()


async def load_active_match_sessions(max_age_sec: int = 24 * 3600):
    """아직 끝나지 않은 내전 세션들을 불러옵니다. (봇 시작 시 뷰 재등록용)"""
    cutoff = time.time() - max_age_sec
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM match_sessions WHERE phase != 'done' AND updated_at >= ? ORDER BY updated_at",
            (cutoff,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def purge_old_match_sessions(max_age_sec: int = 7 * 24 * 3600) -> int:
    """오래된 내전 세션 기록을 정리합니다."""
    cutoff = time.time() - max_age_sec
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM match_sessions WHERE updated_at < ?", (cutoff,))
        await db.commit()
        return cur.rowcount

# ==========================================
# 💡 기존 유저 및 전설이 관련 함수
# ==========================================

async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            data = await cursor.fetchone()
        if not data:
            await db.execute("INSERT INTO users VALUES (?, 3000, 0, 0, 0, 0)", (user_id,))
            await db.commit()
            data = (user_id, 3000, 0, 0, 0, 0)
        return data

async def update_user_points(user_id, points_change):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET points = MAX(0, points + ?) WHERE user_id = ?", (points_change, user_id))
        await db.commit()

async def update_max_star(user_id, star):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET max_star_reached = MAX(max_star_reached, ?) WHERE user_id = ?", (star, user_id))
        await db.commit()

async def get_legend_data(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT pet_data FROM user_multi_pets WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row['pet_data']:
                try: return json.loads(row['pet_data'])
                except json.JSONDecodeError: pass
        async with db.execute("SELECT * FROM user_legends WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

async def save_legend_data(user_id, data: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        json_data = json.dumps(data, ensure_ascii=False)
        await db.execute('''INSERT OR REPLACE INTO user_multi_pets (user_id, pet_data)
                     VALUES (?, ?)''', (user_id, json_data))
        await db.commit()

async def add_item(user_id, item_name, amount=1):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO user_items (user_id, item_name, amount) VALUES (?, ?, ?) ON CONFLICT(user_id, item_name) DO UPDATE SET amount = amount + ?", (user_id, item_name, amount, amount))
        await db.commit()

async def consume_item(user_id, item_name, amount=1) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT amount FROM user_items WHERE user_id = ? AND item_name = ?", (user_id, item_name)) as cursor:
            row = await cursor.fetchone()
        if not row or row[0] < amount: return False
        await db.execute("UPDATE user_items SET amount = amount - ? WHERE user_id = ? AND item_name = ?", (amount, user_id, item_name))
        await db.execute("DELETE FROM user_items WHERE amount <= 0")
        await db.commit()
        return True

async def add_buff(user_id, buff_name, duration_sec=0, vc_sec=0):
    async with aiosqlite.connect(DB_PATH) as db:
        expires_at = time.time() + duration_sec if duration_sec > 0 else 0
        await db.execute('''INSERT OR REPLACE INTO active_buffs (user_id, buff_name, expires_at, vc_seconds_left)
                     VALUES (?, ?, ?, ?)''', (user_id, buff_name, expires_at, vc_sec))
        await db.commit()

async def get_active_buffs(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        now = time.time()
        await db.execute("DELETE FROM active_buffs WHERE expires_at > 0 AND expires_at < ?", (now,))
        await db.execute("DELETE FROM active_buffs WHERE expires_at = 0 AND vc_seconds_left <= 0")
        await db.commit()
        async with db.execute("SELECT buff_name, expires_at, vc_seconds_left FROM active_buffs WHERE user_id = ?", (user_id,)) as cursor:
            rows = await cursor.fetchall()
        return rows

async def get_or_migrate_data(user_id):
    data = await get_legend_data(user_id)
    if not data: return {'pets': [], 'active_idx': 0}
    if 'pets' not in data: return {'pets': [data], 'active_idx': 0}
    return data

async def get_item_amount(user_id, item_name):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT amount FROM user_items WHERE user_id = ? AND item_name = ?", (user_id, item_name)) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

async def set_item_amount(user_id, item_name, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        if amount <= 0:
            await db.execute("DELETE FROM user_items WHERE user_id = ? AND item_name = ?", (user_id, item_name))
        else:
            await db.execute("INSERT INTO user_items (user_id, item_name, amount) VALUES (?, ?, ?) ON CONFLICT(user_id, item_name) DO UPDATE SET amount = ?", (user_id, item_name, amount, amount))
        await db.commit()

async def add_synth_count(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO user_achievements (user_id, synth_count) VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET synth_count = synth_count + 1", (user_id,))
        await db.commit()

async def get_synth_count(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT synth_count FROM user_achievements WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

async def get_top_epic_egg_owners():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, amount FROM user_items WHERE item_name = '서사급 알' ORDER BY amount DESC LIMIT 5") as cursor:
            return await cursor.fetchall()

# ==========================================
# 💡 승부예측 전용 함수 모음
# ==========================================

async def create_bet_session(topic, opt_a, opt_b, msg_id, ch_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO betting_sessions (topic, option_a, option_b, status, message_id, channel_id) VALUES (?, ?, ?, 'active', ?, ?)",
                         (topic, opt_a, opt_b, msg_id, ch_id))
        await db.commit()

async def get_bet_session(topic):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM betting_sessions WHERE topic = ?", (topic,)) as cursor:
            return await cursor.fetchone()

# 💡 신규 추가: 메시지 ID로 세션 조회
async def get_bet_session_by_message_id(message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM betting_sessions WHERE message_id = ?", (message_id,)) as cursor:
            return await cursor.fetchone()

async def update_bet_status(topic, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE betting_sessions SET status = ? WHERE topic = ?", (status, topic))
        await db.commit()

async def add_bet_record(topic, user_id, option, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''INSERT INTO betting_records (topic, user_id, option, amount)
                          VALUES (?, ?, ?, ?)
                          ON CONFLICT(topic, user_id) DO UPDATE SET amount = amount + ?''',
                       (topic, user_id, option, amount, amount))
        await db.commit()

async def get_bet_totals(topic):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT option, SUM(amount) FROM betting_records WHERE topic = ? GROUP BY option", (topic,)) as cursor:
            rows = await cursor.fetchall()

        totals = {'A': 0, 'B': 0}
        for row in rows:
            totals[row[0]] = row[1]
        return totals

async def get_bet_winners(topic, win_option):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, amount FROM betting_records WHERE topic = ? AND option = ?", (topic, win_option)) as cursor:
            return await cursor.fetchall()

async def get_user_bet(topic, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT option, amount FROM betting_records WHERE topic = ? AND user_id = ?", (topic, user_id)) as cursor:
            return await cursor.fetchone()

async def get_user_all_bets(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('''
            SELECT r.topic, r.option, r.amount, s.status, s.option_a, s.option_b
            FROM betting_records r
            JOIN betting_sessions s ON r.topic = s.topic
            WHERE r.user_id = ?
        ''', (user_id,)) as cursor:
            return await cursor.fetchall()

# ==========================================
# 💡 승부예측 자동 마감(예약) 전용 함수
# ==========================================

async def set_bet_close_time(topic, close_at):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE betting_sessions SET close_at = ? WHERE topic = ?", (close_at, topic))
        await db.commit()

async def get_expired_bets():
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # 현재 시간보다 예약된 마감 시간이 지났고, 아직 'active' 상태인 베팅만 가져옴
        async with db.execute("SELECT * FROM betting_sessions WHERE status = 'active' AND close_at IS NOT NULL AND close_at <= ?", (now,)) as cursor:
            return await cursor.fetchall()

async def get_user_items(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT item_name, amount FROM user_items WHERE user_id = ? AND amount > 0", (user_id,)) as cursor:
            rows = await cursor.fetchall()
        return rows