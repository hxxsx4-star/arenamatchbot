# 아레나 볼 — 종합게임 아레나 내전 커뮤니티 사이트

pokeball-lol('몬스터 볼') 스타일을 참고한, 아레나 서버 전용 내전 허브.
경매 웹앱(`web/`)과 같은 FastAPI 패턴이며 **별도 포트(기본 8100)** 로 실행한다.

## 기능
- **대시보드(`/`)** — 이번 주 TOP 3(최다 킬 / 최고 승률 / 최고 KDA), 이번 주 MVP,
  우수 참여자 TOP 5, 다가오는 내전 일정, 최근 내전 결과.
- **전적 검색(`/search`)** — 라이엇 ID로 내전 전적·챔피언별 성적·최근 경기 조회.
  `RIOT_API_KEY` 가 있으면 실 솔랭 티어도 함께 표시(없으면 내부 기록만).
- **소환사 관리(`/summoners`)** — 내전 명단 등록/삭제(관리자).
- **모집/참여(`/betting`)** — 내기 생성/참가, LOL 솔랭 / TFT 롤체 탭.
- **사다리타기(`/ladder`)** — 캔버스 기반 사다리 게임.
- **기록실(`/records`)** — 일반/칼바람/명예의 전당 탭, 경기 추가(킬·데스·어시·챔피언·MVP).

경기·소환사·일정 추가/삭제는 **디스코드 OAuth 관리자**만 가능(경매 앱과 동일한 ADMIN_IDS).

## 데이터
공유 볼륨 `${ARENA_SHARED_DIR}/arena_site/` 에 JSON 으로 저장 → 재시작에도 유지.
대시보드/전적은 이 기록에서 계산된다(내전봇의 `/내전` 결과와는 별개의 웹 기록).

## 실행
```bash
pip install -r arenasite/requirements.txt   # 경매 앱과 의존성 거의 동일
# 리포 루트에서:
uvicorn arenasite.server:app --host 0.0.0.0 --port 8100
# 또는
python -m arenasite.server
```

## 환경변수
| 변수 | 설명 |
|---|---|
| `ARENA_SHARED_DIR` | 공유 데이터 경로 (기본 `/home/hxxsx4/shared_data`) |
| `PUBLIC_BASE_URL` | 외부 공개 URL (OAuth redirect 용) |
| `SESSION_SECRET` | 세션 서명 키 |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | 관리자 로그인용 OAuth |
| `ADMIN_DISCORD_IDS` | 관리자 디스코드 ID(콤마 구분) |
| `DISCORD_INVITE_URL` | 상단 'Discord 연동' 버튼 링크(선택) |
| `RIOT_API_KEY` | 라이엇 API 키(선택 — 없으면 티어 표시 생략) |
| `SITE_NAME` | 상단 로고 문구(기본 `아레나 볼`) |

## Caddy(HTTPS) 예시
경매 사이트(`arenamatch.p-e.kr`)와 별도 서브도메인으로 붙이면 된다.
```
arena.p-e.kr {
    reverse_proxy localhost:8100
}
```
OAuth redirect: `https://arena.p-e.kr/auth/callback` 를 디스코드 앱에 등록.
