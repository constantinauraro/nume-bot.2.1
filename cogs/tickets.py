import discord
from discord import app_commands
from discord.ext import commands

import config
from database import get_db

TICKET_TYPES = {
    "quote": ("💵", "Get a quote"),
    "apply": ("💼", "Apply for freelancer"),
    "support": ("🎧", "General Support"),
}


# ---------------------------------------------------------------------------
# MODALS - forms that clients fill out when opening a ticket
# ---------------------------------------------------------------------------

class QuoteModal(discord.ui.Modal, title="Get a quote"):
    project_type = discord.ui.TextInput(
        label="What type of project do you have?",
        placeholder="e.g., Minecraft build, plugin, website...",
        max_length=100,
    )
    budget = discord.ui.TextInput(
        label="Estimated budget",
        placeholder="e.g., $50-100",
        max_length=50,
    )
    deadline = discord.ui.TextInput(
        label="Desired deadline",
        placeholder="e.g., 2 weeks / flexible",
        max_length=50,
        required=False,
    )
    description = discord.ui.TextInput(
        label="Describe the project in detail",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        fields = [
            ("Project Type", str(self.project_type)),
            ("Estimated Budget", str(self.budget)),
            ("Deadline", str(self.deadline) or "Unspecified"),
            ("Description", str(self.description)),
        ]
        await create_ticket_channel(interaction, "quote", fields)


class ApplyModal(discord.ui.Modal, title="Apply for freelancer"):
    desired_role = discord.ui.TextInput(
        label="What role do you want to apply for?",
        placeholder="e.g., Builder, Graphic Designer, Bot Developer...",
        max_length=100,
    )
    experience = discord.ui.TextInput(
        label="Your experience",
        style=discord.TextStyle.paragraph,
        placeholder="How long have you been doing this, past projects...",
        max_length=500,
    )
    portfolio = discord.ui.TextInput(
        label="Portfolio link (required)",
        placeholder="https://...",
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        fields = [
            ("Desired Role", str(self.desired_role)),
            ("Experience", str(self.experience)),
            ("Portfolio", str(self.portfolio)),
        ]
        await create_ticket_channel(interaction, "apply", fields)


class SupportModal(discord.ui.Modal, title="General Support"):
    subject = discord.ui.TextInput(
        label="Subject",
        placeholder="e.g., Issue with an order",
        max_length=100,
    )
    description = discord.ui.TextInput(
        label="Describe your issue",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        fields = [
            ("Subject", str(self.subject)),
            ("Description", str(self.description)),
        ]
        await create_ticket_channel(interaction, "support", fields)


# ---------------------------------------------------------------------------
# VIEWS
# ---------------------------------------------------------------------------

class TicketPanelView(discord.ui.View):
    """Persistent view with the 3 buttons in #ticket-creation."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Get a quote", style=discord.ButtonStyle.success, emoji="💵", custom_id="mythral_ticket_quote")
    async def quote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_open_ticket(interaction.user.id, interaction.guild.id)
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(QuoteModal())

    @discord.ui.button(label="Apply for freelancer", style=discord.ButtonStyle.primary, emoji="💼", custom_id="mythral_ticket_apply")
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_open_ticket(interaction.user.id, interaction.guild.id)
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(ApplyModal())

    @discord.ui.button(label="General Support", style=discord.ButtonStyle.secondary, emoji="🎧", custom_id="mythral_ticket_support")
    async def support_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_open_ticket(interaction.user.id, interaction.guild.id)
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(SupportModal())


class CloseTicketView(discord.ui.View):
    """Persistent view with the close button inside the ticket channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="mythral_ticket_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        staff_role = interaction.guild.get_role(config.STAFF_ROLE_ID)
        if staff_role not in interaction.user.roles:
            await interaction.response.send_message("❌ You do not have permission to close this ticket! Only the administrative team can do this.", ephemeral=True)
            return

        async with get_db() as db:
            await db.execute(
                "UPDATE tickets SET status = 'closed' WHERE channel_id = ?",
                (interaction.channel.id,),
            )
            await db.commit()

        await interaction.response.send_message("🔒 This ticket is now closed and is being archived...", ephemeral=True)
        
        channel = interaction.channel
        guild = interaction.guild
        
        await channel.set_permissions(guild.default_role, view_channel=False)
        
        ARHIVA_ID = 1544151748900425829  
        archive_category = guild.get_channel(ARHIVA_ID)
        
        if archive_category:
            await channel.edit(category=archive_category, name=f"closed-{channel.name[-4:]}")


# ---------------------------------------------------------------------------
# SHARED LOGIC
# ---------------------------------------------------------------------------

async def get_open_ticket(user_id: int, guild_id: int):
    """Checks if the user already has an open ticket to prevent duplicates."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT channel_id FROM tickets WHERE owner_id = ? AND status = 'open'",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def create_ticket_channel(interaction: discord.Interaction, ticket_type: str, fields: list[tuple[str, str]]):
    guild = interaction.guild
    category = guild.get_channel(config.TICKET_CATEGORY_ID)
    emoji, label = TICKET_TYPES[ticket_type]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    staff_role = guild.get_role(config.STAFF_ROLE_ID)
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    freelancer_role = guild.get_role(1544135641275568158)
    if freelancer_role:
        overwrites[freelancer_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    channel = await guild.create_text_channel(
        name=f"{ticket_type}-{str(interaction.id)[-4:]}",
        category=category,
        overwrites=overwrites,
    )

    async with get_db() as db:
        await db.execute(
            "INSERT INTO tickets (channel_id, owner_id, ticket_type) VALUES (?, ?, ?)",
            (channel.id, interaction.user.id, ticket_type),
        )
        await db.commit()

        embed = discord.Embed(
        title="Information",
        color=discord.Color.green()
    )
    for name, value in fields:
        embed.add_field(name=name, value=value or "—", inline=False)
        
    embed.add_field(name="Rating", value="⭐⭐⭐⭐⭐ (0)", inline=False)
    embed.set_footer(text=config.STUDIO_FOOTER)
    embed.timestamp = interaction.created_at

    ping = f"New ticket for {staff_role.mention}." if staff_role else "New ticket received."
    await channel.send(content=ping, embed=embed, view=NewTicketActionsView())
    await interaction.response.send_message(f"✅ Your ticket has been created: {channel.mention}", ephemeral=True)



# ---------------------------------------------------------------------------
# COG REGISTRATION - The missing part that caused the crash
# ---------------------------------------------------------------------------

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(CloseTicketView())

async def setup(bot):
    await bot.add_cog(Tickets(bot))
