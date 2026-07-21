"""종합게임 아레나 - 내전 커뮤니티 사이트 데이터 계층.

모든 데이터는 공유 볼륨(ARENA_SHARED_DIR)에 JSON 으로 저장되어
봇 재시작/컨테이너 교체에도 유지된다. filelock 으로 프로세스 간 안전하게 접근.

저장 파일 (기본 /home/hxxsx4/shared_data/arena_site/):
    matches.json     내전 경기 기록 (킬/데스/어시/챔피언/승패/MVP)
    summoners.json   등록된 소환사 명단 (라이엇 ID + 티어)
    schedules.json   다가오는 내전 일정
    bets.json        내기(베팅) 목록
"""
from __future__ import annotations

import os
import json
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from filelock import FileLock

SHARED_DIR = os.environ.get("ARENA_SHARED_DIR", "/home/hxxsx4/shared_data")
SITE_DIR = os.path.join(SHARED_DIR, "arena_site")
os.makedirs(SITE_DIR, exist_ok=True)

KST = timezone(timedelta(hours=9))

# 게임 모드: 기록실 탭과 매핑
MODES = {
    "normal": "일반 내전",
    "aram": "칼바람 내전",
    "tournament": "명예의 전당(대회)",
}

_LOCKS: dict[str, FileLock] = {}


def _path(name: str) -> str:
    return os.path.join(SITE_DIR, f"{name}.json")


def _lock(name: str) -> FileLock:
    if name not in _LOCKS:
        _LOCKS[name] = FileLock(_path(name) + ".lock", timeout=5)
    return _LOCKS[name]


def _read(name: str, default):
    p = _path(name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write(name: str, data):
    p = _path(name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _now() -> float:
    return time.time()


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


# ============================================================
# 소환사 명단
# ============================================================
def list_summoners() -> list[dict]:
    data = _read("summoners", [])
    return sorted(data, key=lambda s: s.get("added_at", 0), reverse=True)


def add_summoner(riot_id: str, tier: str = "", discord_id: str = "",
                 position: str = "", avatar: str = "") -> dict:
    riot_id = riot_id.strip()
    with _lock("summoners"):
        data = _read("summoners", [])
        for s in data:
            if s["riot_id"].lower() == riot_id.lower():
                # 이미 존재 → 새 정보만 갱신
                if tier:
                    s["tier"] = tier
                if position:
                    s["position"] = position
                if discord_id:
                    s["discord_id"] = discord_id
                if avatar:
                    s["avatar"] = avatar
                _write("summoners", data)
                return s
        rec = {
            "id": uuid.uuid4().hex[:8],
            "riot_id": riot_id,
            "tier": tier,
            "position": position,
            "discord_id": discord_id,
            "avatar": avatar,
            "added_at": _now(),
        }
        data.append(rec)
        _write("summoners", data)
        return rec


def find_summoner(riot_id: str) -> dict | None:
    """라이엇 ID 로 명단 레코드 조회 (대소문자 무시)."""
    target = (riot_id or "").strip().lower()
    for s in _read("summoners", []):
        if s.get("riot_id", "").lower() == target:
            return s
    return None


def remove_summoner(sid: str) -> bool:
    with _lock("summoners"):
        data = _read("summoners", [])
        new = [s for s in data if s.get("id") != sid]
        if len(new) == len(data):
            return False
        _write("summoners", new)
        return True


# ============================================================
# 경기 기록
# ============================================================
def list_matches(mode: str | None = None) -> list[dict]:
    data = _read("matches", [])
    if mode:
        data = [m for m in data if m.get("mode") == mode]
    return sorted(data, key=lambda m: (m.get("date", ""), m.get("created_at", 0)), reverse=True)


def get_match(mid: str) -> dict | None:
    for m in _read("matches", []):
        if m.get("id") == mid:
            return m
    return None


def add_match(payload: dict) -> dict:
    """payload = {date, mode, title, mvp, teams:[{name,win,players:[{summoner,champion,position,kills,deaths,assists}]}]}"""
    date = (payload.get("date") or _today()).strip()
    mode = payload.get("mode") if payload.get("mode") in MODES else "normal"
    title = (payload.get("title") or "단판 매치").strip()
    mvp = (payload.get("mvp") or "").strip()

    teams = []
    for t in payload.get("teams", []):
        players = []
        for p in t.get("players", []):
            summoner = (p.get("summoner") or "").strip()
            if not summoner:
                continue
            players.append({
                "summoner": summoner,
                "champion": (p.get("champion") or "").strip(),
                "position": (p.get("position") or "").strip(),
                "kills": _int(p.get("kills")),
                "deaths": _int(p.get("deaths")),
                "assists": _int(p.get("assists")),
            })
        if players:
            teams.append({
                "name": (t.get("name") or f"{len(teams)+1}팀").strip(),
                "win": bool(t.get("win")),
                "players": players,
            })
    if not teams:
        raise ValueError("최소 한 팀의 선수를 입력하세요.")

    rec = {
        "id": uuid.uuid4().hex[:8],
        "date": date,
        "mode": mode,
        "title": title,
        "mvp": mvp,
        "teams": teams,
        "created_at": _now(),
    }
    with _lock("matches"):
        data = _read("matches", [])
        data.append(rec)
        _write("matches", data)
    # 참가자 자동 소환사 등록
    for t in teams:
        for p in t["players"]:
            try:
                add_summoner(p["summoner"])
            except Exception:
                pass
    return rec


def remove_match(mid: str) -> bool:
    with _lock("matches"):
        data = _read("matches", [])
        new = [m for m in data if m.get("id") != mid]
        if len(new) == len(data):
            return False
        _write("matches", new)
        return True


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


# ============================================================
# 일정
# ============================================================
def list_schedules(upcoming_only: bool = True) -> list[dict]:
    data = _read("schedules", [])
    if upcoming_only:
        today = _today()
        data = [s for s in data if s.get("date", "") >= today]
    return sorted(data, key=lambda s: (s.get("date", ""), s.get("time", "")))


def add_schedule(date: str, time_: str, title: str, mode: str = "normal", note: str = "") -> dict:
    rec = {
        "id": uuid.uuid4().hex[:8],
        "date": date.strip(),
        "time": (time_ or "").strip(),
        "title": (title or "내전").strip(),
        "mode": mode if mode in MODES else "normal",
        "note": (note or "").strip(),
        "created_at": _now(),
    }
    with _lock("schedules"):
        data = _read("schedules", [])
        data.append(rec)
        _write("schedules", data)
    return rec


def remove_schedule(sid: str) -> bool:
    with _lock("schedules"):
        data = _read("schedules", [])
        new = [s for s in data if s.get("id") != sid]
        if len(new) == len(data):
            return False
        _write("schedules", new)
        return True


# ============================================================
# 내기(베팅)
# ============================================================
def list_bets(category: str | None = None) -> list[dict]:
    data = _read("bets", [])
    if category:
        data = [b for b in data if b.get("category") == category]
    return sorted(data, key=lambda b: b.get("created_at", 0), reverse=True)


def add_bet(payload: dict) -> dict:
    rec = {
        "id": uuid.uuid4().hex[:8],
        "category": payload.get("category", "lol"),  # lol(솔랭) / tft(롤체)
        "title": (payload.get("title") or "내기").strip(),
        "target_score": (payload.get("target_score") or "").strip(),
        "host": (payload.get("host") or "").strip(),
        "participants": [p.strip() for p in payload.get("participants", []) if p.strip()],
        "status": "open",
        "created_at": _now(),
    }
    if not rec["title"]:
        raise ValueError("내기 제목을 입력하세요.")
    with _lock("bets"):
        data = _read("bets", [])
        data.append(rec)
        _write("bets", data)
    return rec


def join_bet(bid: str, name: str) -> bool:
    name = name.strip()
    if not name:
        return False
    with _lock("bets"):
        data = _read("bets", [])
        for b in data:
            if b.get("id") == bid:
                if name not in b["participants"]:
                    b["participants"].append(name)
                _write("bets", data)
                return True
    return False


def remove_bet(bid: str) -> bool:
    with _lock("bets"):
        data = _read("bets", [])
        new = [b for b in data if b.get("id") != bid]
        if len(new) == len(data):
            return False
        _write("bets", new)
        return True


# ============================================================
# 통계 계산
# ============================================================
def _kda(k: int, d: int, a: int) -> float:
    return (k + a) / max(1, d)


def _iter_player_rows(matches: list[dict]):
    """(summoner, win, kills, deaths, assists, champion, is_mvp, date) 로우 생성."""
    for m in matches:
        mvp = (m.get("mvp") or "").strip().lower()
        for t in m.get("teams", []):
            win = bool(t.get("win"))
            for p in t.get("players", []):
                s = p["summoner"]
                yield (
                    s, win, p.get("kills", 0), p.get("deaths", 0), p.get("assists", 0),
                    p.get("champion", ""), s.strip().lower() == mvp and bool(mvp),
                    m.get("date", ""),
                )


def summoner_stats(riot_id: str) -> dict:
    """특정 소환사의 전체 내전 전적 요약 + 챔피언별 + 최근 경기."""
    target = riot_id.strip().lower()
    matches = list_matches()
    games = wins = k = d = a = mvp_cnt = 0
    champ = defaultdict(lambda: {"games": 0, "wins": 0, "k": 0, "d": 0, "a": 0})
    recent = []
    for m in matches:
        mvp = (m.get("mvp") or "").strip().lower()
        for t in m.get("teams", []):
            for p in t.get("players", []):
                if p["summoner"].strip().lower() != target:
                    continue
                games += 1
                win = bool(t.get("win"))
                wins += 1 if win else 0
                k += p.get("kills", 0); d += p.get("deaths", 0); a += p.get("assists", 0)
                is_mvp = p["summoner"].strip().lower() == mvp and bool(mvp)
                mvp_cnt += 1 if is_mvp else 0
                c = p.get("champion") or "미상"
                cs = champ[c]
                cs["games"] += 1; cs["wins"] += 1 if win else 0
                cs["k"] += p.get("kills", 0); cs["d"] += p.get("deaths", 0); cs["a"] += p.get("assists", 0)
                if len(recent) < 10:
                    recent.append({
                        "date": m.get("date", ""), "title": m.get("title", ""),
                        "mode": m.get("mode", "normal"), "win": win,
                        "champion": c, "kills": p.get("kills", 0),
                        "deaths": p.get("deaths", 0), "assists": p.get("assists", 0),
                        "mvp": is_mvp,
                    })
    champ_list = []
    for name, cs in champ.items():
        champ_list.append({
            "champion": name, "games": cs["games"], "wins": cs["wins"],
            "winrate": round(cs["wins"] / cs["games"] * 100) if cs["games"] else 0,
            "kda": round(_kda(cs["k"], cs["d"], cs["a"]), 2),
        })
    champ_list.sort(key=lambda x: x["games"], reverse=True)
    return {
        "riot_id": riot_id, "games": games, "wins": wins, "losses": games - wins,
        "winrate": round(wins / games * 100) if games else 0,
        "kda": round(_kda(k, d, a), 2), "mvp": mvp_cnt,
        "avg_k": round(k / games, 1) if games else 0,
        "avg_d": round(d / games, 1) if games else 0,
        "avg_a": round(a / games, 1) if games else 0,
        "champions": champ_list[:6], "recent": recent,
    }


# ============================================================
# 라이엇 계정 본인 인증 (아이콘 인증 완료된 계정)
# ============================================================
def get_verified(uid) -> dict | None:
    """디스코드 uid 의 인증된 라이엇 계정 {riot_id, puuid, at} 또는 None."""
    return _read("verified", {}).get(str(uid))


def set_verified(uid, riot_id: str, puuid: str):
    with _lock("verified"):
        data = _read("verified", {})
        data[str(uid)] = {"riot_id": riot_id.strip(), "puuid": puuid, "at": _now()}
        _write("verified", data)


def clear_verified(uid) -> bool:
    with _lock("verified"):
        data = _read("verified", {})
        if str(uid) not in data:
            return False
        del data[str(uid)]
        _write("verified", data)
        return True


def verified_riot_ids() -> set:
    """인증된 라이엇 ID 집합(소문자) — 명단에 ✔ 표시용."""
    return {v["riot_id"].strip().lower() for v in _read("verified", {}).values()
            if v.get("riot_id")}


# ============================================================
# 유저 프로필 (봇이 쓰는 공유 stats.json 연동)
# ============================================================
def read_stats() -> dict:
    """모든 봇이 공유하는 stats.json 을 읽는다(읽기 전용)."""
    p = os.path.join(SHARED_DIR, "stats.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _xp_for_level(level: int) -> float:
    # 레벨 곡선: 기본 600, 레벨당 ×1.05 (봇과 동일)
    return 600 * (1.05 ** level - 1) / 0.05


def level_from_xp(xp) -> int:
    try:
        xp = max(0, int(xp or 0))
    except (TypeError, ValueError):
        return 0
    level = 0
    while level < 1000 and _xp_for_level(level + 1) <= xp:
        level += 1
    return level


def user_profile(uid) -> dict:
    """디스코드 유저 ID 로 봇 데이터(포인트/레벨/닉네임/라인)를 조회."""
    rec = read_stats().get(str(uid), {}) or {}
    return {
        "points": int(rec.get("포인트", 0) or 0),
        "warns": int(rec.get("경고", 0) or 0),
        "chat_level": level_from_xp(rec.get("경험치", 0)),
        "voice_level": level_from_xp(rec.get("음성경험치", 0)),
        "lol_nick": rec.get("롤닉") or "",
        "val_nick": rec.get("발닉") or "",
        "main_lane": rec.get("주라인") or "",
        "sub_lane": rec.get("부라인") or "",
        "registered": bool(rec),
    }


def _week_start() -> str:
    now = datetime.now(KST)
    monday = now - timedelta(days=now.weekday())
    return monday.strftime("%Y-%m-%d")


def weekly_dashboard() -> dict:
    """이번 주(월요일~) 통계 대시보드."""
    start = _week_start()
    all_matches = list_matches()
    week_matches = [m for m in all_matches if m.get("date", "") >= start]

    agg = defaultdict(lambda: {"games": 0, "wins": 0, "k": 0, "d": 0, "a": 0, "mvp": 0})
    for (s, win, k, d, a, _c, is_mvp, _date) in _iter_player_rows(week_matches):
        r = agg[s]
        r["games"] += 1; r["wins"] += 1 if win else 0
        r["k"] += k; r["d"] += d; r["a"] += a; r["mvp"] += 1 if is_mvp else 0

    def _rows():
        return [{"summoner": s, **v,
                 "winrate": round(v["wins"] / v["games"] * 100) if v["games"] else 0,
                 "kda": round(_kda(v["k"], v["d"], v["a"]), 2)}
                for s, v in agg.items()]

    rows = _rows()
    top_kills = sorted(rows, key=lambda r: r["k"], reverse=True)[:3]
    qualified = [r for r in rows if r["games"] >= 2]
    top_winrate = sorted(qualified, key=lambda r: (r["winrate"], r["games"]), reverse=True)[:3]
    top_kda = sorted(qualified, key=lambda r: r["kda"], reverse=True)[:3]
    top_players = sorted(rows, key=lambda r: r["games"], reverse=True)[:5]
    mvp_rank = sorted([r for r in qualified if r["mvp"] > 0],
                      key=lambda r: (r["mvp"], r["winrate"]), reverse=True)
    week_mvp = mvp_rank[0] if mvp_rank else (top_kda[0] if top_kda else None)

    return {
        "week_start": start,
        "week_games": len(week_matches),
        "top_kills": top_kills,
        "top_winrate": top_winrate,
        "top_kda": top_kda,
        "top_players": top_players,
        "week_mvp": week_mvp,
        "schedules": list_schedules()[:5],
        "recent_matches": all_matches[:5],
        "total_matches": len(all_matches),
        "total_summoners": len(list_summoners()),
    }
