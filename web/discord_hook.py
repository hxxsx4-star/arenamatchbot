"""경매 종료 시 결과를 내전 로그 채널로 보냅니다.

봇들과 공유하는 로그 큐(utils.logs.enqueue_embed)에 적재하면,
로그봇이 실제 채널(MATCH_LOG_CH)에 최종 기록합니다.
discord.py 를 직접 쓰지 않고 임베드 dict 를 만들어 넣습니다.
"""
from datetime import datetime, timezone

try:
    from utils.logs import enqueue_embed, MATCH_LOG_CH
    _AVAILABLE = True
except Exception:
    _AVAILABLE = False


async def post_auction_result(room) -> None:
    if not _AVAILABLE or not MATCH_LOG_CH:
        return

    fields = []
    for cap in room.captains:
        roster_lines = []
        for mid in cap.roster:
            m = room.get_member(mid)
            if m:
                pos = f" ({m.position})" if m.position else ""
                roster_lines.append(f"• {m.name}{pos} — {m.won_price:,}P")
        value = "\n".join(roster_lines) if roster_lines else "_(영입 없음)_"
        value += f"\n남은 포인트: **{cap.points:,}P**"
        fields.append({"name": f"🧑‍✈️ {cap.name} 팀", "value": value[:1024], "inline": True})

    unsold = [m.name for m in room.members if m.won_by is None]
    if unsold:
        fields.append({"name": "미배정(유찰)", "value": ", ".join(unsold)[:1024], "inline": False})

    embed = {
        "title": f"🏆 경매 결과 - {room.title}",
        "description": f"팀 {room.team_count} · 팀당 {room.team_size}명 · 총 포인트 {room.total_points:,}P",
        "color": 0xF1C40F,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": fields,
        "footer": {"text": f"경매 ID: {room.id}"},
    }
    try:
        enqueue_embed(MATCH_LOG_CH, embed)
    except Exception as e:
        print(f"[auction] 결과 로그 적재 실패: {e}")
