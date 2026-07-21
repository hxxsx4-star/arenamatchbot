"""사이트용 실제 티어 엠블럼 이미지 다운로드/캐시/서빙.

VM(사이트 서버)에서 최초 1회 다운로드해 공유 볼륨에 캐시하고,
/tiericon/{game}/{key}.png 라우트로 서빙한다. 실패 시 404 → 템플릿의
onerror 로 이미지가 조용히 숨겨져 텍스트 뱃지만 남는다.
"""
from __future__ import annotations

import os

import httpx

from arenasite.store import SITE_DIR

ICON_DIR = os.path.join(SITE_DIR, "tier_icons")

_LOL_BASE = ("https://raw.communitydragon.org/latest/plugins/"
             "rcp-fe-lol-static-assets/global/default/images/ranked-emblem")
LOL_KEYS = {
    "challenger", "grandmaster", "master", "diamond", "emerald",
    "platinum", "gold", "silver", "bronze", "iron",
}

_VAL_UUID = "03621f52-342b-cf4e-4f86-9350a49c6d04"
_VAL_BASE = f"https://media.valorant-api.com/competitivetiers/{_VAL_UUID}"
VAL_TIER_NO = {
    "radiant": 27, "immortal": 25, "ascendant": 22, "diamond": 19,
    "platinum": 16, "gold": 13, "silver": 10, "bronze": 7, "iron": 4,
    "unranked": 0,
}

# 한글/영문 티어 텍스트 → 표준 키 (자유 입력 파싱)
_ALIASES = {
    "챌린저": "challenger", "challenger": "challenger",
    "그랜드마스터": "grandmaster", "그마": "grandmaster", "grandmaster": "grandmaster",
    "마스터": "master", "master": "master",
    "다이아몬드": "diamond", "다이아": "diamond", "diamond": "diamond",
    "에메랄드": "emerald", "emerald": "emerald",
    "플래티넘": "platinum", "플래": "platinum", "platinum": "platinum",
    "골드": "gold", "gold": "gold",
    "실버": "silver", "silver": "silver",
    "브론즈": "bronze", "bronze": "bronze",
    "아이언": "iron", "iron": "iron",
    "레디언트": "radiant", "radiant": "radiant",
    "이모탈": "immortal", "불멸": "immortal", "immortal": "immortal",
    "어센던트": "ascendant", "초월자": "ascendant", "ascendant": "ascendant",
}
# 긴 이름 먼저 매칭 (예: '그랜드마스터'가 '마스터'보다 우선)
_ALIAS_ORDER = sorted(_ALIASES, key=len, reverse=True)


def tier_key(text) -> str | None:
    """'다이아몬드 IV', 'DIAMOND', '플래 2' 같은 자유 텍스트에서 티어 키 추출."""
    if not text:
        return None
    t = str(text).strip().lower()
    for alias in _ALIAS_ORDER:
        if alias.lower() in t:
            return _ALIASES[alias]
    return None


def _url(game: str, key: str) -> str | None:
    if game == "lol" and key in LOL_KEYS:
        return f"{_LOL_BASE}/emblem-{key}.png"
    if game == "val" and key in VAL_TIER_NO:
        return f"{_VAL_BASE}/{VAL_TIER_NO[key]}/largeicon.png"
    return None


async def get_icon_path(game: str, key: str) -> str | None:
    """캐시 경로 반환. 없으면 다운로드, 실패/미지원 키면 None."""
    key = (key or "").lower()
    url = _url(game, key)
    if not url:
        return None
    os.makedirs(ICON_DIR, exist_ok=True)
    # v2: 투명 여백 크롭 적용 (기존 캐시와 파일명 분리해 자동 재다운로드)
    path = os.path.join(ICON_DIR, f"{game}_{key}_v2.png")
    if os.path.exists(path) and os.path.getsize(path) > 500:
        return path
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(url)
            if r.status_code != 200 or len(r.content) < 1000:
                return None
        data = _crop_transparent(r.content)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path
    except Exception as e:
        print(f"[티어 아이콘] {game}/{key} 다운로드 실패: {e}")
        return None


def _crop_transparent(data: bytes) -> bytes:
    """라이엇 원본의 큰 투명 여백을 잘라내 문양이 크게 보이게 한다."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        bbox = im.getchannel("A").getbbox()
        if bbox:
            im = im.crop(bbox)
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()
    except Exception:
        return data  # Pillow 미설치 등 — 원본 그대로
