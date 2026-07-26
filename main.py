# main.py — 종합게임 아레나 내전봇 (내전 + 경매 + 승부예측)
import asyncio
import configparser
import json
import time
import traceback
import urllib.request

import discord
from discord.ext import commands

from utils.database import init_db
from utils.cmdexport import export_commands
from utils.logs import ERROR_LOG_CH, init_log_queue

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
            "cogs.esports",    # e스포츠(LCK/MSI/Worlds) 자동 예측
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
        n = export_commands(self, "matchbot", "내전봇")
        print(f"📖 명령어 {n}개를 봇공지용으로 내보냈습니다.")

    async def on_ready(self):
        print(f"✅ 내전봇 로그인 성공: {self.user} (ID: {self.user.id})")

    async def on_error(self, event_method, *args, **kwargs):
        # 이벤트 핸들러(on_message 등) 내부에서 발생한 예외는 봇을 죽이지 않고
        # 여기로 넘어옵니다. 콘솔에도 남기고, 운영자가 바로 볼 수 있게 디스코드에도 알립니다.
        err_text = traceback.format_exc()
        print(f"🚨 [이벤트 오류: {event_method}]\n{err_text}")
        channel = self.get_channel(ERROR_LOG_CH)
        if channel:
            try:
                await channel.send(
                    f"⚠️ **이벤트 처리 중 오류** (`{event_method}`)\n```py\n{err_text[-1800:]}\n```"
                )
            except Exception as e:
                print(f"🚨 [오류 로그 전송 실패] {e}")


def _report_crash_to_discord(token: str, error_text: str):
    """봇 프로세스 자체가 죽는 크래시는 게이트웨이 연결이 끊긴 상태라
    on_error 로 잡을 수 없습니다. 봇 토큰으로 REST API를 직접 호출해 알립니다."""
    if not ERROR_LOG_CH or not token:
        return
    try:
        content = f"🚨 **내전봇 프로세스가 예기치 않게 종료되었습니다 (자동 재시작 예정)**\n```py\n{error_text[-1800:]}\n```"
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{ERROR_LOG_CH}/messages",
            data=json.dumps({"content": content}).encode("utf-8"),
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                # Discord 는 기본 Python-urllib User-Agent 를 403 으로 막는다.
                # 이게 없으면 크래시 알림이 조용히 실패한다.
                "User-Agent": "DiscordBot (https://arenamatch.p-e.kr, 1.0)",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"🚨 [크래시 알림 전송 실패] {e}")


async def main():
    if not TOKEN:
        raise SystemExit("🚨 토큰이 비어 있습니다. config.ini 파일을 확인하세요.")
    bot = MatchBot()
    async with bot:
        # bot.run() 과 달리 start() 는 discord.py 로깅을 세팅하지 않아
        # 게이트웨이 연결/오류 로그가 아예 안 남는다. 다른 봇과 동일하게 맞춘다.
        discord.utils.setup_logging()
        await bot.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("🚨 토큰이 비어 있습니다. config.ini 파일을 확인하세요.")

    BACKOFF_START = 5
    BACKOFF_MAX = 300
    backoff = BACKOFF_START

    while True:
        try:
            asyncio.run(main())
            # 예외 없이 끝났다면 close() 등으로 정상 종료된 것 → 재시작하지 않음
            break
        except discord.LoginFailure:
            # 토큰이 잘못된 경우: 재시작해봐야 계속 실패하므로 바로 중단
            print("🚨 로그인 실패: 토큰(config.ini / DISCORD_TOKEN)을 확인하세요.")
            raise
        except Exception:
            err_text = traceback.format_exc()
            print(f"🚨 [봇 종료 - 예외 발생]\n{err_text}")
            _report_crash_to_discord(TOKEN, err_text)
            print(f"⏳ {backoff}초 후 자동 재시작합니다...")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
