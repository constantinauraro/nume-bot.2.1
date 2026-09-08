import discord
from discord import app_commands
from discord.ext import commands

import config
from database import get_db


async def get_avg_rating(freelancer_id: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT AVG(rating), COUNT(*) FROM reviews WHERE freelancer_id = ?",
            (freelancer_id,),
        )
        avg, count = await cursor.fetchone()
        return (avg or 0), (count or 0)


def stars(avg: float) -> str:
    full = round(avg)
    return "⭐" * full + "☆" * (5 - full)


class ProfileView(discord.ui.View):
    def __init__(self, freelancer_id: int, portfolio_url: str | None):
        super().__init__(timeout=None)
        self.freelancer_id = freelancer_id
        if portfolio_url:
            self.add_item(discord.ui.Button(label="Portfolio", url=portfolio_url, style=discord.ButtonStyle.link))

    @discord.ui.button(label="Reviews", style=discord.ButtonStyle.secondary, custom_id="mythral_profile_reviews")
    async def reviews_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT reviewer_id, service, rating, comment FROM reviews WHERE freelancer_id = ? ORDER BY id DESC LIMIT 5",
                (self.freelancer_id,),
            )
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message("Acest freelancer nu are încă recenzii.", ephemeral=True)
            return

        embed = discord.Embed(title="Ultimele recenzii", color=config.COLOR_GOLD)
        for reviewer_id, service, rating, comment in rows:
            embed.add_field(
                name=f"{stars(rating)} — {service or 'N/A'}",
                value=comment or "*(fără comentariu)*",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Order from...", style=discord.ButtonStyle.success, custom_id="mythral_profile_order")
    async def order_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Trimitem catre modulul de interactiuni client-freelancer
        interactions_cog = interaction.client.get_cog("Interactions")
        if interactions_cog:
            await interactions_cog.start_order_flow(interaction, self.freelancer_id)
        else:
            await interaction.response.send_message("Modulul de comenzi nu este disponibil momentan.", ephemeral=True)


class Profiles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="set-profile", description="Creează sau actualizează profilul tău de freelancer")
    @app_commands.describe(
        role="Rolul tau (ex: Builder, Graphic Designer, Bot Developer)",
        bio="Scurta descriere despre tine",
        portfolio_url="Link catre portofoliul tau (optional)",
    )
    async def set_profile(self, interaction: discord.Interaction, role: str, bio: str, portfolio_url: str = None):
        async with get_db() as db:
            await db.execute(
                """INSERT INTO freelancers (user_id, display_name, bio, role, portfolio_url, avatar_url)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       display_name=excluded.display_name,
                       bio=excluded.bio,
                       role=excluded.role,
                       portfolio_url=excluded.portfolio_url,
                       avatar_url=excluded.avatar_url""",
                (
                    interaction.user.id,
                    interaction.user.display_name,
                    bio,
                    role,
                    portfolio_url,
                    interaction.user.display_avatar.url,
                ),
            )
            await db.commit()
        await interaction.response.send_message("✅ Profilul tău a fost salvat/actualizat.", ephemeral=True)

    @app_commands.command(name="profile", description="Afișează profilul unui freelancer")
    @app_commands.describe(member="Freelancerul al cărui profil vrei să îl vezi")
    async def profile(self, interaction: discord.Interaction, member: discord.Member):
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT display_name, bio, role, portfolio_url, avatar_url FROM freelancers WHERE user_id = ?",
                (member.id,),
            )
            row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message("Acest utilizator nu are un profil de freelancer setat.", ephemeral=True)
            return

        display_name, bio, role, portfolio_url, avatar_url = row
        avg, count = await get_avg_rating(member.id)

        embed = discord.Embed(title=f"{display_name}'s profile", color=config.COLOR_MAIN)
        embed.description = f"🛠️ **{role}**\n{bio}"
        embed.add_field(name="Average rating", value=f"{stars(avg)} ({count} reviews)")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        embed.set_footer(text=config.STUDIO_FOOTER)

        await interaction.response.send_message(embed=embed, view=ProfileView(member.id, portfolio_url))


async def setup(bot: commands.Bot):
    await bot.add_cog(Profiles(bot))
