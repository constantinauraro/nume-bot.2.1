import discord
from discord import app_commands
from discord.ext import commands

import config
from database import get_db
from cogs.interactions import Interactions


class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="my-level", description="Vezi nivelul tău de client")
    async def my_level(self, interaction: discord.Interaction):
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT completed_orders, current_level FROM customer_progress WHERE user_id = ?",
                (interaction.user.id,),
            )
            row = await cursor.fetchone()

        completed = row[0] if row else 0
        level = row[1] if row and row[1] else "Fără nivel încă"

        embed = discord.Embed(title=f"Nivelul tău, {interaction.user.display_name}", color=config.COLOR_MAIN)
        embed.add_field(name="Comenzi finalizate", value=str(completed))
        embed.add_field(name="Nivel curent", value=level)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="add-completed-order", description="(Staff) Adaugă manual o comandă finalizată unui client")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add_completed_order(self, interaction: discord.Interaction, client: discord.Member):
        await Interactions.bump_customer_level(interaction.guild, client.id)
        await interaction.response.send_message(f"✅ Adăugat o comandă finalizată pentru {client.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Levels(bot))
