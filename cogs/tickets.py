import asyncio
import logging
import os
import traceback

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import get_db

log = logging.getLogger("tickets")

TICKET_TYPES = {
    "quote": ("💵", "Get a quote"),
    "apply": ("💼", "Apply for freelancer"),
    "support": ("🎧", "General Support"),
}

FREELANCER_ROLE_ID = 1544135641275568158
ARCHIVE_CATEGORY_ID = 1544151814012932256
FREELANCER_CATEGORY_ID = getattr(config, "FREELANCER_CATEGORY_ID", config.TICKET_CATEGORY_ID)

# Used by the ticket-welcome message (see send_ticket_welcome below).
STUDIO_NAME = "Mythral Creations"
TOS_CHANNEL_ID = 1544009125451800707
# There is no dedicated "welcome"/"server info" channel, so #general is used
# in its place for both of those links in the welcome message.
GENERAL_CHANNEL_NAME = "general"
EXECUTIVE_TEAM_LABEL = "Executive team"

# Studio logo shown instead of the client's own avatar when their messages
# are relayed to freelancers - the client must stay 100% anonymous, so no
# real avatar or name is ever attached to that side of the relay. Bundle the
# image in an "assets" folder next to this file (or update the path below).
MYTHRAL_LOGO_FILENAME = "mythral_logo.png"
MYTHRAL_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", MYTHRAL_LOGO_FILENAME)


# ---------------------------------------------------------------------------
# DB SCHEMA (auto-migration, safe to run every startup)
# ---------------------------------------------------------------------------

async def ensure_schema():
    async with get_db() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_type TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                channel_id INTEGER,
                customer_channel_id INTEGER,
                freelancer_channel_id INTEGER,
                assigned_freelancer_id INTEGER,
                status TEXT DEFAULT 'open',
                active_relay_message_id INTEGER,
                active_relay_side TEXT,
                quoted_amount TEXT,
                quoted_deadline TEXT,
                chat_prompt_message_id INTEGER
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS freelancer_profiles (
                user_id INTEGER PRIMARY KEY,
                portfolio TEXT,
                timezone TEXT,
                tech_stack TEXT,
                bio TEXT
            )
            """
        )
        # One row per freelancer -> client review, submitted via DM after a
        # ticket is closed. A client can rack up several of these across
        # different tickets/freelancers. Named "client_reviews" (not just
        # "reviews") on purpose, to not collide with any pre-existing
        # freelancer-reviews table elsewhere in the bot.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS client_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                freelancer_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # One row per client -> freelancer review, requested in-channel via
        # /review before a ticket is closed. Separate table/name from
        # client_reviews (that one is freelancer -> client) and from any
        # pre-existing "reviews" table elsewhere in the bot.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS freelancer_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                freelancer_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                rating INTEGER NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # One row per client, remembers whether they want to be pinged when
        # a freelancer sends them a chat message or a quote. Defaults to
        # "pinged" (True) for anyone who never touched the buttons.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_prefs (
                user_id INTEGER PRIMARY KEY,
                pinged INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # One row per freelancer quote. Several freelancers can each have a
        # pending row for the same ticket at once - that's what lets every
        # freelancer send their own offer instead of only the first one in.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                freelancer_id INTEGER NOT NULL,
                amount TEXT NOT NULL,
                deadline TEXT NOT NULL,
                comment TEXT,
                status TEXT DEFAULT 'pending',
                message_id INTEGER
            )
            """
        )
        # Best-effort ALTERs in case an older "tickets" table already existed
        # without these columns. Errors (column already exists) are ignored.
        for stmt in (
            "ALTER TABLE tickets ADD COLUMN customer_channel_id INTEGER",
            "ALTER TABLE tickets ADD COLUMN freelancer_channel_id INTEGER",
            "ALTER TABLE tickets ADD COLUMN assigned_freelancer_id INTEGER",
            "ALTER TABLE tickets ADD COLUMN active_relay_message_id INTEGER",
            "ALTER TABLE tickets ADD COLUMN active_relay_side TEXT",
            "ALTER TABLE tickets ADD COLUMN quoted_amount TEXT",
            "ALTER TABLE tickets ADD COLUMN quoted_deadline TEXT",
            "ALTER TABLE tickets ADD COLUMN chat_prompt_message_id INTEGER",
        ):
            try:
                await db.execute(stmt)
            except Exception:
                pass
        await db.commit()


def _stars(rating: float) -> str:
    full = round(rating)
    return "⭐" * full + "☆" * (5 - full)


async def add_client_review(ticket_id: int, client_id: int, freelancer_id: int, rating: int, comment: str | None):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO client_reviews (ticket_id, client_id, freelancer_id, rating, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticket_id, client_id, freelancer_id, rating, comment, discord.utils.utcnow().isoformat()),
        )
        await db.commit()


async def get_client_rating(client_id: int):
    """Returns (average_rating, review_count) for a client. (0.0, 0) if none yet."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT AVG(rating), COUNT(*) FROM client_reviews WHERE client_id = ?", (client_id,)
        )
        avg, count = await cursor.fetchone()
        return (round(avg, 1) if avg else 0.0, count or 0)


async def get_client_reviews(client_id: int, limit: int = 5):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT rating, comment, freelancer_id, created_at FROM client_reviews "
            "WHERE client_id = ? ORDER BY id DESC LIMIT ?",
            (client_id, limit),
        )
        return await cursor.fetchall()


async def has_reviewed(ticket_id: int, freelancer_id: int) -> bool:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT 1 FROM client_reviews WHERE ticket_id = ? AND freelancer_id = ?", (ticket_id, freelancer_id)
        )
        return await cursor.fetchone() is not None


async def add_freelancer_review(ticket_id: int, freelancer_id: int, client_id: int, service: str, rating: int, comment: str | None):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO freelancer_reviews (ticket_id, freelancer_id, client_id, service, rating, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, freelancer_id, client_id, service, rating, comment, discord.utils.utcnow().isoformat()),
        )
        await db.commit()


async def get_freelancer_rating(freelancer_id: int):
    """Returns (average_rating, review_count) for a freelancer. (0.0, 0) if none yet."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT AVG(rating), COUNT(*) FROM freelancer_reviews WHERE freelancer_id = ?", (freelancer_id,)
        )
        avg, count = await cursor.fetchone()
        return (round(avg, 1) if avg else 0.0, count or 0)


async def get_freelancer_reviews(freelancer_id: int, limit: int = 5):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT rating, comment, service, client_id, created_at FROM freelancer_reviews "
            "WHERE freelancer_id = ? ORDER BY id DESC LIMIT ?",
            (freelancer_id, limit),
        )
        return await cursor.fetchall()


async def has_reviewed_freelancer(ticket_id: int, client_id: int) -> bool:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT 1 FROM freelancer_reviews WHERE ticket_id = ? AND client_id = ?", (ticket_id, client_id)
        )
        return await cursor.fetchone() is not None


async def get_freelancer_profile(user_id: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT portfolio, timezone, tech_stack, bio FROM freelancer_profiles WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"portfolio": "Unset", "timezone": "Unset", "tech_stack": "Unset", "bio": "Unset"}
        portfolio, timezone, tech_stack, bio = row
        return {
            "portfolio": portfolio or "Unset",
            "timezone": timezone or "Unset",
            "tech_stack": tech_stack or "Unset",
            "bio": bio or "Unset",
        }


async def upsert_freelancer_profile(user_id: int, portfolio: str, timezone: str, tech_stack: str, bio: str):
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO freelancer_profiles (user_id, portfolio, timezone, tech_stack, bio)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                portfolio = excluded.portfolio,
                timezone = excluded.timezone,
                tech_stack = excluded.tech_stack,
                bio = excluded.bio
            """,
            (user_id, portfolio, timezone, tech_stack, bio),
        )
        await db.commit()


async def get_ping_pref(user_id: int) -> bool:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT pinged FROM notification_prefs WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return True if row is None else bool(row[0])


async def set_ping_pref(user_id: int, pinged: bool):
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO notification_prefs (user_id, pinged) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET pinged = excluded.pinged
            """,
            (user_id, int(pinged)),
        )
        await db.commit()


