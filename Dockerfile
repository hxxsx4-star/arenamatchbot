FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저 설치 (레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스 복사
COPY . .

# 공유 데이터(stats.json, log_queue.db) 위치. compose에서 볼륨으로 마운트.
# print/traceback 이 버퍼에 갇혀 유실되지 않도록(크래시 로그 확인용)
ENV PYTHONUNBUFFERED=1

ENV ARENA_SHARED_DIR=/data

# 토큰은 DISCORD_TOKEN 환경변수로 주입 (config.ini 불필요)
CMD ["python", "main.py"]
