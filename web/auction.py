"""종합게임 아레나 - 경매(팀 드래프트) 도메인 로직.

teambid 스타일 실시간 경매:
  방 생성 → 팀장/팀원 등록 → 팀원 셔플 → 한 명씩 경매(타이머/연장/입찰) → 낙찰 → 반복.

방 상태는 메모리에 보관(라이브 이벤트). 서버 권위 타이머(asyncio)로 카운트다운을 관리하고,
상태 변경 시 broadcast 콜백으로 접속자 전원(주최자/팀장/옵저버)에게 알린다.
"""
from __future__ import annotations

import asyncio
import random
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Awaitable


def _token(n: int = 8) -> str:
    return secrets.token_urlsafe(n)[:n]


class Phase(str, Enum):
    LOBBY = "lobby"        # 생성됨, 시작 대기
    BIDDING = "bidding"    # 특정 팀원 경매 진행 중
    SOLD = "sold"          # 방금 낙찰/유찰 결과 표시 중(짧은 텀)
    FINISHED = "finished"  # 모든 팀원 처리 완료


class BidUnitMode(str, Enum):
    FIXED = "fixed"   # 고정 입찰: 매 입찰 시 fixed_unit 이상 상승
    RATIO = "ratio"   # 비율 입찰: 현재가의 ratio_percent% 이상 상승


@dataclass
class Captain:
    cid: str
    name: str
    position: str = ""
    intro: str = ""
    points: int = 0            # 남은 포인트
    token: str = field(default_factory=lambda: _token(10))  # 팀장 개별 링크 토큰
    roster: list = field(default_factory=list)  # 낙찰받은 팀원 mid 목록

    def to_public(self, team_size: int) -> dict:
        return {
            "cid": self.cid, "name": self.name, "position": self.position,
            "intro": self.intro, "points": self.points,
            "roster": list(self.roster),
            "slots_left": max(0, (team_size - 1) - len(self.roster)),
        }


@dataclass
class Member:
    mid: str
    name: str
    position: str = ""
    intro: str = ""
    won_by: Optional[str] = None   # 낙찰 팀장 cid (없으면 유찰/미경매)
    won_price: int = 0

    def to_public(self) -> dict:
        return {
            "mid": self.mid, "name": self.name, "position": self.position,
            "intro": self.intro, "won_by": self.won_by, "won_price": self.won_price,
        }


