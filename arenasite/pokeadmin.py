"""관리자용 포켓몬 데이터 조작.

읽기 전용인 pokedex.py 와 달리 여기서는 실제로 값을 바꾼다.
봇(펫봇)과 같은 DB 를 쓰므로 스키마를 임의로 바꾸지 않고, 봇이 만들어 둔
테이블만 조작한다. 소환처럼 '봇이 해야 하는 일'은 admin_requests 큐에 넣는다.
"""
from __future__ import annotations

import os
import sqlite3
import time

SHARED_DIR = os.environ.get("ARENA_SHARED_DIR", "/home/hxxsx4/shared_data")
DB_PATH = os.environ.get("ARENA_POKEMON_DB_PATH", os.path.join(SHARED_DIR, "pokedex.db"))

# 상점에서 파는 볼 (펫봇 config.BALLS 와 이름을 맞춰야 한다)
BALL_NAMES = ["몬스터볼", "슈퍼볼", "하이퍼볼", "마스터볼"]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def available() -> bool:
    if not os.path.exists(DB_PATH):
        return False
    try:
        with _conn() as c:
            c.execute("SELECT 1 FROM species LIMIT 1")
        return True
    except sqlite3.Error:
        return False


# ───────── 트레이너 ─────────
def list_trainers() -> list[dict]:
    """포켓몬이나 아이템을 가진 유저 목록."""
    try:
        with _conn() as c:
            rows = c.execute("""
                SELECT u.owner_id AS uid,
                       (SELECT COUNT(*) FROM user_pokemon p WHERE p.owner_id = u.owner_id) AS mons,
                       (SELECT COUNT(*) FROM user_pokemon p
                         WHERE p.owner_id = u.owner_id AND p.in_party = 1) AS party
                FROM (SELECT DISTINCT owner_id FROM user_pokemon) u
                ORDER BY mons DESC""").fetchall()
            out = [dict(r) for r in rows]
            known = {r["uid"] for r in out}
            extra = c.execute(
                "SELECT DISTINCT user_id AS uid FROM user_items").fetchall()
            for r in extra:
                if r["uid"] not in known:
                    out.append({"uid": r["uid"], "mons": 0, "party": 0})
    except sqlite3.Error:
        return []
    for r in out:
        r["items"] = get_items(r["uid"])
    return out


def get_items(user_id: int) -> dict[str, int]:
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT item_name, amount FROM user_items WHERE user_id = ? AND amount > 0",
                (user_id,)).fetchall()
        return {r["item_name"]: r["amount"] for r in rows}
    except sqlite3.Error:
        return {}


def get_pokemon(user_id: int) -> list[dict]:
    try:
        with _conn() as c:
            rows = c.execute("""
                SELECT up.uid, up.nickname, up.level, up.exp, up.is_partner, up.in_party,
                       s.name_ko, s.rarity, s.sprite_url
                FROM user_pokemon up JOIN species s ON up.species_id = s.id
                WHERE up.owner_id = ?
                ORDER BY up.in_party DESC, up.is_partner DESC, up.level DESC""",
                (user_id,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


# ───────── 아이템 조작 ─────────
def set_item(user_id: int, item: str, amount: int) -> int:
    """아이템 개수를 지정한 값으로 맞춘다. 0 이면 삭제. 최종 개수를 반환."""
    amount = max(0, min(amount, 9999))
    with _conn() as c:
        if amount == 0:
            c.execute("DELETE FROM user_items WHERE user_id = ? AND item_name = ?",
                      (user_id, item))
        else:
            c.execute(
                """INSERT INTO user_items (user_id, item_name, amount) VALUES (?,?,?)
                   ON CONFLICT(user_id, item_name) DO UPDATE SET amount = excluded.amount""",
                (user_id, item, amount))
        c.commit()
    return amount


def add_item(user_id: int, item: str, delta: int) -> int:
    cur = get_items(user_id).get(item, 0)
    return set_item(user_id, item, cur + delta)


# ───────── 봇에게 시킬 일 (소환 등) ─────────
def request_summon(name: str, actor: str) -> bool:
    """전설/환상(또는 지정 종) 소환을 봇에게 요청한다.

    웹은 디스코드에 메시지를 보낼 수 없으므로 큐에 넣고 펫봇이 처리한다.
    """
    try:
        with _conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS admin_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT, payload TEXT, actor TEXT,
                    created_at REAL, done INTEGER DEFAULT 0, result TEXT)""")
            c.execute(
                "INSERT INTO admin_requests (kind, payload, actor, created_at, done) "
                "VALUES ('summon', ?, ?, ?, 0)", (name or "", actor, time.time()))
            c.commit()
        return True
    except sqlite3.Error:
        return False


def recent_requests(limit: int = 10) -> list[dict]:
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM admin_requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def search_species(q: str, only_legendary: bool = False, limit: int = 30) -> list[dict]:
    sql = "SELECT id, name_ko, rarity, sprite_url FROM species WHERE name_ko LIKE ?"
    params: list = [f"%{q}%"]
    if only_legendary:
        sql += " AND rarity IN ('전설','환상')"
    # 정확히 일치하는 이름을 앞에 (예: '뮤' 가 '뮤츠' 보다 먼저)
    sql += " ORDER BY CASE WHEN name_ko = ? THEN 0 ELSE 1 END, LENGTH(name_ko), id LIMIT ?"
    params += [q, limit]
    try:
        with _conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []
