# 아레나 경매 웹 (arenamatchbot/web)

teambid 스타일 실시간 팀 드래프트 경매.

- `server.py` : FastAPI + WebSocket 서버 (방/링크/실시간 입찰)
- `auction.py` : 경매 상태머신 (셔플·타이머·연장·낙찰)
- `discord_hook.py` : 종료 시 결과를 공유 로그 큐 → 로그봇이 내전 로그 채널에 기록
- `templates/`, `static/` : 프론트(생성 폼 + 실시간 룸)

## 로컬 실행
```
pip install -r web/requirements.txt
# 리포 루트에서:
python -m web.server           # 또는
uvicorn web.server:app --host 0.0.0.0 --port 8000
```
브라우저에서 http://localhost:8000

## 배포 (arenamatch.p-e.kr)
- 공개 도메인으로 링크를 만들려면 환경변수 지정:
  `PUBLIC_BASE_URL=https://arenamatch.p-e.kr`
- 리버스 프록시(nginx/Caddy)에서 80/443 → 127.0.0.1:8000 로 전달하고
  **WebSocket 업그레이드**를 허용해야 합니다. (아래 nginx 예시)

```nginx
server {
    server_name arenamatch.p-e.kr;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }
}
```
> 참고: 경매 방은 메모리에 보관되어 서버 재시작 시 사라집니다(라이브 이벤트용).
