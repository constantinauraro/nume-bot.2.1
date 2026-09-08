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
# MODALE - formularele pe care le completeaza clientul la deschiderea tichetului
# ---------------------------------------------------------------------------

class QuoteModal(discord.ui.Modal, title="Get a quote"):
    project_type = discord.ui.TextInput(
        label="Ce tip de proiect ai?",
        placeholder="ex: Minecraft build, plugin, website...",
        max_length=100,
    )
    budget = discord.ui.TextInput(
        label="Buget estimativ",
        placeholder="ex: $50-100",
        max_length=50,
    )
    deadline = discord.ui.TextInput(
        label="Termen limită dorit",
        placeholder="ex: 2 săptămâni / flexibil",
        max_length=50,
        required=False,
    )
    description = discord.ui.TextInput(
        label="Descrie proiectul în detaliu",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        fields = [
            ("Tip proiect", str(self.project_type)),
            ("Buget estimativ", str(self.budget)),
            ("Termen limită", str(self.deadline) or "Nespecificat"),
            ("Descriere", str(self.description)),
        ]
        await create_ticket_channel(interaction, "quote", fields)


class ApplyModal(discord.ui.Modal, title="Apply for freelancer"):
    desired_role = discord.ui.TextInput(
        label="Ce rol vrei să ocupi?",
        placeholder="ex: Builder, Graphic Designer, Bot Developer...",
        max_length=100,
    )
    experience = discord.ui.TextInput(
        label="Experiența ta",
        style=discord.TextStyle.paragraph,
        placeholder="De cât timp faci asta, ce ai lucrat până acum...",
        max_length=500,
    )
    portfolio = discord.ui.TextInput(
        label="Link portofoliu (obligatoriu)",
        placeholder="https://...",
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        fields = [
            ("Rol dorit", str(self.desired_role)),
            ("Experiență", str(self.experience)),
            ("Portofoliu", str(self.portfolio)),
        ]
        await create_ticket_channel(interaction, "apply", fields)


class SupportModal(discord.ui.Modal, title="General Support"):
    subject = discord.ui.TextInput(
        label="Subiect",
        placeholder="ex: Problemă cu o comandă",
        max_length=100,
    )
    description = discord.ui.TextInput(
        label="Descrie problema ta",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        fields = [
            ("Subiect", str(self.subject)),
            ("Descriere", str(self.description)),
        ]
        await create_ticket_channel(interaction, "support", fields)


# ---------------------------------------------------------------------------
# VIEW-URI
# ---------------------------------------------------------------------------

class TicketPanelView(discord.ui.View):
    """View-ul persistent cu cele 3 butoane din #ticket-creation."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Get a quote", style=discord.ButtonStyle.success, emoji="💵", custom_id="mythral_ticket_quote")
    async def quote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_open_ticket(interaction.user.id, interaction.guild.id)
        if existing:
            await interaction.response.send_message(f"Ai deja un tichet deschis: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(QuoteModal())

    @discord.ui.button(label="Apply for freelancer", style=discord.ButtonStyle.primary, emoji="💼", custom_id="mythral_ticket_apply")
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_open_ticket(interaction.user.id, interaction.guild.id)
        if existing:
            await interaction.response.send_message(f"Ai deja un tichet deschis: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(ApplyModal())

    @discord.ui.button(label="General Support", style=discord.ButtonStyle.secondary, emoji="🎧", custom_id="mythral_ticket_support")
    async def support_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_open_ticket(interaction.user.id, interaction.guild.id)
        if existing:
            await interaction.response.send_message(f"Ai deja un tichet deschis: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(SupportModal())


class CloseTicketView(discord.ui.View):
    """View-ul persistent cu butonul de inchidere din canalul de tichet."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="mythral_ticket_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_db() as db:
            await db.execute(
                "UPDATE tickets SET status = 'closed' WHERE channel_id = ?",
                (interaction.channel.id,),
            )
            await db.commit()

        await interaction.response.send_message("🔒 Tichetul va fi închis în câteva secunde...")
        await interaction.channel.edit(name=f"closed-{interaction.channel.name}")
        await interaction.channel.set_permissions(interaction.guild.default_role, view_channel=False)


# ---------------------------------------------------------------------------
# LOGICA PARTAJATA
# ---------------------------------------------------------------------------

async def get_open_ticket(user_id: int, guild_id: int):
    """Verifica daca userul are deja un tichet deschis, ca sa nu creeze mai multe."""
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

    channel = await guild.create_text_channel(
        name=f"{ticket_type}-{interaction.user.name}",
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
        title=f"{emoji} {label}",
        description=f"Bun venit, {interaction.user.mention}! Un membru din staff te va ajuta în curând.",
        color=config.COLOR_MAIN,
    )
    for name, value in fields:
        embed.add_field(name=name, value=value or "—", inline=False)
    embed.set_footer(text=config.STUDIO_FOOTER)

    ping = f"{staff_role.mention} — " if staff_role else ""
    await channel.send(content=f"{ping}{interaction.user.mention}", embed=embed, view=CloseTicketView())
    await interaction.response.send_message(f"✅ Tichetul tău a fost creat: {channel.mention}", ephemeral=True)


# ---------------------------------------------------------------------------
# COG
# ---------------------------------------------------------------------------

class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Inregistram view-urile persistente ca sa functioneze butoanele si dupa restart
        bot.add_view(TicketPanelView())
        bot.add_view(CloseTicketView())

    @app_commands.command(name="ticket-panel", description="Trimite panoul de creare tichete în acest canal (staff)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticket_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎫 Ticket Center",
            description=(
                "Bine ai venit în centrul de tichete. Aici poți deschide un tichet pentru a "
                "cere o ofertă de preț, a obține suport pentru un produs, sau a aplica pentru a lucra cu noi."
            ),
            color=config.COLOR_MAIN,
        )
        embed.set_footer(text=config.STUDIO_FOOTER)
        await interaction.channel.send(embed=embed, view=TicketPanelView())
        await interaction.response.send_message("✅ Panoul a fost trimis.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
