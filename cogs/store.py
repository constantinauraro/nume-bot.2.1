import discord
from discord import app_commands
from discord.ext import commands

import config
from database import get_db


class ProductView(discord.ui.View):
    def __init__(self, buy_url: str = None):
        super().__init__(timeout=None)
        if buy_url:
            self.add_item(discord.ui.Button(label="Buy it", url=buy_url, style=discord.ButtonStyle.link, emoji="🛒"))


class Store(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="add-product", description="(Staff) Adaugă un produs nou în magazin")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        name="Numele produsului",
        description="Descrierea produsului",
        price="Prețul (ex: $9.99)",
        image_url="Link către imaginea produsului",
        buy_url="Link către pagina de cumpărare",
    )
    async def add_product(
        self,
        interaction: discord.Interaction,
        name: str,
        description: str,
        price: str,
        image_url: str = None,
        buy_url: str = None,
    ):
        async with get_db() as db:
            await db.execute(
                "INSERT INTO products (name, description, price, image_url, buy_url) VALUES (?, ?, ?, ?, ?)",
                (name, description, price, image_url, buy_url),
            )
            await db.commit()

        embed = discord.Embed(title=f"💲 {name}", description=description, color=config.COLOR_MAIN)
        embed.add_field(name="Price", value=price)
        if image_url:
            embed.set_image(url=image_url)
        embed.set_footer(text=config.STUDIO_FOOTER)

        channel = interaction.guild.get_channel(config.STORE_CHANNEL_ID) or interaction.channel
        await channel.send(embed=embed, view=ProductView(buy_url))
        await interaction.response.send_message("✅ Produsul a fost publicat.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Store(bot))
