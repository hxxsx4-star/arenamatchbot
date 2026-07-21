"""챔피언 초상화 매핑 (DataDragon).

ko_KR 챔피언 데이터를 하루 1회 캐시해 '아리'/'Ahri'/'MonkeyKing' 같은
어떤 표기든 → DataDragon 아이콘 URL 로 변환한다. 실패 시 None (초상화 생략).
"""
from __future__ import annotations

import os
import json
import time

import httpx

from arenasite.store import SITE_DIR

_CACHE_PATH = os.path.join(SITE_DIR, "champions.json")
_TTL = 86400  # 24시간

_state: dict = {"ver": "", "by_name": {}, "loaded_at": 0.0}


def _load_from_file() -> bool:
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("at", 0) < _TTL and data.get("by_name"):
            _state.update(ver=data["ver"], by_name=data["by_name"], loaded_at=data["at"])
            return True
    except Exception:
        pass
    return False


async def ensure():
    """챔피언 매핑을 메모리에 준비(캐시 유효하면 재사용). 실패해도 조용히 통과."""
    if _state["by_name"] and time.time() - _state["loaded_at"] < _TTL:
        return
    if _load_from_file():
        return
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            vr = await cli.get("https://ddragon.leagueoflegends.com/api/versions.json")
            if vr.status_code != 200:
                return
            ver = vr.json()[0]
            cr = await cli.get(
                f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/ko_KR/champion.json")
            if cr.status_code != 200:
                return
            by_name = {}
            for cid, c in cr.json().get("data", {}).items():
                by_name[cid.lower()] = cid                      # 영문 ID (Ahri, MonkeyKing)
                by_name[c.get("name", "").strip().lower()] = cid  # 한글 이름 (아리, 오공)
        _state.update(ver=ver, by_name=by_name, loaded_at=time.time())
        os.makedirs(SITE_DIR, exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ver": ver, "by_name": by_name, "at": time.time()}, f,
                      ensure_ascii=False)
        os.replace(tmp, _CACHE_PATH)
    except Exception as e:
        print(f"[챔피언 매핑] 로드 실패: {e}")


def icon_url(name) -> str | None:
    """챔피언 표기(한글/영문) → 초상화 URL. 모르면 None."""
    if not name or not _state["by_name"]:
        return None
    cid = _state["by_name"].get(str(name).strip().lower())
    if not cid:
        return None
    return (f"https://ddragon.leagueoflegends.com/cdn/{_state['ver']}"
            f"/img/champion/{cid}.png")
