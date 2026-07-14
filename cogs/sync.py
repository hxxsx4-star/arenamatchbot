import discord
from discord.ext import commands
from discord import app_commands

class SyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="동기화", description="봇의 슬래시 커맨드를 현재 서버에 즉시 동기화합니다.")
    @app_commands.default_permissions(manage_guild=True) # 서버 관리자만 사용 가능
    async def sync_command(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            # 현재 명령어가 실행된 길드(서버)에만 커맨드 트리 동기화
            await self.bot.tree.sync(guild=interaction.guild)
            await interaction.followup.send(f"✅ **{interaction.guild.name}** 서버의 커맨드를 동기화했습니다.", ephemeral=True)
            print(f"🌀 '{interaction.guild.name}' (ID: {interaction.guild.id}) 서버에서 수동 동기화 요청이 있었습니다.")
        except Exception as e:
            await interaction.followup.send(f"❌ 동기화 중 오류가 발생했습니다: {e}", ephemeral=True)
            print(f"❌ '{interaction.guild.name}' 서버 동기화 실패: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot))