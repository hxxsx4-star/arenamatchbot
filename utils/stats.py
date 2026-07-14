from typing import Optional
import json
import os
import asyncio
from filelock import FileLock
from datetime import datetime, timedelta, timezone

# 공유 데이터 디렉터리 (모든 봇이 동일하게 사용). ARENA_SHARED_DIR 환경변수로 재정의 가능.
SHARED_DIR = os.environ.get("ARENA_SHARED_DIR", "/home/hxxsx4/shared_data")
SHARED_FILE_PATH = os.path.join(SHARED_DIR, "stats.json")
LOCK_FILE_PATH = SHARED_FILE_PATH + ".lock"

os.makedirs(os.path.dirname(SHARED_FILE_PATH), exist_ok=True)
lock = FileLock(LOCK_FILE_PATH, timeout=5)

def _load_stats_nolock() -> dict:
    if not os.path.exists(SHARED_FILE_PATH): return {}
    try:
        with open(SHARED_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def _save_stats_nolock(stats: dict):
    with open(SHARED_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=4, ensure_ascii=False)

def ensure_user(stats: dict, user_id: str) -> dict:
    if user_id not in stats:
        stats[user_id] = {"포인트": 0, "경고": 0}
    return stats[user_id]

async def load_stats() -> dict:
    def _task():
        with lock:
            return _load_stats_nolock()
    return await asyncio.to_thread(_task)

async def save_stats(stats: dict):
    def _task():
        with lock:
            _save_stats_nolock(stats)
    await asyncio.to_thread(_task)

async def get_points(user_id: int) -> int:
    def _task():
        with lock:
            stats = _load_stats_nolock()
            return int(ensure_user(stats, str(user_id)).get("포인트", 0))
    return await asyncio.to_thread(_task)

async def add_points(user_id: int, amount: int):
    def _task():
        with lock:
            stats = _load_stats_nolock()
            rec = ensure_user(stats, str(user_id))
            rec["포인트"] = int(rec.get("포인트", 0)) + amount
            _save_stats_nolock(stats)
    await asyncio.to_thread(_task)

async def spend_points(user_id: int, amount: int) -> bool:
    def _task():
        with lock:
            stats = _load_stats_nolock()
            rec = ensure_user(stats, str(user_id))
            current_points = int(rec.get("포인트", 0))
            if current_points < amount:
                return False
            rec["포인트"] = current_points - amount
            _save_stats_nolock(stats)
            return True
    return await asyncio.to_thread(_task)

# ==========================================
# ✨ 아래부터 누락되어 에러를 발생시키던 추가 함수들입니다.
# (기존 FileLock 동기화 방식과 동일하게 작성되었습니다)
# ==========================================

def format_num(num: int) -> str:
    """숫자에 3자리마다 콤마를 찍어주는 유틸 함수입니다."""
    return f"{num:,}"

async def process_attendance(user_id: int, reward: int, attend_key: str, today_str: str) -> bool:
    """출석 체크를 진행하고 포인트를 지급하는 함수입니다."""
    def _task():
        with lock:
            stats = _load_stats_nolock()
            rec = ensure_user(stats, str(user_id))

            # 이미 오늘 출석을 한 경우
            if rec.get(attend_key) == today_str:
                return False

            # 출석 처리 및 포인트 지급
            rec[attend_key] = today_str
            rec["포인트"] = int(rec.get("포인트", 0)) + reward
            _save_stats_nolock(stats)
            return True

    return await asyncio.to_thread(_task)

async def add_warning(user_id: int, count: int) -> tuple[int, int]:
    """유저에게 경고를 부여하는 함수입니다. (기존경고, 바뀐경고) 튜플을 반환합니다."""
    def _task():
        with lock:
            stats = _load_stats_nolock()
            rec = ensure_user(stats, str(user_id))

            old_warn = int(rec.get("경고", 0))
            new_warn = old_warn + count
            rec["경고"] = new_warn

            _save_stats_nolock(stats)
            return old_warn, new_warn

    return await asyncio.to_thread(_task)

async def reduce_warning(user_id: int, count: int) -> tuple[int, int]:
    """유저의 경고를 차감하는 함수입니다. (기존경고, 바뀐경고) 튜플을 반환합니다."""
    def _task():
        with lock:
            stats = _load_stats_nolock()
            rec = ensure_user(stats, str(user_id))

            old_warn = int(rec.get("경고", 0))
            new_warn = max(0, old_warn - count) # 경고가 마이너스가 되지 않도록 0 밑으로는 내리지 않음
            rec["경고"] = new_warn

            _save_stats_nolock(stats)
            return old_warn, new_warn

    return await asyncio.to_thread(_task)