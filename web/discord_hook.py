"""경매 종료 시 결과 + 입찰 로그를 '경매 로그' 채널(관리자 전용)로 보냅니다.

봇들과 공유하는 로그 큐(utils.logs.enqueue_embed)에 적재하면,
로그봇이 실제 채널(AUCTION_LOG_CH)에 최종 기록합니다.
※ 이 채널은 디스코드에서 관리자만 볼 수 있도록 채널 권한을 설정하세요.
"""
from datetime import datetime, timezone

try:
    from utils.logs import enqueue_embed, AUCTION_LOG_CH
    _AVAILABLE = True
except Exception:
    _AVAILABLE = False


def _bidlog_text(room, limit: int = 25) -> str:
    lines = []
    for e in room.events:
        if e["type"] == "sold":
            lines.append(f"🔨 {e.get('member')} → **{e.get('captain')}** ({e.get('amount', 0):,}P)")
        elif e["type"] == "unsold":
            lines.append(f"🚫 {e.get('member')} 유찰")
    if not lines:
        return "_(기록 없음)_"
    if len(lines) > limit:
        lines = lines[-limit:]
        lines.insert(0, "…(생략)…")
    return "\n".join(lines)[:1024]


async def post_auction_result(room) -> None:
    if not _AVAILABLE or not AUCTION_LOG_CH:
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

    fields.append({"name": "📜 입찰 로그", "value": _bidlog_text(room), "inline": False})

    embed = {
        "title": f"🏆 경매 결과 - {room.title}",
        "description": f"팀 {room.team_count} · 팀당 {room.team_size}명 · 총 포인트 {room.total_points:,}P",
        "color": 0xF1C40F,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": fields,
        "footer": {"text": f"경매 ID: {room.id} · 관리자 전용"},
    }
    try:
        enqueue_embed(AUCTION_LOG_CH, embed)
    except Exception as e:
        print(f"[auction] 결과 로그 적재 실패: {e}")