class AuctionRoom:
    def __init__(self, config: dict):
        self.id: str = _token(6)
        self.host_token: str = _token(12)
        self.observer_token: str = _token(10)

        self.title: str = config["title"]
        self.team_count: int = int(config["team_count"])
        self.team_size: int = int(config["team_size"])          # 팀당 인원(팀장 포함)
        self.total_points: int = int(config["total_points"])
        self.show_order: bool = bool(config.get("show_order", True))
        self.bid_mode: BidUnitMode = BidUnitMode(config.get("bid_mode", "fixed"))
        self.fixed_unit: int = int(config.get("fixed_unit", 5))
        self.ratio_percent: float = float(config.get("ratio_percent", 10))
        self.bid_time: int = int(config.get("bid_time", 15))
        self.extend_time: int = int(config.get("extend_time", 5))

        self.captains: list[Captain] = []
        for c in config["captains"]:
            self.captains.append(Captain(
                cid=_token(6), name=c["name"], position=c.get("position", ""),
                intro=c.get("intro", ""),
                points=int(c.get("points") or self.total_points),
            ))

        self.members: list[Member] = []
        for m in config["members"]:
            self.members.append(Member(
                mid=_token(6), name=m["name"], position=m.get("position", ""),
                intro=m.get("intro", ""),
            ))

        # 진행 상태
        self.phase: Phase = Phase.LOBBY
        self.order: list[str] = []           # 셔플된 member mid 순서
        self.current_pos: int = -1           # order 인덱스
        self.current_mid: Optional[str] = None
        self.high_bid: int = 0
        self.high_cid: Optional[str] = None
        self.timer_remaining: int = 0
        self.created_at: float = time.time()
        self.events: list[dict] = []   # 경매 이벤트 로그(입찰/낙찰/유찰) - 관리자 열람용

        self._timer_task: Optional[asyncio.Task] = None
        self._broadcast: Optional[Callable[[], Awaitable[None]]] = None
        self._on_finish: Optional[Callable[["AuctionRoom"], Awaitable[None]]] = None
        self._save: Optional[Callable[["AuctionRoom"], None]] = None
        self._lock = asyncio.Lock()

    # ---------- 영속화(재시작 복구) ----------
    def to_dict(self) -> dict:
        return {
            "id": self.id, "host_token": self.host_token, "observer_token": self.observer_token,
            "title": self.title, "team_count": self.team_count, "team_size": self.team_size,
            "total_points": self.total_points, "show_order": self.show_order,
            "bid_mode": self.bid_mode.value, "fixed_unit": self.fixed_unit, "ratio_percent": self.ratio_percent,
            "bid_time": self.bid_time, "extend_time": self.extend_time,
            "captains": [vars(c) for c in self.captains],
            "members": [vars(m) for m in self.members],
            "phase": self.phase.value, "order": self.order, "current_pos": self.current_pos,
            "current_mid": self.current_mid, "high_bid": self.high_bid, "high_cid": self.high_cid,
            "timer_remaining": self.timer_remaining, "created_at": self.created_at, "events": self.events,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuctionRoom":
        self = cls.__new__(cls)
        self.id = d["id"]; self.host_token = d["host_token"]; self.observer_token = d["observer_token"]
        self.title = d["title"]; self.team_count = d["team_count"]; self.team_size = d["team_size"]
        self.total_points = d["total_points"]; self.show_order = d["show_order"]
        self.bid_mode = BidUnitMode(d["bid_mode"]); self.fixed_unit = d["fixed_unit"]
        self.ratio_percent = d["ratio_percent"]
        self.bid_time = d["bid_time"]; self.extend_time = d["extend_time"]
        self.captains = [Captain(**c) for c in d["captains"]]
        self.members = [Member(**m) for m in d["members"]]
        self.phase = Phase(d["phase"]); self.order = d["order"]; self.current_pos = d["current_pos"]
        self.current_mid = d["current_mid"]; self.high_bid = d["high_bid"]; self.high_cid = d["high_cid"]
        self.timer_remaining = d["timer_remaining"]; self.created_at = d["created_at"]
        self.events = d.get("events", [])
        self._timer_task = None; self._broadcast = None; self._on_finish = None; self._save = None
        self._lock = asyncio.Lock()
        return self

    def _do_save(self):
        if self._save:
            try:
                self._save(self)
            except Exception as e:
                print(f"[경매 저장 실패] {e}")

    async def resume(self):
        """재시작 후 복구 시 호출. 진행 중이던 경매의 타이머를 다시 시작합니다."""
        if self.phase == Phase.BIDDING and self.current_mid:
            self.timer_remaining = self.bid_time  # 안전하게 타이머를 새로 부여
            await self._emit()
            self._start_timer()
        elif self.phase == Phase.SOLD:
            await self._advance()

    # ---------- 조회 헬퍼 ----------
    def get_captain(self, cid: str) -> Optional[Captain]:
        return next((c for c in self.captains if c.cid == cid), None)

    def get_captain_by_token(self, tok: str) -> Optional[Captain]:
        return next((c for c in self.captains if c.token == tok), None)

    def get_member(self, mid: str) -> Optional[Member]:
        return next((m for m in self.members if m.mid == mid), None)

    def min_next_bid(self) -> int:
        if self.high_cid is None:
            return 0 if self.bid_mode == BidUnitMode.FIXED else 0
        if self.bid_mode == BidUnitMode.RATIO:
            inc = max(1, int(self.high_bid * self.ratio_percent / 100))
            return self.high_bid + inc
        return self.high_bid + self.fixed_unit

    # ---------- 상태 직렬화 ----------
    def snapshot(self, role: str = "observer", viewer_cid: Optional[str] = None) -> dict:
        cur = self.get_member(self.current_mid) if self.current_mid else None
        # 다음 경매 대상 미리보기(순서 공개 옵션)
        upcoming = []
        if self.show_order and self.phase in (Phase.BIDDING, Phase.SOLD, Phase.LOBBY):
            for mid in self.order[self.current_pos + 1: self.current_pos + 6]:
                m = self.get_member(mid)
                if m:
                    upcoming.append(m.to_public())
        data = {
            "id": self.id, "title": self.title, "phase": self.phase.value,
            "team_count": self.team_count, "team_size": self.team_size,
            "total_points": self.total_points, "show_order": self.show_order,
            "bid_mode": self.bid_mode.value, "fixed_unit": self.fixed_unit,
            "ratio_percent": self.ratio_percent,
            "bid_time": self.bid_time, "extend_time": self.extend_time,
            "captains": [c.to_public(self.team_size) for c in self.captains],
            "members": [m.to_public() for m in self.members],
            "current": cur.to_public() if cur else None,
            "high_bid": self.high_bid, "high_cid": self.high_cid,
            "min_next_bid": self.min_next_bid(),
            "timer_remaining": self.timer_remaining,
            "progress": {"done": self.current_pos + 1 if self.current_pos >= 0 else 0,
                          "total": len(self.members)},
            "upcoming": upcoming,
            "role": role, "viewer_cid": viewer_cid,
        }
        return data

    def _log(self, etype: str, **data):
        self.events.append({"t": time.time(), "type": etype, **data})

    def event_log(self) -> list[dict]:
        """관리자 열람용 이벤트 로그(사람이 읽기 좋은 형태)."""
        out = []
        for e in self.events:
            out.append({
                "time": e["t"], "type": e["type"],
                "member": e.get("member"), "captain": e.get("captain"),
                "amount": e.get("amount"),
            })
        return out

    # ---------- 진행 제어 ----------
    def bind(self, broadcast, on_finish, save=None):
        self._broadcast = broadcast
        self._on_finish = on_finish
        self._save = save

    async def _emit(self):
        if self._broadcast:
            await self._broadcast()

    async def start(self):
        async with self._lock:
            if self.phase != Phase.LOBBY:
                return
            self.order = [m.mid for m in self.members]
            random.shuffle(self.order)
            self.current_pos = -1
            self._log("start")
        await self._advance()

    async def reshuffle(self):
        """LOBBY 상태에서 순서 다시 섞기."""
        async with self._lock:
            if self.phase != Phase.LOBBY:
                return
            random.shuffle(self.members)
            self._do_save()

    async def _advance(self):
        """다음 팀원 경매 시작 (없으면 종료)."""
        self._cancel_timer()
        async with self._lock:
            nxt = self.current_pos + 1
            # 남은 팀원 중 아직 낙찰 안 된 것 탐색
            while nxt < len(self.order):
                m = self.get_member(self.order[nxt])
                if m and m.won_by is None:
                    break
                nxt += 1
            if nxt >= len(self.order):
                self.phase = Phase.FINISHED
                self.current_mid = None
                self._do_save()
                await self._emit()
                if self._on_finish:
                    await self._on_finish(self)
                return
            self.current_pos = nxt
            self.current_mid = self.order[nxt]
            self.high_bid = 0
            self.high_cid = None
            self.timer_remaining = self.bid_time
            self.phase = Phase.BIDDING
            self._do_save()
        await self._emit()
        self._start_timer()

    def _start_timer(self):
        self._cancel_timer()
        self._timer_task = asyncio.create_task(self._run_timer())

    def _cancel_timer(self):
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    async def _run_timer(self):
        try:
            while self.timer_remaining > 0:
                await asyncio.sleep(1)
                self.timer_remaining -= 1
                await self._emit()
            await self._finalize_current()
        except asyncio.CancelledError:
            pass

    async def place_bid(self, cid: str, amount: int) -> tuple[bool, str]:
        async with self._lock:
            if self.phase != Phase.BIDDING:
                return False, "지금은 입찰할 수 없습니다."
            cap = self.get_captain(cid)
            if not cap:
                return False, "팀장 정보를 찾을 수 없습니다."
            if (self.team_size - 1) - len(cap.roster) <= 0:
                return False, "이미 팀원 정원을 모두 채웠습니다."
            min_bid = self.min_next_bid()
            if amount < min_bid or amount <= 0:
                return False, f"최소 입찰가는 {min_bid:,}P 입니다."
            if amount > cap.points:
                return False, f"보유 포인트({cap.points:,}P)를 초과했습니다."
            if cid == self.high_cid:
                return False, "이미 최고 입찰자입니다."
            self.high_bid = amount
            self.high_cid = cid
            cur_m = self.get_member(self.current_mid) if self.current_mid else None
            self._log("bid", member=cur_m.name if cur_m else None,
                      captain=cap.name, amount=amount)
            # 안티 스나이핑: 남은 시간이 연장 시간보다 적으면 연장
            if self.timer_remaining < self.extend_time:
                self.timer_remaining = self.extend_time
            self._do_save()
        await self._emit()
        return True, "입찰 완료"

    async def _finalize_current(self):
        """타이머 종료 → 낙찰 처리 후 다음으로."""
        async with self._lock:
            m = self.get_member(self.current_mid) if self.current_mid else None
            if m and self.high_cid:
                cap = self.get_captain(self.high_cid)
                if cap:
                    cap.points -= self.high_bid
                    cap.roster.append(m.mid)
                    m.won_by = cap.cid
                    m.won_price = self.high_bid
                    self._log("sold", member=m.name, captain=cap.name, amount=self.high_bid)
            elif m:
                self._log("unsold", member=m.name)
            self.phase = Phase.SOLD
            self._do_save()
        await self._emit()
        await asyncio.sleep(3)  # 결과 잠깐 표시
        await self._advance()

    async def skip_current(self):
        """현재 팀원 유찰 처리하고 넘어가기(주최자)."""
        self._cancel_timer()
        async with self._lock:
            cur_m = self.get_member(self.current_mid) if self.current_mid else None
            if cur_m:
                self._log("unsold", member=cur_m.name)
            self.phase = Phase.SOLD
            self.high_bid = 0
            self.high_cid = None
        await self._emit()
        await self._advance()

    def cleanup(self):
        self._cancel_timer()
