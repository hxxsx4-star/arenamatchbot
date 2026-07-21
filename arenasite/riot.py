"""라이엇 API 연동 (선택). RIOT_API_KEY 환경변수가 있으면 실 티어를 조회하고,
없으면 조용히 None 을 반환해 사이트는 내부 기록만으로 동작한다.

키 발급: https://developer.riotgames.com/  (RIOT_API_KEY 로 지정)
지역 라우팅은 asia(계정) + kr(리그) 고정. (필요 시 확장)
"""
from __future__ import annotations

import os
import httpx

RIOT_API_KEY = os.environ.get("RIOT_API_KEY", "").strip()
ACCOUNT_HOST = "https://asia.api.riotgames.com"
PLATFORM_HOST = "https://kr.api.riotgames.com"

TIER_KO = {
    "IRON": "아이언", "BRONZE": "브론즈", "SILVER": "실버", "GOLD": "골드",
    "PLATINUM": "플래티넘", "EMERALD": "에메랄드", "DIAMOND": "다이아몬드",
    "MASTER": "마스터", "GRANDMASTER": "그랜드마스터", "CHALLENGER": "챌린저",
}


def enabled() -> bool:
    return bool(RIOT_API_KEY)


async def get_puuid(riot_id: str) -> str | None:
    """'이름#태그' → puuid. 실패 시 None."""
    if not RIOT_API_KEY or "#" not in riot_id:
        return None
    name, tag = riot_id.split("#", 1)
    try:
        async with httpx.AsyncClient(timeout=8, headers={"X-Riot-Token": RIOT_API_KEY}) as cli:
            r = await cli.get(
                f"{ACCOUNT_HOST}/riot/account/v1/accounts/by-riot-id/"
                f"{name.strip()}/{tag.strip()}")
            if r.status_code != 200:
                return None
            return r.json().get("puuid")
    except Exception:
        return None


async def get_match_ids(puuid: str, count: int = 5) -> list:
    """puuid 의 최근 매치 ID 목록 (커스텀 게임 포함)."""
    if not RIOT_API_KEY or not puuid:
        return []
    try:
        async with httpx.AsyncClient(timeout=8, headers={"X-Riot-Token": RIOT_API_KEY}) as cli:
            r = await cli.get(
                f"{ACCOUNT_HOST}/lol/match/v5/matches/by-puuid/{puuid}/ids",
                params={"count": count})
            if r.status_code != 200:
                return []
            return r.json()
    except Exception:
        return []


async def get_match(match_id: str) -> dict | None:
    """매치 상세 (participants 에 챔피언/KDA 포함)."""
    if not RIOT_API_KEY or not match_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10, headers={"X-Riot-Token": RIOT_API_KEY}) as cli:
            r = await cli.get(f"{ACCOUNT_HOST}/lol/match/v5/matches/{match_id}")
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


async def get_summoner(puuid: str) -> dict | None:
    """puuid → {profileIconId, summonerLevel}. 실패 시 None."""
    if not RIOT_API_KEY or not puuid:
        return None
    try:
        async with httpx.AsyncClient(timeout=8, headers={"X-Riot-Token": RIOT_API_KEY}) as cli:
            r = await cli.get(f"{PLATFORM_HOST}/lol/summoner/v4/summoners/by-puuid/{puuid}")
            if r.status_code != 200:
                return None
            d = r.json()
            return {"profileIconId": d.get("profileIconId"),
                    "summonerLevel": d.get("summonerLevel")}
    except Exception:
        return None


async def lookup(riot_id: str) -> dict | None:
    """riot_id = '이름#태그' → {tier, rank, lp, wins, losses} 또는 None."""
    if not RIOT_API_KEY or "#" not in riot_id:
        return None
    name, tag = riot_id.split("#", 1)
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=8, headers=headers) as cli:
            r = await cli.get(
                f"{ACCOUNT_HOST}/riot/account/v1/accounts/by-riot-id/"
                f"{name.strip()}/{tag.strip()}")
            if r.status_code != 200:
                return None
            puuid = r.json().get("puuid")
            if not puuid:
                return None
            # 소환사 프로필 아이콘 (전적 카드에 표시)
            icon_id = None
            sr = await cli.get(f"{PLATFORM_HOST}/lol/summoner/v4/summoners/by-puuid/{puuid}")
            if sr.status_code == 200:
                icon_id = sr.json().get("profileIconId")
            lr = await cli.get(
                f"{PLATFORM_HOST}/lol/league/v4/entries/by-puuid/{puuid}")
            if lr.status_code != 200:
                return {"tier": "", "rank": "", "puuid": puuid, "icon_id": icon_id}
            for entry in lr.json():
                if entry.get("queueType") == "RANKED_SOLO_5x5":
                    tier = entry.get("tier", "")
                    return {
                        "puuid": puuid,
                        "icon_id": icon_id,
                        "tier": tier,
                        "tier_ko": TIER_KO.get(tier, tier),
                        "rank": entry.get("rank", ""),
                        "lp": entry.get("leaguePoints", 0),
                        "wins": entry.get("wins", 0),
                        "losses": entry.get("losses", 0),
                    }
            return {"tier": "UNRANKED", "tier_ko": "언랭크", "rank": "",
                    "puuid": puuid, "icon_id": icon_id}
    except Exception:
        return None
