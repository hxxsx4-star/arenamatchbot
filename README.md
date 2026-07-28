# arenamatchbot

종합게임 아레나 **내전봇** (내전 + 경매 + 승부예측).

- `cogs/match.py`   : 내전 모집/진행 (내전 시작 로그 → 공유 큐 → 로그봇)
- `cogs/auction.py` : 경매 (경매 사이트는 추후 확장 예정)
- `cogs/predict.py`, `cogs/ui_predict.py` : 승부예측
- `cogs/sync.py`    : 슬래시 커맨드 수동 동기화
- `cogs/rules_gate.py` : 내전 규칙 안내 임베드 + ✅ 반응 시 내전 역할 자동 지급

## 실행
```
cp config.ini.example config.ini   # 토큰 입력
pip install -r requirements.txt
python main.py
```