class FreelancerProfileModal(discord.ui.Modal, title="Profilul tău de freelancer"):
    portfolio = discord.ui.TextInput(
        label="Portfolio link",
        placeholder="https://...",
        max_length=200,
        required=False,
    )
    timezone = discord.ui.TextInput(
        label="Timezone",
        placeholder="e.g., +01:00",
        max_length=50,
        required=False,
    )
    tech_stack = discord.ui.TextInput(
        label="Tech Stack",
        placeholder="e.g., WorldEdit, Blender, Java",
        max_length=200,
        required=False,
    )
    bio = discord.ui.TextInput(
        label="Bio",
        style=discord.TextStyle.paragraph,
        placeholder="Spune-le clienților ceva despre tine...",
        max_length=500,
        required=False,
    )

    def __init__(self, existing: dict | None = None):
        super().__init__()
        if existing:
            self.portfolio.default = None if existing["portfolio"] == "Unset" else existing["portfolio"]
            self.timezone.default = None if existing["timezone"] == "Unset" else existing["timezone"]
            self.tech_stack.default = None if existing["tech_stack"] == "Unset" else existing["tech_stack"]
            self.bio.default = None if existing["bio"] == "Unset" else existing["bio"]

    async def on_submit(self, interaction: discord.Interaction):
        await upsert_freelancer_profile(
            interaction.user.id,
            str(self.portfolio) or None,
            str(self.timezone) or None,
            str(self.tech_stack) or None,
            str(self.bio) or None,
        )
        await interaction.response.send_message("✅ Profilul tău a fost salvat.", ephemeral=True)


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
        existing = await get_open_ticket(interaction.guild, interaction.user.id)
        if existing:
            await interaction.response.send_message(f"Ai deja un ticket deschis: <#{existing}>", ephemeral=True)
            return
        fields = [
            ("Project Type", str(self.project_type)),
            ("Estimated Budget", str(self.budget)),
            ("Deadline", str(self.deadline) or "Unspecified"),
            ("Description", str(self.description)),
        ]
        await create_quote_ticket(interaction, fields)


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
        existing = await get_open_ticket(interaction.guild, interaction.user.id)
        if existing:
            await interaction.response.send_message(f"Ai deja un ticket deschis: <#{existing}>", ephemeral=True)
            return
        fields = [
            ("Desired Role", str(self.desired_role)),
            ("Experience", str(self.experience)),
            ("Portfolio", str(self.portfolio)),
        ]
        await create_simple_ticket(interaction, "apply", fields)


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
        existing = await get_open_ticket(interaction.guild, interaction.user.id)
        if existing:
            await interaction.response.send_message(f"Ai deja un ticket deschis: <#{existing}>", ephemeral=True)
            return
        fields = [
            ("Subject", str(self.subject)),
            ("Description", str(self.description)),
        ]
        await create_simple_ticket(interaction, "support", fields)


class QuotePriceModal(discord.ui.Modal, title="Quote"):
    amount = discord.ui.TextInput(
        label="Amount",
        placeholder="The amount you would like to quote. In USD",
        max_length=50,
        required=True,
    )
    deadline = discord.ui.TextInput(
        label="Deadline",
        placeholder="e.g., 1 month 2 weeks",
        max_length=50,
        required=True,
    )
    comment = discord.ui.TextInput(
        label="Comment",
        style=discord.TextStyle.paragraph,
        placeholder="Anything else you would like to add",
        max_length=1000,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            ticket = await get_ticket_by_freelancer_channel(interaction.channel.id)
            if not ticket:
                await interaction.response.send_message("❌ Ticketul nu a fost găsit.", ephemeral=True)
                return
            if ticket["status"] != "open":
                await interaction.response.send_message(
                    "❌ Acest proiect nu mai este disponibil pentru oferte.", ephemeral=True
                )
                return

            existing = await get_pending_quote(ticket["id"], interaction.user.id)
            if existing:
                await interaction.response.send_message(
                    "❌ Ai deja o ofertă activă pentru acest client. Așteaptă răspunsul lui înainte de a trimite alta.",
                    ephemeral=True,
                )
                return

            async with get_db() as db:
                cursor = await db.execute(
                    "INSERT INTO quotes (ticket_id, freelancer_id, amount, deadline, comment, status) "
                    "VALUES (?, ?, ?, ?, ?, 'pending')",
                    (
                        ticket["id"],
                        interaction.user.id,
                        str(self.amount),
                        str(self.deadline),
                        str(self.comment) if self.comment.value else None,
                    ),
                )
                await db.commit()
                quote_id = cursor.lastrowid

            # Confirmation is EPHEMERAL - other freelancers with access to this
            # shared channel must not see who quoted or for how much.
            await interaction.response.send_message(
                "✅ Oferta ta a fost trimisă clientului, în așteptarea răspunsului.", ephemeral=True
            )

            # Rich, identified card shown to the client (client-facing identity is fine,
            # anonymity only applies between competing freelancers)
            customer_channel = interaction.guild.get_channel(ticket["customer_channel_id"])
            if customer_channel:
                profile = await get_freelancer_profile(interaction.user.id)
                wants_ping = await get_ping_pref(ticket["owner_id"])
                msg = await send_incoming_quote_card(
                    customer_channel,
                    quote_id=quote_id,
                    freelancer=interaction.user,
                    amount=str(self.amount),
                    deadline=str(self.deadline),
                    comment=str(self.comment) if self.comment.value else None,
                    profile=profile,
                    ping_user_id=ticket["owner_id"] if wants_ping else None,
                )
                async with get_db() as db:
                    await db.execute("UPDATE quotes SET message_id = ? WHERE id = ?", (msg.id, quote_id))
                    await db.commit()
            else:
                log.error(
                    "QuotePriceModal: customer_channel_id %s not found for ticket %s",
                    ticket["customer_channel_id"], ticket["id"],
                )
        except Exception:
            traceback.print_exc()
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ A apărut o eroare la trimiterea ofertei. Verifică log-ul botului.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ A apărut o eroare la trimiterea ofertei. Verifică log-ul botului.", ephemeral=True
                )


# ---------------------------------------------------------------------------
# INCOMING QUOTE CARD (client-facing, rich, per-freelancer)
# ---------------------------------------------------------------------------

async def send_incoming_quote_card(customer_channel, quote_id, freelancer, amount, deadline, comment, profile, ping_user_id=None):
    embed = discord.Embed(
        title="Incoming Quote",
        description=f"{freelancer.mention} has quoted **${amount}**",
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url=freelancer.display_avatar.url)
    fr_avg, fr_count = await get_freelancer_rating(freelancer.id)
    embed.add_field(name="Rating", value=f"{_stars(fr_avg)} ({fr_count})", inline=False)
    embed.add_field(name="Portfolio", value=profile["portfolio"], inline=False)
    embed.add_field(name="Timezone", value=profile["timezone"], inline=False)
    embed.add_field(name="Tech Stack", value=profile["tech_stack"], inline=False)
    embed.add_field(name="Bio", value=profile["bio"], inline=False)
    if comment:
        embed.add_field(name="Message from freelancer:", value=f"```{comment}```", inline=False)
    embed.add_field(name="Deadline", value=deadline, inline=False)
    embed.set_footer(text=config.STUDIO_FOOTER)
    embed.timestamp = discord.utils.utcnow()

    content = f"<@{ping_user_id}>" if ping_user_id else None
    return await customer_channel.send(content=content, embed=embed, view=IncomingQuoteView(quote_id=quote_id, amount=amount))


