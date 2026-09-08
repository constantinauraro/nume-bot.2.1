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
# MENIUL DE SELECȚIE PENTRU RECUREZĂ (DENY REASONS DROPDOWN)
# ---------------------------------------------------------------------------

class DenyReasonSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Won't fit in the deadline", description="The deadline is too short for our freelancers.", emoji="⏳"),
            discord.SelectOption(label="Not interested", description="Our freelancers are not interested in this project.", emoji="❌"),
            discord.SelectOption(label="Not my niche", description="This project is outside of our studio's expertise.", emoji="🎨"),
            discord.SelectOption(label="Budget", description="The budget is too low for the requested work.", emoji="💰"),
        ]
        super().__init__(placeholder="Select a deny reason", min_values=1, max_values=1, options=options, custom_id="mythral_deny_select")

    async def callback(self, interaction: discord.Interaction):
        # Doar staff-ul poate folosi acest dropdown
        staff_role = interaction.guild.get_role(config.STAFF_ROLE_ID)
        if staff_role not in interaction.user.roles:
            await interaction.response.send_message("❌ Only staff can select the reason.", ephemeral=True)
            return

        reason = self.values[0]
        await interaction.response.send_message(f"🔒 Ticket denied. Reason: **{reason}**. Archiving...", ephemeral=True)
        
        # Trimite un mesaj public în canal cu motivul înainte de arhivare
        embed_reason = discord.Embed(
            title="Ticket Denied",
            description=f"This ticket has been rejected by the administration.\n**Reason:** {reason}",
            color=discord.Color.red()
        )
        await interaction.channel.send(embed=embed_reason)
        
        # Procesul de arhivare
        await interaction.channel.set_permissions(interaction.guild.default_role, view_channel=False)
        archive_category = interaction.guild.get_channel(1544151748900425829)
        if archive_category:
            await interaction.channel.edit(category=archive_category, name=f"closed-{interaction.channel.name[-4:]}")


class DenyReasonView(discord.ui.View):
    """View-ul efemer care conține dropdown-ul."""
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(DenyReasonSelect())


# ---------------------------------------------------------------------------
# VIEWS PENTRU PANOURI
# ---------------------------------------------------------------------------

class NewTicketActionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Quote", style=discord.ButtonStyle.success, custom_id="mythral_action_quote")
    async def quote_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("💡 Feature matching this button will be triggered here.", ephemeral=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="mythral_action_deny")
    async def deny_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        staff_role = interaction.guild.get_role(config.STAFF_ROLE_ID)
        if staff_role not in interaction.user.roles:
            await interaction.response.send_message("❌ Only staff can deny/close this ticket.", ephemeral=True)
            return
        
        # În loc să închidem direct, generăm caseta efemeră "What went wrong?" cu meniul
        embed = discord.Embed(
            title="What went wrong?",
            description="Select a reason that explains your reason for denying the ticket.",
            color=discord.Color.green()
        )
        embed.set_footer(text=config.STUDIO_FOOTER)
        embed.timestamp = interaction.created_at
        
        await interaction.response.send_message(embed=embed, view=DenyReasonView(), ephemeral=True)

    @discord.ui.button(label="Reviews", style=discord.ButtonStyle.primary, custom_id="mythral_action_reviews")
    async def reviews_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⭐ Displaying freelancer reviews...", ephemeral=True)


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
        return row if row else None


async def create_ticket_channel(interaction: discord.Interaction, ticket_type: str, fields: list[tuple[str, str]]):
    guild = interaction.guild

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(NewTicketActionsView())

async def setup(bot):
    await bot.add_cog(Tickets(bot))
