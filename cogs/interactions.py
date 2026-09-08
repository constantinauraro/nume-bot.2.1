import discord
from discord import app_commands
from discord.ext import commands

import config
from database import get_db


class OrderRequestModal(discord.ui.Modal, title="Descrie cererea ta"):
    description = discord.ui.TextInput(
        label="Ce ai nevoie de la acest freelancer?",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    def __init__(self, freelancer: discord.Member):
        super().__init__()
        self.freelancer = freelancer

    async def on_submit(self, interaction: discord.Interaction):
        await Interactions.create_order_channel(interaction, self.freelancer, str(self.description))


class AcceptDeclineView(discord.ui.View):
    """Butoanele pe care le vede freelancerul pentru a accepta/refuza comanda."""

    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅", custom_id="mythral_order_accept")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, "accepted")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌", custom_id="mythral_order_decline")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, "declined")

    async def _resolve(self, interaction: discord.Interaction, new_status: str):
        async with get_db() as db:
            cursor = await db.execute("SELECT freelancer_id, client_id, status FROM orders WHERE id = ?", (self.order_id,))
            row = await cursor.fetchone()
            if not row:
                await interaction.response.send_message("Comanda nu mai există.", ephemeral=True)
                return
            freelancer_id, client_id, status = row

            if interaction.user.id != freelancer_id:
                await interaction.response.send_message("Doar freelancerul poate răspunde la această cerere.", ephemeral=True)
                return
            if status != "pending":
                await interaction.response.send_message(f"Această cerere a fost deja marcată ca **{status}**.", ephemeral=True)
                return

            await db.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, self.order_id))
            await db.commit()

        button_used = self.children[0] if new_status == "accepted" else self.children[1]
        for child in self.children:
            child.disabled = True

        color = config.COLOR_SUCCESS if new_status == "accepted" else config.COLOR_DANGER
        verb = "acceptată ✅" if new_status == "accepted" else "refuzată ❌"

        embed = discord.Embed(
            description=f"Cererea a fost **{verb}** de către {interaction.user.mention}.",
            color=color,
        )

        await interaction.response.edit_message(view=self)
        await interaction.channel.send(embed=embed)

        if new_status == "accepted":
            complete_view = MarkCompletedView(self.order_id)
            await interaction.channel.send(
                "Când lucrarea este finalizată, apasă butonul de mai jos pentru a marca comanda ca terminată:",
                view=complete_view,
            )


class MarkCompletedView(discord.ui.View):
    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(label="Mark as Completed", style=discord.ButtonStyle.success, emoji="🏁", custom_id="mythral_order_complete")
    async def complete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_db() as db:
            cursor = await db.execute("SELECT client_id, freelancer_id, status FROM orders WHERE id = ?", (self.order_id,))
            row = await cursor.fetchone()
            if not row:
                await interaction.response.send_message("Comanda nu mai există.", ephemeral=True)
                return
            client_id, freelancer_id, status = row

            if interaction.user.id not in (freelancer_id,) and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("Doar freelancerul sau staff-ul poate finaliza comanda.", ephemeral=True)
                return
            if status == "completed":
                await interaction.response.send_message("Comanda este deja marcată ca finalizată.", ephemeral=True)
                return

            await db.execute("UPDATE orders SET status = 'completed' WHERE id = ?", (self.order_id,))
            await db.commit()

        button.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(f"🏁 Comanda a fost marcată ca **finalizată** de {interaction.user.mention}.")

        await Interactions.bump_customer_level(interaction.guild, client_id)


class Interactions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(AcceptDeclineView(0))  # placeholder pt persistenta - custom_id-urile conteaza
        bot.add_view(MarkCompletedView(0))

    async def start_order_flow(self, interaction: discord.Interaction, freelancer_id: int):
        freelancer = interaction.guild.get_member(freelancer_id)
        if not freelancer:
            await interaction.response.send_message("Nu am putut găsi acest freelancer pe server.", ephemeral=True)
            return
        if freelancer.id == interaction.user.id:
            await interaction.response.send_message("Nu poți plasa o comandă către tine însuți.", ephemeral=True)
            return
        await interaction.response.send_modal(OrderRequestModal(freelancer))

    @staticmethod
    async def create_order_channel(interaction: discord.Interaction, freelancer: discord.Member, description: str):
        guild = interaction.guild
        category = guild.get_channel(config.TICKET_CATEGORY_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            freelancer: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        staff_role = guild.get_role(config.STAFF_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel = await guild.create_text_channel(
            name=f"order-{interaction.user.name}-{freelancer.name}",
            category=category,
            overwrites=overwrites,
        )

        async with get_db() as db:
            cursor = await db.execute(
                "INSERT INTO orders (client_id, freelancer_id, channel_id, description) VALUES (?, ?, ?, ?)",
                (interaction.user.id, freelancer.id, channel.id, description),
            )
            await db.commit()
            order_id = cursor.lastrowid

        embed = discord.Embed(
            title="📦 Cerere nouă de comandă",
            description=description,
            color=config.COLOR_MAIN,
        )
        embed.add_field(name="Client", value=interaction.user.mention)
        embed.add_field(name="Freelancer", value=freelancer.mention)
        embed.set_footer(text=config.STUDIO_FOOTER)

        await channel.send(
            content=f"{freelancer.mention}, ai o cerere nouă de la {interaction.user.mention}!",
            embed=embed,
            view=AcceptDeclineView(order_id),
        )
        await interaction.response.send_message(f"✅ Cererea ta a fost trimisă: {channel.mention}", ephemeral=True)

    @staticmethod
    async def bump_customer_level(guild: discord.Guild, client_id: int):
        async with get_db() as db:
            await db.execute(
                """INSERT INTO customer_progress (user_id, completed_orders) VALUES (?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET completed_orders = completed_orders + 1""",
                (client_id,),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT completed_orders, current_level FROM customer_progress WHERE user_id = ?",
                (client_id,),
            )
            completed, current_level = await cursor.fetchone()

        # Gasim cel mai mare nivel atins
        new_level = None
        for threshold in sorted(config.CUSTOMER_LEVELS.keys()):
            if completed >= threshold:
                new_level = config.CUSTOMER_LEVELS[threshold]

        if new_level and new_level != current_level:
            async with get_db() as db:
                await db.execute(
                    "UPDATE customer_progress SET current_level = ? WHERE user_id = ?",
                    (new_level, client_id),
                )
                await db.commit()

            channel = guild.get_channel(config.LEVELS_CHANNEL_ID)
            member = guild.get_member(client_id)
            if channel and member:
                role = discord.utils.get(guild.roles, name=new_level)
                if role:
                    try:
                        await member.add_roles(role)
                    except discord.Forbidden:
                        pass

                embed = discord.Embed(
                    title="🎉 Congratulations!",
                    description=f"{member.mention} a ajuns la nivelul **@{new_level}** în nivelele noastre de clienți.",
                    color=config.COLOR_GOLD,
                )
                embed.set_footer(text=config.STUDIO_FOOTER)
                await channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Interactions(bot))