class IncomingQuoteView(discord.ui.View):
    """One of these is posted per freelancer quote, so several can be live in
    the customer channel at once. Not persisted across restarts, since the
    Accept button label is dynamic (bakes in the quoted amount) and each
    instance is tied to a specific quote_id. If the bot restarts mid-negotiation,
    re-send the quote (e.g. via a staff command) to get working buttons again."""

    def __init__(self, quote_id: int, amount: str):
        super().__init__(timeout=None)
        self.quote_id = quote_id
        self.amount = amount
        self.accept_btn.label = f"Accept ${amount}"

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        traceback.print_exc()
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)
        else:
            await interaction.followup.send("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        quote = await get_quote(self.quote_id)
        if not quote:
            await interaction.response.send_message("❌ Oferta nu a fost găsită.", ephemeral=True)
            return

        ticket = await get_ticket_by_id(quote["ticket_id"])
        if not ticket or ticket["owner_id"] != interaction.user.id:
            await interaction.response.send_message("❌ Doar clientul poate accepta oferta.", ephemeral=True)
            return
        if ticket["status"] != "open" or quote["status"] != "pending":
            await interaction.response.send_message("❌ Această ofertă nu mai este activă.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        _, assigned_member = await finalize_quote_acceptance(
            interaction.client, interaction.guild, ticket, quote
        )

        await interaction.followup.send(
            f"✅ Ai acceptat oferta de la {assigned_member.mention if assigned_member else '<@' + str(quote['freelancer_id']) + '>'}! "
            "Puteți discuta direct în acest canal de acum înainte."
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        quote = await get_quote(self.quote_id)
        if not quote:
            await interaction.response.send_message("❌ Oferta nu a fost găsită.", ephemeral=True)
            return

        ticket = await get_ticket_by_id(quote["ticket_id"])
        if not ticket or ticket["owner_id"] != interaction.user.id:
            await interaction.response.send_message("❌ Doar clientul poate refuza oferta.", ephemeral=True)
            return
        if quote["status"] != "pending":
            await interaction.response.send_message("❌ Această ofertă nu mai este activă.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        async with get_db() as db:
            await db.execute("UPDATE quotes SET status = 'declined' WHERE id = ?", (quote["id"],))
            await db.commit()

        await interaction.followup.send(
            "❌ Ai refuzat această ofertă. Poți continua discuția cu ceilalți freelanceri sau aștepta alte oferte."
        )

        # Told privately (DM), not in the shared freelancer channel - the
        # other freelancers still bidding don't need to see who got declined.
        freelancer = interaction.guild.get_member(quote["freelancer_id"])
        if freelancer:
            try:
                await freelancer.send("❌ Clientul a refuzat oferta ta. Poți trimite o altă ofertă oricând.")
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Counteroffer", style=discord.ButtonStyle.secondary)
    async def counteroffer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        quote = await get_quote(self.quote_id)
        if not quote:
            await interaction.response.send_message("❌ Oferta nu a fost găsită.", ephemeral=True)
            return

        ticket = await get_ticket_by_id(quote["ticket_id"])
        if not ticket or ticket["owner_id"] != interaction.user.id:
            await interaction.response.send_message("❌ Doar clientul poate face o contraofertă.", ephemeral=True)
            return
        if quote["status"] != "pending":
            await interaction.response.send_message("❌ Această ofertă nu mai este activă.", ephemeral=True)
            return
        await interaction.response.send_modal(CounterofferModal(quote_id=quote["id"]))

    @discord.ui.button(label="Message", style=discord.ButtonStyle.secondary, emoji="✉️")
    async def message_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        quote = await get_quote(self.quote_id)
        if not quote:
            await interaction.response.send_message("❌ Oferta nu a fost găsită.", ephemeral=True)
            return

        ticket = await get_ticket_by_id(quote["ticket_id"])
        if not ticket or ticket["owner_id"] != interaction.user.id:
            await interaction.response.send_message("❌ Doar clientul poate trimite un mesaj.", ephemeral=True)
            return
        await interaction.response.send_modal(MessageModal(freelancer_id=quote["freelancer_id"]))


class CounterofferModal(discord.ui.Modal, title="Trimite o contraofertă"):
    amount = discord.ui.TextInput(label="Suma propusă", placeholder="e.g., $250", max_length=50)
    deadline = discord.ui.TextInput(label="Deadline propus", placeholder="e.g., 3 weeks", max_length=50, required=False)
    message = discord.ui.TextInput(
        label="Mesaj pentru freelancer",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
    )

    def __init__(self, quote_id: int):
        super().__init__()
        self.quote_id = quote_id

    async def on_submit(self, interaction: discord.Interaction):
        quote = await get_quote(self.quote_id)
        if not quote or quote["status"] != "pending":
            await interaction.response.send_message("❌ Această ofertă nu mai este activă.", ephemeral=True)
            return

        freelancer = interaction.guild.get_member(quote["freelancer_id"])
        if not freelancer:
            await interaction.response.send_message("❌ Nu am găsit acel freelancer.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔁 Contraofertă de la client",
            description=f"Clientul a propus **${self.amount}**"
                        + (f" cu deadline **{self.deadline}**" if self.deadline.value else ""),
            color=discord.Color.orange(),
        )
        if self.message.value:
            embed.add_field(name="Mesaj:", value=f"```{self.message}```", inline=False)
        embed.set_footer(text=f"Legat de oferta ta în {interaction.guild.name}.")
        embed.timestamp = interaction.created_at

        # Sent as a DM, privately - other freelancers competing for the same
        # project must never see the amount being negotiated.
        try:
            await freelancer.send(
                embed=embed,
                view=CounterofferResponseView(quote_id=quote["id"], amount=str(self.amount)),
            )
            await interaction.response.send_message(
                "✅ Contraoferta ta a fost trimisă freelancerului, privat.", ephemeral=True
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Nu am putut trimite contraoferta (freelancerul are DM-urile închise).", ephemeral=True
            )


class CounterofferResponseView(discord.ui.View):
    def __init__(self, quote_id: int, amount: str):
        super().__init__(timeout=None)
        self.quote_id = quote_id
        self.amount = amount
        self.accept_btn.label = f"Accept ${amount}"

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        traceback.print_exc()
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)
        else:
            await interaction.followup.send("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        quote = await get_quote(self.quote_id)
        if not quote or quote["freelancer_id"] != interaction.user.id:
            await interaction.response.send_message("❌ Doar freelancerul vizat poate răspunde.", ephemeral=True)
            return
        if quote["status"] != "pending":
            await interaction.response.send_message("❌ Această contraofertă nu mai este activă.", ephemeral=True)
            return

        ticket = await get_ticket_by_id(quote["ticket_id"])
        if not ticket or ticket["status"] != "open":
            await interaction.response.send_message("❌ Acest ticket nu mai este activ.", ephemeral=True)
            return

        # This view is used from a DM, so interaction.guild is None here -
        # fetch the customer channel (and its guild) via the bot client.
        customer_channel = interaction.client.get_channel(ticket["customer_channel_id"])
        if not customer_channel:
            await interaction.response.send_message("❌ Nu am putut găsi canalul clientului.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        # The client proposed this exact amount, so the freelancer accepting
        # it here is the final agreement - no separate client click needed.
        # Update the quote's amount before finalizing so finalize_quote_acceptance
        # (which trusts quote["amount"]) records and displays the right figure.
        async with get_db() as db:
            await db.execute("UPDATE quotes SET amount = ? WHERE id = ?", (self.amount, quote["id"]))
            await db.commit()
        quote["amount"] = self.amount

        await finalize_quote_acceptance(interaction.client, customer_channel.guild, ticket, quote)

        await interaction.followup.send(
            f"✅ Ai acceptat contraoferta de **${self.amount}**. Proiectul ți-a fost atribuit - poți discuta "
            f"direct cu clientul în {customer_channel.mention} de acum înainte."
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        quote = await get_quote(self.quote_id)
        if not quote or quote["freelancer_id"] != interaction.user.id:
            await interaction.response.send_message("❌ Doar freelancerul vizat poate răspunde.", ephemeral=True)
            return
        if quote["status"] != "pending":
            await interaction.response.send_message("❌ Această contraofertă nu mai este activă.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        await interaction.followup.send("❌ Ai refuzat contraoferta. Oferta ta inițială rămâne valabilă.")

        ticket = await get_ticket_by_id(quote["ticket_id"])
        if ticket:
            customer_channel = interaction.client.get_channel(ticket["customer_channel_id"])
            if customer_channel:
                await customer_channel.send(
                    f"❌ <@{quote['freelancer_id']}> a refuzat contraoferta ta. "
                    f"Oferta inițială de **${quote['amount']}** rămâne valabilă dacă vrei să o accepți."
                )


class MessageModal(discord.ui.Modal, title="Trimite un mesaj"):
    message = discord.ui.TextInput(
        label="Mesajul tău",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, freelancer_id: int):
        super().__init__()
        self.freelancer_id = freelancer_id

    async def on_submit(self, interaction: discord.Interaction):
        freelancer = interaction.guild.get_member(self.freelancer_id)
        if not freelancer:
            await interaction.response.send_message("❌ Nu am găsit acel freelancer.", ephemeral=True)
            return

        embed = discord.Embed(
            title="✉️ Mesaj privat de la client",
            description=str(self.message),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Legat de oferta ta în {interaction.guild.name}.")
        embed.timestamp = interaction.created_at

        try:
            await freelancer.send(embed=embed)
            await interaction.response.send_message("✅ Mesajul tău a fost trimis freelancerului.", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Nu am putut trimite mesajul (freelancerul are DM-urile închise).", ephemeral=True
            )


# ---------------------------------------------------------------------------
# DENY REASONS DROPDOWN (freelancer team rejects the whole project)
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
        freelancer_role = interaction.guild.get_role(FREELANCER_ROLE_ID)
        if freelancer_role not in interaction.user.roles:
            await interaction.response.send_message("❌ Only freelancers can select the deny reason.", ephemeral=True)
            return

        ticket = await get_ticket_by_freelancer_channel(interaction.channel.id)
        if not ticket:
            await interaction.response.send_message("❌ Ticketul nu a fost găsit.", ephemeral=True)
            return

        reason = self.values[0]
        await interaction.response.send_message(f"🔒 Ticket denied. Reason: **{reason}**. Archiving...", ephemeral=True)

        async with get_db() as db:
            await db.execute("UPDATE tickets SET status = 'denied' WHERE rowid = ?", (ticket["id"],))
            await db.commit()

        embed_reason = discord.Embed(
            title="Ticket Denied",
            description=f"This ticket has been rejected by the freelancers.\n**Reason:** {reason}",
            color=discord.Color.red(),
        )
        await interaction.channel.send(embed=embed_reason)
        await archive_freelancer_channel(interaction.channel)

        customer_channel = interaction.guild.get_channel(ticket["customer_channel_id"])
        if customer_channel:
            customer_embed = discord.Embed(
                title="Ticket închis",
                description="Din păcate niciun freelancer nu poate prelua acest proiect momentan.\n"
                            f"**Motiv:** {reason}",
                color=discord.Color.red(),
            )
            await customer_channel.send(embed=customer_embed)
            await archive_channel(customer_channel)


class DenyReasonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(DenyReasonSelect())

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        traceback.print_exc()
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)
        else:
            await interaction.followup.send("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)


# ---------------------------------------------------------------------------
# VIEWS FOR PANELS
# ---------------------------------------------------------------------------

class NewTicketActionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        traceback.print_exc()
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)
        else:
            await interaction.followup.send("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)

    @discord.ui.button(label="Quote", style=discord.ButtonStyle.success, custom_id="mythral_action_quote")
    async def quote_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        freelancer_role = interaction.guild.get_role(FREELANCER_ROLE_ID)
        if freelancer_role not in interaction.user.roles:
            await interaction.response.send_message("❌ Only freelancers can submit a quote for this ticket.", ephemeral=True)
            return

        ticket = await get_ticket_by_freelancer_channel(interaction.channel.id)
        if not ticket or ticket["status"] != "open":
            await interaction.response.send_message(
                "❌ Acest proiect nu mai este disponibil pentru oferte.", ephemeral=True
            )
            return

        await interaction.response.send_modal(QuotePriceModal())

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="mythral_action_deny")
    async def deny_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        freelancer_role = interaction.guild.get_role(FREELANCER_ROLE_ID)
        if freelancer_role not in interaction.user.roles:
            await interaction.response.send_message("❌ Only freelancers can use the Deny button for this ticket.", ephemeral=True)
            return

        embed = discord.Embed(
            title="What went wrong?",
            description="Select a reason that explains your reason for denying the ticket.",
            color=discord.Color.green(),
        )
        embed.set_footer(text=config.STUDIO_FOOTER)
        embed.timestamp = interaction.created_at

        await interaction.response.send_message(embed=embed, view=DenyReasonView(), ephemeral=True)

    @discord.ui.button(label="Reviews", style=discord.ButtonStyle.primary, custom_id="mythral_action_reviews")
    async def reviews_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await get_ticket_by_freelancer_channel(interaction.channel.id)
        if not ticket:
            await interaction.response.send_message("❌ Ticketul nu a fost găsit.", ephemeral=True)
            return

        client_id = ticket["owner_id"]
        avg, count = await get_client_rating(client_id)
        embed = discord.Embed(
            title="⭐ Client Reviews",
            description=f"<@{client_id}> has an average rating of **{_stars(avg)} ({avg}/5)** across **{count}** review(s).",
            color=discord.Color.gold(),
        )
        if count:
            reviews = await get_client_reviews(client_id, limit=5)
            for rating, comment, freelancer_id, created_at in reviews:
                embed.add_field(
                    name=_stars(rating),
                    value=comment or "*No comment left.*",
                    inline=False,
                )
        embed.set_footer(text=config.STUDIO_FOOTER)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Get a quote", style=discord.ButtonStyle.success, emoji="💵", custom_id="mythral_ticket_quote")
    async def quote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Sent immediately, with no DB call in between - a modal must be the
        # very first response to an interaction, and even a small delay here
        # (DB latency, etc.) can make Discord expire the interaction before
        # we get to respond. The "already have a ticket" check now happens
        # in the modal's on_submit instead, which has its own fresh timer.
        await interaction.response.send_modal(QuoteModal())

    @discord.ui.button(label="Apply for freelancer", style=discord.ButtonStyle.primary, emoji="💼", custom_id="mythral_ticket_apply")
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApplyModal())

    @discord.ui.button(label="General Support", style=discord.ButtonStyle.secondary, emoji="🎧", custom_id="mythral_ticket_support")
    async def support_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SupportModal())


# ---------------------------------------------------------------------------
# SHARED LOGIC - lookups
# ---------------------------------------------------------------------------

async def get_open_ticket(guild: discord.Guild, user_id: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT rowid AS id, COALESCE(customer_channel_id, channel_id) AS chan FROM tickets "
            "WHERE owner_id = ? AND status NOT IN ('denied', 'closed') "
            "ORDER BY rowid DESC LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        ticket_id, channel_id = row
        if channel_id and guild.get_channel(channel_id):
            return channel_id

        # The channel no longer exists (deleted manually, etc.) - don't keep
        # blocking the user with a ticket that points nowhere.
        await db.execute("UPDATE tickets SET status = 'closed' WHERE rowid = ?", (ticket_id,))
        await db.commit()
        return None


async def _row_to_dict(cursor, row):
    if row is None:
        return None
    columns = [d[0] for d in cursor.description]
    return dict(zip(columns, row))


async def get_ticket_by_id(ticket_id: int):
    async with get_db() as db:
        cursor = await db.execute("SELECT rowid AS id, * FROM tickets WHERE rowid = ?", (ticket_id,))
        row = await cursor.fetchone()
        return await _row_to_dict(cursor, row)


async def get_ticket_by_customer_channel(channel_id: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT rowid AS id, * FROM tickets WHERE COALESCE(customer_channel_id, channel_id) = ?",
            (channel_id,),
        )
        row = await cursor.fetchone()
        return await _row_to_dict(cursor, row)


async def get_ticket_by_freelancer_channel(channel_id: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT rowid AS id, * FROM tickets WHERE freelancer_channel_id = ?", (channel_id,)
        )
        row = await cursor.fetchone()
        return await _row_to_dict(cursor, row)


async def get_quote(quote_id: int):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,))
        row = await cursor.fetchone()
        return await _row_to_dict(cursor, row)


async def get_pending_quote(ticket_id: int, freelancer_id: int):
    """Returns the id of a freelancer's already-pending quote for this
    ticket, if any - used to stop one freelancer from stacking duplicate
    offers, without blocking other freelancers from quoting."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM quotes WHERE ticket_id = ? AND freelancer_id = ? AND status = 'pending'",
            (ticket_id, freelancer_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def archive_channel(channel: discord.TextChannel):
    await channel.set_permissions(channel.guild.default_role, view_channel=False)
    archive_category = channel.guild.get_channel(ARCHIVE_CATEGORY_ID)
    if archive_category:
        await channel.edit(category=archive_category, name=f"closed-{channel.name[-4:]}")


async def archive_freelancer_channel(channel: discord.TextChannel):
    """Archives a freelancer discussion channel: strips the blanket
    freelancer-role access (so freelancers who didn't win the project can no
    longer see it, even though @everyone was already denied) and moves it to
    the archive category."""
    freelancer_role = channel.guild.get_role(FREELANCER_ROLE_ID)
    if freelancer_role:
        try:
            await channel.set_permissions(freelancer_role, overwrite=None)
        except discord.HTTPException:
            pass
    await archive_channel(channel)


# ---------------------------------------------------------------------------
# CLIENT REVIEWS - freelancers rate the client after a ticket is closed. The
# request is sent by DM (mirrors the client-facing "Reviews" button on the
# request card, which shows what other freelancers said about this client).
# ---------------------------------------------------------------------------

class ReviewClientModal(discord.ui.Modal, title="Review Client"):
    rating = discord.ui.TextInput(
        label="Rating (1-5)",
        placeholder="e.g., 5",
        max_length=1,
    )
    comment = discord.ui.TextInput(
        label="Comment (optional)",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
    )

    def __init__(self, ticket_id: int, client_id: int, freelancer_id: int):
        super().__init__()
        self.ticket_id = ticket_id
        self.client_id = client_id
        self.freelancer_id = freelancer_id

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exc()
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ A apărut o eroare la trimiterea review-ului.", ephemeral=True)
        else:
            await interaction.followup.send("❌ A apărut o eroare la trimiterea review-ului.", ephemeral=True)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.rating).strip()
        if not raw.isdigit() or not (1 <= int(raw) <= 5):
            await interaction.response.send_message("❌ Rating-ul trebuie să fie un număr între 1 și 5.", ephemeral=True)
            return

        await add_client_review(
            self.ticket_id, self.client_id, self.freelancer_id,
            int(raw), str(self.comment) if self.comment.value else None,
        )
        await interaction.response.send_message("✅ Mulțumim pentru review! Este vizibil doar freelancerilor.", ephemeral=True)
        for child in self.review_view.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self.review_view)
        except (discord.HTTPException, AttributeError):
            pass


class ReviewClientView(discord.ui.View):
    """Not persistent across restarts (the ticket/client/freelancer ids are
    baked into the closure, same trade-off as IncomingQuoteView above) - if
    the bot restarts before a freelancer reviews, re-trigger /close-ticket's
    DM manually to get a working button again."""

    def __init__(self, ticket_id: int, client_id: int, freelancer_id: int):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.client_id = client_id
        self.freelancer_id = freelancer_id

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        traceback.print_exc()
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)
        else:
            await interaction.followup.send("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)

    @discord.ui.button(label="Review", style=discord.ButtonStyle.success)
    async def review_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await has_reviewed(self.ticket_id, self.freelancer_id):
            await interaction.response.send_message("✅ Ai lăsat deja un review pentru acest client.", ephemeral=True)
            return
        modal = ReviewClientModal(self.ticket_id, self.client_id, self.freelancer_id)
        modal.review_view = self
        await interaction.response.send_modal(modal)


async def send_client_review_request(bot: discord.Client, ticket: dict, client: discord.abc.User):
    """DMs the freelancer who worked the ticket, asking them to rate the client."""
    freelancer_id = ticket.get("assigned_freelancer_id")
    if not freelancer_id:
        return
    freelancer = bot.get_user(freelancer_id) or await bot.fetch_user(freelancer_id)
    if not freelancer:
        return

    embed = discord.Embed(
        title="Review Client",
        description=(
            "We care about our freelancers and we want to know how you found working with "
            f"`{client.display_name if hasattr(client, 'display_name') else client.name}`?\n"
            "Use the button below to describe your experience.\n"
            "The review will only be visible to freelancers."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=config.STUDIO_FOOTER)
    embed.timestamp = discord.utils.utcnow()

    try:
        await freelancer.send(embed=embed, view=ReviewClientView(ticket["id"], client.id, freelancer_id))
    except discord.HTTPException:
        log.warning("Could not DM freelancer %s a review request for ticket %s.", freelancer_id, ticket["id"])


# ---------------------------------------------------------------------------
# FREELANCER REVIEWS - the client rates the freelancer. Unlike client
# reviews (sent by DM), this one is requested in-channel: the freelancer
# runs /review before the ticket is closed, which posts a panel the client
# clicks to open the form.
# ---------------------------------------------------------------------------

class FreelancerReviewModal(discord.ui.Modal, title="Lasă o recenzie"):
    service = discord.ui.TextInput(
        label="Serviciu oferit",
        placeholder="ex: Builder, Web Developer...",
        max_length=100,
    )
    rating = discord.ui.TextInput(
        label="Rating (1-5)",
        placeholder="5",
        max_length=1,
    )
    comment = discord.ui.TextInput(
        label="Comentariu",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, ticket_id: int, freelancer_id: int, client_id: int):
        super().__init__()
        self.ticket_id = ticket_id
        self.freelancer_id = freelancer_id
        self.client_id = client_id

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exc()
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ A apărut o eroare la trimiterea recenziei.", ephemeral=True)
        else:
            await interaction.followup.send("❌ A apărut o eroare la trimiterea recenziei.", ephemeral=True)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.rating).strip()
        if not raw.isdigit() or not (1 <= int(raw) <= 5):
            await interaction.response.send_message("❌ Rating-ul trebuie să fie un număr între 1 și 5.", ephemeral=True)
            return

        rating_val = int(raw)
        await add_freelancer_review(
            self.ticket_id, self.freelancer_id, self.client_id,
            str(self.service), rating_val, str(self.comment) if self.comment.value else None,
        )

        # Post a public copy in the studio's #reviews channel, same as the
        # old standalone /review command used to do before it was merged
        # into this ticket-based flow.
        reviews_channel_id = getattr(config, "REVIEWS_CHANNEL_ID", None)
        reviews_channel = interaction.guild.get_channel(reviews_channel_id) if reviews_channel_id else None
        if reviews_channel:
            freelancer_member = interaction.guild.get_member(self.freelancer_id)
            review_embed = discord.Embed(
                title=f"⭐ New review from {interaction.user.display_name}",
                color=getattr(config, "COLOR_GOLD", config.COLOR_MAIN),
            )
            review_embed.add_field(name="Service Provided", value=str(self.service), inline=False)
            review_embed.add_field(
                name="Freelancer",
                value=freelancer_member.display_name if freelancer_member else f"<@{self.freelancer_id}>",
                inline=False,
            )
            review_embed.add_field(name="Rating", value=_stars(rating_val) + f" ({rating_val}/5)", inline=False)
            if self.comment.value:
                review_embed.add_field(name="Comment", value=f"```{self.comment}```", inline=False)
            review_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            review_embed.set_footer(text=config.STUDIO_FOOTER)
            try:
                await reviews_channel.send(embed=review_embed)
            except discord.HTTPException:
                log.warning("Could not post review to #reviews channel for ticket %s.", self.ticket_id)
        else:
            log.warning(
                "config.REVIEWS_CHANNEL_ID is not set (or channel not found) - "
                "review for ticket %s was saved but not posted publicly.", self.ticket_id
            )

        await interaction.response.send_message("✅ Mulțumim pentru recenzie!", ephemeral=True)
        for child in self.review_view.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self.review_view)
        except (discord.HTTPException, AttributeError):
            pass


class FreelancerReviewView(discord.ui.View):
    """Not persistent across restarts, same trade-off documented on
    IncomingQuoteView/ReviewClientView above - if the bot restarts before the
    client reviews, the freelancer can just run /review again."""

    def __init__(self, ticket_id: int, freelancer_id: int, client_id: int):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.freelancer_id = freelancer_id
        self.client_id = client_id

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        traceback.print_exc()
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)
        else:
            await interaction.followup.send("❌ A apărut o eroare. Verifică log-ul botului.", ephemeral=True)

    @discord.ui.button(label="Review", style=discord.ButtonStyle.success)
    async def review_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.client_id:
            await interaction.response.send_message("❌ Doar clientul acestui proiect poate lăsa o recenzie.", ephemeral=True)
            return
        if await has_reviewed_freelancer(self.ticket_id, self.client_id):
            await interaction.response.send_message("✅ Ai lăsat deja o recenzie pentru acest proiect.", ephemeral=True)
            return
        modal = FreelancerReviewModal(self.ticket_id, self.freelancer_id, self.client_id)
        modal.review_view = self
        await interaction.response.send_modal(modal)


async def finalize_quote_acceptance(bot: discord.Client, guild: discord.Guild, ticket: dict, quote: dict):
    """Shared finalization for a quote being accepted - whether the client
    hit Accept directly on the quote card, or a freelancer accepted the
    client's counteroffer from DM. Either way the deal is done: the ticket
    and quote are marked accepted, every other pending quote for this ticket
    is expired and greyed out, the winning freelancer is added into the
    customer channel, and the freelancer negotiation channel is archived.

    `quote["amount"]` is trusted as the final agreed amount, so callers must
    make sure it already reflects any counteroffer before calling this.

    Returns (customer_channel, assigned_member) so the caller can still send
    its own confirmation message.
    """
    async with get_db() as db:
        await db.execute("UPDATE quotes SET status = 'accepted' WHERE id = ?", (quote["id"],))
        await db.execute(
            "UPDATE tickets SET status = 'accepted', assigned_freelancer_id = ?, "
            "quoted_amount = ?, quoted_deadline = ? WHERE rowid = ?",
            (quote["freelancer_id"], quote["amount"], quote["deadline"], ticket["id"]),
        )
        await db.execute(
            "UPDATE quotes SET status = 'expired' WHERE ticket_id = ? AND id != ? AND status = 'pending'",
            (ticket["id"], quote["id"]),
        )
        cursor = await db.execute(
            "SELECT id, message_id, freelancer_id FROM quotes WHERE ticket_id = ? AND id != ? AND status = 'expired'",
            (ticket["id"], quote["id"]),
        )
        other_quotes = await cursor.fetchall()
        await db.commit()

    customer_channel = guild.get_channel(ticket["customer_channel_id"])
    assigned_member = guild.get_member(quote["freelancer_id"])

    if customer_channel:
        # Mark the winning quote's own card as accepted. Needed even on the
        # path where the client already clicked Accept on this exact card
        # (harmless re-edit), and essential on the counteroffer-from-DM path
        # where this card was never touched by the accepting interaction.
        if quote.get("message_id"):
            try:
                msg = await customer_channel.fetch_message(quote["message_id"])
                embed = msg.embeds[0] if msg.embeds else None
                if embed:
                    if assigned_member:
                        embed.description = f"{assigned_member.mention} has quoted **${quote['amount']}**"
                    embed.color = discord.Color.green()
                    embed.add_field(name="Status", value="✅ Ofertă acceptată.", inline=False)
                view = discord.ui.View.from_message(msg)
                for child in view.children:
                    child.disabled = True
                await msg.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass

        # Grey out and disable every other pending quote card - the project
        # is taken.
        for oq_id, oq_message_id, oq_freelancer_id in other_quotes:
            if not oq_message_id:
                continue
            try:
                msg = await customer_channel.fetch_message(oq_message_id)
                view = discord.ui.View.from_message(msg)
                for child in view.children:
                    child.disabled = True
                embed = msg.embeds[0] if msg.embeds else None
                if embed:
                    embed.color = discord.Color.greyple()
                    embed.add_field(name="Status", value="Proiectul a fost atribuit altui freelancer.", inline=False)
                await msg.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass
            oq_member = guild.get_member(oq_freelancer_id)
            if oq_member:
                try:
                    await oq_member.send(
                        f"Clientul a ales o altă ofertă pentru ticketul din {guild.name}. Mulțumim oricum!"
                    )
                except discord.HTTPException:
                    pass

        # Transfer the winning freelancer directly into the client's ticket -
        # they now share this channel and talk to each other with no relay.
        if assigned_member:
            await customer_channel.set_permissions(
                assigned_member, view_channel=True, send_messages=True, attach_files=True
            )
        reveal_embed = discord.Embed(
            title="🤝 Proiect asignat",
            description=(
                f"{assigned_member.mention if assigned_member else 'Freelancerul'} va lucra la acest proiect. "
                "Puteți discuta direct aici de acum înainte."
            ),
            color=discord.Color.green(),
        )
        await customer_channel.send(embed=reveal_embed)

    freelancer_channel = guild.get_channel(ticket["freelancer_channel_id"])
    if freelancer_channel:
        await freelancer_channel.send(
            f"✅ Clientul a ales oferta lui "
            f"{assigned_member.mention if assigned_member else '<@' + str(quote['freelancer_id']) + '>'}. "
            "Acest canal se arhivează."
        )
        await archive_freelancer_channel(freelancer_channel)

    return customer_channel, assigned_member


# ---------------------------------------------------------------------------
# TICKET CREATION
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TICKET WELCOME MESSAGE - a mention-preference panel followed by a short
# orientation message, sent to the client the moment their ticket channel is
# created.
# ---------------------------------------------------------------------------

class MentionPreferenceView(discord.ui.View):
    """Lets the client toggle whether they get pinged on incoming freelancer
    messages/quotes. Persistent (custom_id-based) so the buttons keep working
    across restarts. Note: the *label* shown right after a bot restart may be
    stale until someone clicks a button, since old panels aren't re-rendered
    on boot - only the button behaviour is guaranteed to stay correct."""

    def __init__(self, pinged: bool = True):
        super().__init__(timeout=None)
        self._sync(pinged)

    def _sync(self, pinged: bool):
        self.enable_btn.label = "Already Enabled" if pinged else "Enable"
        self.enable_btn.disabled = pinged
        self.disable_btn.label = "Disable" if pinged else "Already Disabled"
        self.disable_btn.disabled = not pinged

    @discord.ui.button(label="Already Enabled", style=discord.ButtonStyle.success, custom_id="mention_pref_enable")
    async def enable_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_ping_pref(interaction.user.id, True)
        self._sync(True)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.danger, custom_id="mention_pref_disable")
    async def disable_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_ping_pref(interaction.user.id, False)
        self._sync(False)
        await interaction.response.edit_message(view=self)


class DismissWelcomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.danger, custom_id="ticket_welcome_dismiss")
    async def dismiss_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass


async def send_ticket_welcome(channel: discord.TextChannel, member: discord.Member):
    """Posted once, right after a ticket channel is created for `member`."""
    pinged = await get_ping_pref(member.id)

    pref_embed = discord.Embed(
        description=(
            "By using the buttons below, you can control whether you'd want to be mentioned "
            "every time a freelancer sends you a message or a quote.\n"
            f"Currently you {'**will be pinged**' if pinged else '**will not be pinged**'}."
        ),
        color=config.COLOR_MAIN,
    )
    pref_embed.set_footer(text=config.STUDIO_FOOTER)
    pref_embed.timestamp = discord.utils.utcnow()
    await channel.send(embed=pref_embed, view=MentionPreferenceView(pinged=pinged))

    general_channel = discord.utils.get(channel.guild.text_channels, name=GENERAL_CHANNEL_NAME)
    general_mention = general_channel.mention if general_channel else f"#{GENERAL_CHANNEL_NAME}"

    welcome_embed = discord.Embed(
        description=(
            f"Hello, {member.mention}!\n"
            f"**Thank you for choosing {STUDIO_NAME}.**\n\n"
            "It looks like this is your first commission made here.\n"
            f"- If you are unsure on how our server works, or want to have a look around, "
            f"check out {general_mention}.\n"
            f"- It is also important to mention that we have a <#{TOS_CHANNEL_ID}> you should "
            "follow during the commission process.\n"
            f"- If you need any assistance, do not hesitate to ping any of our "
            f"**{EXECUTIVE_TEAM_LABEL}**."
        ),
        color=config.COLOR_MAIN,
    )
    welcome_embed.set_footer(text=config.STUDIO_FOOTER)
    welcome_embed.timestamp = discord.utils.utcnow()
    await channel.send(embed=welcome_embed, view=DismissWelcomeView())


async def create_quote_ticket(interaction: discord.Interaction, fields: list[tuple[str, str]]):
    """Creates two separate channels (customer / freelancer) so neither side
    can see or interact with the other until a quote is accepted.

    Everything after channel creation is wrapped in a try/except: if any
    step in here throws, the channels would otherwise sit there fully empty
    forever (no info embed, no ticket row committed on some paths) and the
    interaction itself dies silently with "This interaction failed" and no
    error shown to the client. Instead we now log the real cause, delete the
    orphaned channels so they don't pile up, and tell the person to retry.
    """
    guild = interaction.guild
    # Defensive - guarantees the tables this function touches (tickets,
    # reviews, etc.) exist even if on_ready hasn't finished running yet.
    await ensure_schema()

    ticket_category = guild.get_channel(config.TICKET_CATEGORY_ID)
    freelancer_category = guild.get_channel(FREELANCER_CATEGORY_ID)
    suffix = str(interaction.id)[-4:]

    staff_role = guild.get_role(config.STAFF_ROLE_ID)
    freelancer_role = guild.get_role(FREELANCER_ROLE_ID)

    customer_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff_role:
        customer_overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    freelancer_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff_role:
        freelancer_overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    if freelancer_role:
        freelancer_overwrites[freelancer_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    customer_channel = await guild.create_text_channel(
        name=f"quote-{suffix}",
        category=ticket_category,
        overwrites=customer_overwrites,
    )
    freelancer_channel = await guild.create_text_channel(
        name=f"offer-{suffix}",
        category=freelancer_category,
        overwrites=freelancer_overwrites,
    )

    try:
        async with get_db() as db:
            cursor = await db.execute(
                "INSERT INTO tickets (ticket_type, owner_id, customer_channel_id, freelancer_channel_id, status) "
                "VALUES ('quote', ?, ?, ?, 'open')",
                (interaction.user.id, customer_channel.id, freelancer_channel.id),
            )
            await db.commit()
            ticket_id = cursor.lastrowid

        # Embed sent to the freelancer channel, WITH full project details
        freelancer_embed = discord.Embed(title="Information", color=discord.Color.green())
        for name, value in fields:
            freelancer_embed.add_field(name=name, value=value or "—", inline=False)
        client_avg, client_review_count = await get_client_rating(interaction.user.id)
        freelancer_embed.add_field(name="Rating", value=f"{_stars(client_avg)} ({client_review_count})", inline=False)
        freelancer_embed.set_footer(text=config.STUDIO_FOOTER)
        freelancer_embed.timestamp = interaction.created_at

        ping = f"New quote request for {freelancer_role.mention}." if freelancer_role else "New quote request received."
        await freelancer_channel.send(content=ping, embed=freelancer_embed, view=NewTicketActionsView())

        # Any freelancer with the role can post here to chat with the client and
        # form an opinion on the project before quoting - no first-reply claim,
        # everyone stays able to talk until a quote is accepted.
        chat_embed = discord.Embed(
            title="💬 Discuție cu clientul",
            description="Dă **reply la acest mesaj** oricând pentru a discuta cu clientul și a-ți face o idee "
                         "despre proiect. Clientul va vedea numele și poza ta de profil la mesajele trimise astfel. "
                         "Orice altceva scrii în canal (care nu e reply la acest mesaj) rămâne doar între freelanceri "
                         "și nu ajunge la client. Când ești pregătit, trimite o ofertă cu butonul **Quote** de mai sus.",
            color=discord.Color.blurple(),
        )
        chat_prompt_msg = await freelancer_channel.send(embed=chat_embed)
        async with get_db() as db:
            await db.execute(
                "UPDATE tickets SET chat_prompt_message_id = ? WHERE rowid = ?",
                (chat_prompt_msg.id, ticket_id),
            )
            await db.commit()

        # Embed sent to the customer channel
        customer_embed = discord.Embed(
            title="✅ Cererea ta a fost trimisă",
            description="Freelancerii din echipa noastră pot analiza cererea și te pot contacta direct aici "
                         "pentru a discuta detalii și a-ți trimite oferte.",
            color=discord.Color.green(),
        )
        customer_embed.set_footer(text=config.STUDIO_FOOTER)
        customer_embed.timestamp = interaction.created_at
        await customer_channel.send(embed=customer_embed)

        # Explains the same chat mechanic from the client's side - unlike the
        # freelancer prompt above, no "reply to this message" is needed here:
        # every message the client sends in this channel is relayed as-is.
        customer_chat_embed = discord.Embed(
            title="💬 Discuție cu freelancerii",
            description="Poți scrie orice mesaj în acest canal pentru a discuta cu freelancerii care au acces la "
                         "proiectul tău - nu trebuie să dai reply, orice trimiți aici ajunge la ei. Când un "
                         "freelancer îți răspunde, vei vedea numele și poza lui de profil.",
            color=discord.Color.blurple(),
        )
        await customer_channel.send(embed=customer_chat_embed)

        await send_ticket_welcome(customer_channel, interaction.user)

        await interaction.response.send_message(f"✅ Your ticket has been created: {customer_channel.mention}", ephemeral=True)

    except Exception:
        traceback.print_exc()
        log.error("create_quote_ticket failed after channels were created (quote-%s/offer-%s) - deleting them.", suffix, suffix)
        for ch in (customer_channel, freelancer_channel):
            try:
                await ch.delete(reason="Ticket creation failed, cleaning up empty channel")
            except discord.HTTPException:
                pass
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ A apărut o eroare la crearea ticketului. Te rugăm încearcă din nou sau contactează staff-ul.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ A apărut o eroare la crearea ticketului. Te rugăm încearcă din nou sau contactează staff-ul.",
                ephemeral=True,
            )


async def create_simple_ticket(interaction: discord.Interaction, ticket_type: str, fields: list[tuple[str, str]]):
    """Unchanged single-channel flow, used for apply/support tickets."""
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
        name=f"{ticket_type}-{str(interaction.id)[-4:]}",
        category=category,
        overwrites=overwrites,
    )

    try:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO tickets (ticket_type, owner_id, channel_id, status) VALUES (?, ?, ?, 'open')",
                (ticket_type, interaction.user.id, channel.id),
            )
            await db.commit()

        embed = discord.Embed(title="Information", color=discord.Color.green())
        for name, value in fields:
            embed.add_field(name=name, value=value or "—", inline=False)
        embed.set_footer(text=config.STUDIO_FOOTER)
        embed.timestamp = interaction.created_at

        ping = f"New ticket for {staff_role.mention}." if staff_role else "New ticket received."
        await channel.send(content=ping, embed=embed)

        await send_ticket_welcome(channel, interaction.user)

        await interaction.response.send_message(f"✅ Your ticket has been created: {channel.mention}", ephemeral=True)

    except Exception:
        traceback.print_exc()
        log.error("create_simple_ticket failed after channel %s was created - deleting it.", channel.name)
        try:
            await channel.delete(reason="Ticket creation failed, cleaning up empty channel")
        except discord.HTTPException:
            pass
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ A apărut o eroare la crearea ticketului. Te rugăm încearcă din nou sau contactează staff-ul.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ A apărut o eroare la crearea ticketului. Te rugăm încearcă din nou sau contactează staff-ul.",
                ephemeral=True,
            )


# ---------------------------------------------------------------------------
# GROUP RELAY (pre-acceptance discussion between client and every freelancer
# with access to the offer channel). Freelancer -> client messages show the
# freelancer's name/avatar; client -> freelancer messages show the studio
# logo instead of the client's real avatar/name - the client stays 100%
# anonymous, always.
# ---------------------------------------------------------------------------

async def relay_message(
    destination_channel: discord.TextChannel,
    message: discord.Message,
    label: str,
    icon_url: str | None = None,
    use_logo_icon: bool = False,
    ping_user_id: int | None = None,
):
    """Relays `message` into `destination_channel` as a plain embed.

    icon_url: shows a real per-sender avatar (used for freelancer -> client,
    where the freelancer's identity is meant to be visible).
    use_logo_icon: shows the bundled studio logo instead of any real avatar
    (used for client -> freelancer, so the client stays 100% anonymous - no
    photo, no name, nothing that could identify them).
    ping_user_id: when given, that user is @-mentioned above the embed -
    used for freelancer -> client relays, gated by the client's mention
    preference (see get_ping_pref/MentionPreferenceView).
    """
    embed = discord.Embed(description=message.content or "*[fără text]*", color=discord.Color.blurple())

    files = [await a.to_file() for a in message.attachments] if message.attachments else []

    if use_logo_icon:
        try:
            files.append(discord.File(MYTHRAL_LOGO_PATH, filename=MYTHRAL_LOGO_FILENAME))
            embed.set_author(name=label, icon_url=f"attachment://{MYTHRAL_LOGO_FILENAME}")
        except (FileNotFoundError, OSError):
            # Don't let a missing/misplaced logo asset silently swallow the
            # whole relay - the message still needs to reach the other side,
            # it just won't have the logo icon this time. Deploy
            # assets/mythral_logo.png next to tickets.py to fix the icon.
            log.warning(
                "Mythral logo not found at %s - relaying '%s' without an icon.",
                MYTHRAL_LOGO_PATH, label,
            )
            embed.set_author(name=label)
    elif icon_url:
        embed.set_author(name=label, icon_url=icon_url)
    else:
        embed.set_author(name=label)

    embed.timestamp = message.created_at
    content = f"<@{ping_user_id}>" if ping_user_id else None
    await destination_channel.send(content=content, embed=embed, files=files)

    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        await ensure_schema()
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(NewTicketActionsView())
        self.bot.add_view(MentionPreferenceView())
        self.bot.add_view(DismissWelcomeView())

        # Global command sync (bot.tree.sync() with no guild) can take up to
        # an hour to propagate, which is exactly the "This command is
        # outdated, please try again in a few minutes" message Discord shows
        # for a brand new/changed command. Per-guild sync is instant, so we
        # copy the globally-registered commands into each guild's tree and
        # sync those - safe to run every startup. If your main bot file
        # already does a global sync somewhere, this is redundant but
        # harmless; remove this block if you'd rather manage sync yourself.
        for guild in self.bot.guilds:
            try:
                self.bot.tree.copy_global_to(guild=guild)
                await self.bot.tree.sync(guild=guild)
            except discord.HTTPException:
                log.warning("Could not sync commands for guild %s.", guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        # Freelancer discussion channel: any freelancer can talk to the
        # client to form an opinion on the project. Relayed with the
        # freelancer's own name/avatar attached, so the client can tell
        # freelancers apart and follow a conversation with the same person.
        # Only messages that are a reply to the "Discuție cu clientul" prompt
        # are relayed, so freelancers can still talk among themselves in the
        # same channel without that leaking to the client.
        ticket = await get_ticket_by_freelancer_channel(message.channel.id)
        if ticket:
            if ticket["status"] != "open":
                return  # a freelancer already won this project, channel is being archived

            is_reply_to_prompt = (
                message.reference is not None
                and ticket["chat_prompt_message_id"] is not None
                and message.reference.message_id == ticket["chat_prompt_message_id"]
            )
            if not is_reply_to_prompt:
                return  # internal freelancer chatter, not meant for the client

            freelancer_role = message.guild.get_role(FREELANCER_ROLE_ID)
            if freelancer_role and freelancer_role in message.author.roles:
                customer_channel = message.guild.get_channel(ticket["customer_channel_id"])
                if customer_channel:
                    wants_ping = await get_ping_pref(ticket["owner_id"])
                    await relay_message(
                        customer_channel, message,
                        label=f"💬 {message.author.display_name}",
                        icon_url=message.author.display_avatar.url,
                        ping_user_id=ticket["owner_id"] if wants_ping else None,
                    )
            return

        # Customer channel: the client talking back to the whole freelancer
        # group. Once a quote is accepted the winning freelancer shares this
        # very channel, so nothing needs relaying anymore.
        ticket = await get_ticket_by_customer_channel(message.channel.id)
        if ticket and ticket["status"] == "open" and message.author.id == ticket["owner_id"]:
            freelancer_channel = message.guild.get_channel(ticket["freelancer_channel_id"])
            if freelancer_channel:
                await relay_message(
                    freelancer_channel, message,
                    label="💬 Client",
                    use_logo_icon=True,
                )
            return

    @app_commands.command(name="review", description="Cere clientului să lase o recenzie pentru tine, înainte de închiderea ticketului")
    async def review(self, interaction: discord.Interaction):
        ticket = await get_ticket_by_customer_channel(interaction.channel.id)
        if not ticket:
            await interaction.response.send_message("❌ Comanda se folosește în canalul unui client.", ephemeral=True)
            return
        if ticket["status"] != "accepted":
            await interaction.response.send_message(
                "❌ Comanda funcționează doar pe un proiect activ (ofertă acceptată, ticket încă deschis).",
                ephemeral=True,
            )
            return
        if interaction.user.id != ticket.get("assigned_freelancer_id"):
            await interaction.response.send_message(
                "❌ Doar freelancerul asignat acestui proiect poate folosi /review.", ephemeral=True
            )
            return
        if await has_reviewed_freelancer(ticket["id"], ticket["owner_id"]):
            await interaction.response.send_message("✅ Clientul a lăsat deja o recenzie pentru acest proiect.", ephemeral=True)
            return

        embed = discord.Embed(
            title="⭐ Lasă o recenzie",
            description=f"Cum a fost experiența ta lucrând cu {interaction.user.mention}? "
                         "Apasă butonul de mai jos pentru a lăsa o recenzie.",
            color=config.COLOR_MAIN,
        )
        embed.set_footer(text=config.STUDIO_FOOTER)
        await interaction.response.send_message(
            embed=embed,
            view=FreelancerReviewView(ticket["id"], interaction.user.id, ticket["owner_id"]),
        )

    @app_commands.command(name="close-ticket", description="Închide un ticket finalizat și cere freelancerului un review despre client")
    async def close_ticket(self, interaction: discord.Interaction):
        ticket = await get_ticket_by_customer_channel(interaction.channel.id)
        if not ticket:
            await interaction.response.send_message("❌ Acest canal nu este un ticket de client.", ephemeral=True)
            return
        if ticket["status"] != "accepted":
            await interaction.response.send_message(
                "❌ Doar un ticket cu o ofertă acceptată poate fi închis astfel.", ephemeral=True
            )
            return

        staff_role = interaction.guild.get_role(config.STAFF_ROLE_ID)
        is_staff = staff_role and staff_role in interaction.user.roles
        is_assigned_freelancer = interaction.user.id == ticket.get("assigned_freelancer_id")
        if not (is_staff or is_assigned_freelancer):
            await interaction.response.send_message(
                "❌ Doar staff-ul sau freelancerul asignat poate închide acest ticket.", ephemeral=True
            )
            return

        await interaction.response.send_message("🔒 Se închide ticketul...", ephemeral=True)

        try:
            async with get_db() as db:
                await db.execute("UPDATE tickets SET status = 'closed' WHERE rowid = ?", (ticket["id"],))
                await db.commit()

            client = interaction.guild.get_member(ticket["owner_id"]) or await self.bot.fetch_user(ticket["owner_id"])
            if client:
                await send_client_review_request(self.bot, ticket, client)

            closed_embed = discord.Embed(
                title="Ticket închis",
                description="Acest proiect a fost marcat ca finalizat. Mulțumim pentru colaborare!",
                color=discord.Color.green(),
            )
            closed_embed.set_footer(text=config.STUDIO_FOOTER)
            await interaction.channel.send(embed=closed_embed)
            await archive_channel(interaction.channel)
        except Exception:
            traceback.print_exc()
            log.error("close_ticket failed for ticket %s after status was already marked closed.", ticket["id"])
            await interaction.followup.send(
                "⚠️ Ticketul a fost marcat ca închis, dar a apărut o eroare la trimiterea review-ului sau "
                "la arhivare. Verifică log-ul botului.",
                ephemeral=True,
            )

    @app_commands.command(name="freelancer-profile", description="Setează sau editează profilul tău de freelancer")
    async def freelancer_profile(self, interaction: discord.Interaction):
        freelancer_role = interaction.guild.get_role(FREELANCER_ROLE_ID)
        if freelancer_role not in interaction.user.roles:
            await interaction.response.send_message("❌ Doar freelancerii pot seta un profil.", ephemeral=True)
            return
        existing = await get_freelancer_profile(interaction.user.id)
        await interaction.response.send_modal(FreelancerProfileModal(existing=existing))

    @app_commands.command(name="ticket-panel", description="Spawns the ticket creation panel")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎫 Ticket Center",
            description="Welcome to our ticket center. Here you can open a ticket to request a quote, get support for a product, or apply to work with us.",
            color=config.COLOR_MAIN,
        )
        embed.set_footer(text=config.STUDIO_FOOTER)

        await interaction.response.send_message("Sending ticket panel...", ephemeral=True)
        await interaction.channel.send(embed=embed, view=TicketPanelView())


async def setup(bot):
    await bot.add_cog(Tickets(bot))
