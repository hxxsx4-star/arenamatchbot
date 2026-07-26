"""포켓몬 센터 데이터 계층 (읽기 전용).

펫봇이 관리하는 pokedex.db 를 그대로 읽어 웹에 보여준다.
쓰기는 전부 봇 쪽에서만 일어나므로 여기서는 조회만 한다.

DB 위치: ARENA_POKEMON_DB_PATH (기본 ${ARENA_SHARED_DIR}/pokedex.db)
"""
from __future__ import annotations

import os
import sqlite3

SHARED_DIR = os.environ.get("ARENA_SHARED_DIR", "/home/hxxsx4/shared_data")
DB_PATH = os.environ.get("ARENA_POKEMON_DB_PATH", os.path.join(SHARED_DIR, "pokedex.db"))

# PokeAPI 타입 슬러그 → 한글 (펫봇 species.py 와 동일 표기)
TYPE_KO = {
    "normal": "노말", "fire": "불꽃", "water": "물", "electric": "전기",
    "grass": "풀", "ice": "얼음", "fighting": "격투", "poison": "독",
    "ground": "땅", "flying": "비행", "psychic": "에스퍼", "bug": "벌레",
    "rock": "바위", "ghost": "고스트", "dragon": "드래곤", "dark": "악",
    "steel": "강철", "fairy": "페어리",
}

RARITY_ORDER = ["환상", "전설", "에픽", "희귀", "일반"]

# 볼 안내 (펫봇 cogs/pokemon/config.py 의 BALLS 와 값을 맞춰 둘 것)
_ITEM_SPRITE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items/{}.png"
BALLS = [
    {"name": "몬스터볼", "price": 50, "desc": "기본 볼", "rate": "×1.0",
     "sprite": _ITEM_SPRITE.format("poke-ball")},
    {"name": "슈퍼볼", "price": 150, "desc": "포획률 1.5배", "rate": "×1.5",
     "sprite": _ITEM_SPRITE.format("great-ball")},
    {"name": "하이퍼볼", "price": 400, "desc": "포획률 2배", "rate": "×2.0",
     "sprite": _ITEM_SPRITE.format("ultra-ball")},
    {"name": "마스터볼", "price": 5000, "desc": "반드시 포획", "rate": "확정",
     "sprite": _ITEM_SPRITE.format("master-ball")},
]

PAGE_SIZE = 60


def available() -> bool:
    """도감 DB 가 준비돼 있는지. (시딩 전이면 False)"""
    if not os.path.exists(DB_PATH):
        return False
    try:
        return _count("SELECT COUNT(*) FROM species") > 0
    except sqlite3.Error:
        return False


def _connect() -> sqlite3.Connection:
    # 봇이 쓰는 DB 라 읽기 전용으로 연다(실수로도 웹이 건드리지 않도록).
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _count(sql: str, params: tuple = ()) -> int:
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def type_ko(slug: str | None) -> str:
    return TYPE_KO.get(slug or "", slug or "")


def summary() -> dict:
    """상단 요약 타일용 통계."""
    try:
        return {
            "species": _count("SELECT COUNT(*) FROM species"),
            "caught": _count("SELECT COUNT(*) FROM user_pokemon"),
            "trainers": _count("SELECT COUNT(DISTINCT owner_id) FROM user_pokemon"),
            "discovered": _count("SELECT COUNT(DISTINCT species_id) FROM user_pokemon"),
        }
    except sqlite3.Error:
        return {"species": 0, "caught": 0, "trainers": 0, "discovered": 0}


def list_species(q: str = "", rarity: str = "", page: int = 0) -> tuple[list[dict], int]:
    """도감 목록. (행 목록, 전체 개수) 를 반환한다."""
    where, params = [], []
    if q:
        where.append("(name_ko LIKE ? OR name_en LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if rarity in RARITY_ORDER:
        where.append("rarity = ?")
        params.append(rarity)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    try:
        total = _count(f"SELECT COUNT(*) FROM species {clause}", tuple(params))
        rows = _rows(
            f"""SELECT id, name_ko, name_en, type1, type2, rarity, sprite_url,
                       hp, atk, def_, spatk, spdef, speed
                FROM species {clause} ORDER BY id LIMIT ? OFFSET ?""",
            tuple(params) + (PAGE_SIZE, page * PAGE_SIZE),
        )
    except sqlite3.Error:
        return [], 0

    caught = _caught_species_ids()
    for r in rows:
        r["types"] = [type_ko(r["type1"])] + ([type_ko(r["type2"])] if r["type2"] else [])
        r["bst"] = sum(r[k] or 0 for k in ("hp", "atk", "def_", "spatk", "spdef", "speed"))
        r["caught"] = r["id"] in caught
    return rows, total


def _caught_species_ids() -> set[int]:
    try:
        return {r["species_id"] for r in _rows("SELECT DISTINCT species_id FROM user_pokemon")}
    except sqlite3.Error:
        return set()


def trainer_ranking(limit: int = 10) -> list[dict]:
    """보유 마릿수 기준 트레이너 랭킹."""
    try:
        return _rows(
            """SELECT up.owner_id,
                      COUNT(*) AS total,
                      COUNT(DISTINCT up.species_id) AS unique_count,
                      SUM(CASE WHEN s.rarity IN ('전설','환상') THEN 1 ELSE 0 END) AS rare_count
               FROM user_pokemon up JOIN species s ON up.species_id = s.id
               GROUP BY up.owner_id ORDER BY total DESC, unique_count DESC LIMIT ?""",
            (limit,),
        )
    except sqlite3.Error:
        return []


def recent_catches(limit: int = 12) -> list[dict]:
    """최근 포획 기록."""
    try:
        rows = _rows(
            """SELECT up.owner_id, up.caught_at, s.id, s.name_ko, s.rarity, s.sprite_url
               FROM user_pokemon up JOIN species s ON up.species_id = s.id
               ORDER BY up.caught_at DESC LIMIT ?""",
            (limit,),
        )
    except sqlite3.Error:
        return []
    return rows


def user_box(owner_id: int) -> list[dict]:
    """특정 트레이너의 보유 목록 (내 프로필용)."""
    try:
        return _rows(
            """SELECT up.uid, up.nickname, up.level, up.is_partner, up.caught_at,
                      s.id, s.name_ko, s.rarity, s.sprite_url
               FROM user_pokemon up JOIN species s ON up.species_id = s.id
               WHERE up.owner_id = ?
               ORDER BY up.is_partner DESC, up.caught_at DESC""",
            (owner_id,),
        )
    except sqlite3.Error:
        return []
