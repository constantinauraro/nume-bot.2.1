import discord
from discord import app_commands
from discord.ext import commands

import config
from database import get_db
from cogs.profiles import stars


class ReviewModal(discord.ui.Modal, title="Lasă o recenzie"):
    service = discord.ui.TextInput(label="Serviciu oferit", placeholder="ex: Builder, Web Developer...", required=True)
    rating = discord.ui.TextInput(label="Rating (1-5)", placeholder="5", max_length=1, required=True)
    comment = discord.ui.TextInput(
        label="Comentariu", style=discord.TextStyle.paragraph, required=True, max_length=500
    )

    def __init__(self, freelancer: discord.Member):
        super().__init__()
        self.freelancer = freelancer

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rating_val = max(1, min(5, int(str(self.rating))))
        except ValueError:
            rating_val = 5

        async with get_db() as db:
            await db.execute(
                "INSERT INTO reviews (freelancer_id, reviewer_id, service, rating, comment) VALUES (?, ?, ?, ?, ?)",
                (self.freelancer.id, interaction.user.id, str(self.service), rating_val, str(self.comment)),
            )
            await db.commit()

        embed = discord.Embed(
            title=f"⭐ New review from {interaction.user.display_name}",
            color=config.COLOR_GOLD,
        )
        embed.add_field(name="Service Provided", value=str(self.service), inline=False)
        embed.add_field(name="Freelancer", value=self.freelancer.display_name, inline=False)
        embed.add_field(name="Rating", value=stars(rating_val) + f" ({rating_val}/5)", inline=False)
        embed.add_field(name="Comment", value=f"```{self.comment}```", inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"Review System • {config.STUDIO_NAME}")

        channel = interaction.guild.get_channel(config.REVIEWS_CHANNEL_ID)
        target_channel = channel or interaction.channel
        await target_channel.send(embed=embed)

        await interaction.response.send_message("✅ Mulțumim pentru recenzie!", ephemeral=True)


class Reviews(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="review", description="Lasă o recenzie pentru un freelancer")
    @app_commands.describe(freelancer="Freelancerul pe care vrei să îl recenzezi")
    async def review(self, interaction: discord.Interaction, freelancer: discord.Member):
        if freelancer.id == interaction.user.id:
            await interaction.response.send_message("Nu poți să te recenzezi singur.", ephemeral=True)
            return
        await interaction.response.send_modal(ReviewModal(freelancer))


async def setup(bot: commands.Bot):
    await bot.add_cog(Reviews(bot))
