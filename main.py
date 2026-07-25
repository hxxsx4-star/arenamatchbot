# main.py — 종합게임 아레나 내전봇 (내전 + 경매 + 승부예측)
import asyncio
import configparser

import discord
from discord.ext import commands

from utils.database import init_db
from utils.logs import init_log_queue

# --- 설정 로드 ---
config = configparser.ConfigParser()
config.read("config.ini", encoding="utf-8")
TOKEN = config.get("Settings", "token", fallback="").strip()
# config.ini 가 없으면 환경변수(DISCORD_TOKEN)에서 토큰을 읽습니다. (도커/CI 배포용)
if not TOKEN:
    import os
    TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()


class MatchBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await init_db()
        print("✅ 데이터베이스 초기화 완료")
        init_log_queue()
        print("✅ 로그 큐 초기화 완료")

        extensions = [
            "cogs.sync",       # 슬래시 커맨드 수동 동기화
            "cogs.registration",  # 닉네임/라인 등록
            "cogs.match",      # 내전 (내전 로그 → 공유 큐)
            "cogs.auction",    # 경매 (경매 사이트는 추후 확장)
            "cogs.predict",    # 승부예측
            "cogs.stats_lookup",  # 내전 전적 조회 (/내전전적)
        ]
        for ext in extensions:
            try:
                await self.load_extension(ext)
                print(f"▶️ {ext} 로드 완료")
            except Exception as e:
                print(f"🚨 {ext} 로드 실패 : {e}")

        synced = await self.tree.sync()
        print(f"🌀 총 {len(synced)}개의 커맨드를 동기화했습니다!")

    async def on_ready(self):
        print(f"✅ 내전봇 로그인 성공: {self.user} (ID: {self.user.id})")


async def main():
    if not TOKEN:
        raise SystemExit("🚨 토큰이 비어 있습니다. config.ini 파일을 확인하세요.")
    bot = MatchBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
