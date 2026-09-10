import asyncio
import logging
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
ARCHIVE_CATEGORY_ID = 1544151748900425829
FREELANCER_CATEGORY_ID = getattr(config, "FREELANCER_CATEGORY_ID", config.TICKET_CATEGORY_ID)


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
                quoted_deadline TEXT
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
        ):
            try:
                await db.execute(stmt)
            except Exception:
                pass
        await db.commit()


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
                msg = await send_incoming_quote_card(
                    customer_channel,
                    quote_id=quote_id,
                    freelancer=interaction.user,
                    amount=str(self.amount),
                    deadline=str(self.deadline),
                    comment=str(self.comment) if self.comment.value else None,
                    profile=profile,
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

async def send_incoming_quote_card(customer_channel, quote_id, freelancer, amount, deadline, comment, profile):
    embed = discord.Embed(
        title="Incoming Quote",
        description=f"{freelancer.mention} has quoted **${amount}**",
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url=freelancer.display_avatar.url)
    embed.add_field(name="Portfolio", value=profile["portfolio"], inline=False)
    embed.add_field(name="Timezone", value=profile["timezone"], inline=False)
    embed.add_field(name="Tech Stack", value=profile["tech_stack"], inline=False)
    embed.add_field(name="Bio", value=profile["bio"], inline=False)
    if comment:
        embed.add_field(name="Message from freelancer:", value=f"```{comment}```", inline=False)
    embed.add_field(name="Deadline", value=deadline, inline=False)
    embed.set_footer(text=config.STUDIO_FOOTER)
    embed.timestamp = discord.utils.utcnow()

    return await customer_channel.send(embed=embed, view=IncomingQuoteView(quote_id=quote_id, amount=amount))


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

        assigned_member = interaction.guild.get_member(quote["freelancer_id"])

        await interaction.followup.send(
            f"✅ Ai acceptat oferta de la {assigned_member.mention if assigned_member else '<@' + str(quote['freelancer_id']) + '>'}! "
            "Puteți discuta direct în acest canal de acum înainte."
        )

        # Grey out and disable every other pending quote card - the project
        # is taken.
        for oq_id, oq_message_id, oq_freelancer_id in other_quotes:
            if not oq_message_id:
                continue
            try:
                msg = await interaction.channel.fetch_message(oq_message_id)
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
            oq_member = interaction.guild.get_member(oq_freelancer_id)
            if oq_member:
                try:
                    await oq_member.send(
                        f"Clientul a ales o altă ofertă pentru ticketul din {interaction.guild.name}. Mulțumim oricum!"
                    )
                except discord.HTTPException:
                    pass

        # Transfer the winning freelancer directly into the client's ticket -
        # they now share this channel and talk to each other with no relay.
        if assigned_member:
            await interaction.channel.set_permissions(
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
        await interaction.channel.send(embed=reveal_embed)

        freelancer_channel = interaction.guild.get_channel(ticket["freelancer_channel_id"])
        if freelancer_channel:
            await freelancer_channel.send(
                f"✅ Clientul a ales oferta lui "
                f"{assigned_member.mention if assigned_member else '<@' + str(quote['freelancer_id']) + '>'}. "
                "Acest canal se arhivează."
            )
            await archive_freelancer_channel(freelancer_channel)

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

        freelancer_channel = interaction.guild.get_channel(ticket["freelancer_channel_id"]) if ticket else None
        if freelancer_channel:
            await freelancer_channel.send(f"❌ Clientul a refuzat oferta de <@{quote['freelancer_id']}>.")

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

        ticket = await get_ticket_by_id(quote["ticket_id"])
        freelancer_channel = interaction.guild.get_channel(ticket["freelancer_channel_id"]) if ticket else None
        if not freelancer_channel:
            await interaction.response.send_message("❌ Nu am găsit canalul freelancerilor.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔁 Contraofertă de la client",
            description=f"<@{quote['freelancer_id']}>, clientul a propus **${self.amount}**"
                        + (f" cu deadline **{self.deadline}**" if self.deadline.value else ""),
            color=discord.Color.orange(),
        )
        if self.message.value:
            embed.add_field(name="Mesaj:", value=f"```{self.message}```", inline=False)
        embed.set_footer(text=config.STUDIO_FOOTER)
        embed.timestamp = interaction.created_at

        await freelancer_channel.send(
            content=f"<@{quote['freelancer_id']}>",
            embed=embed,
            view=CounterofferResponseView(quote_id=quote["id"], amount=str(self.amount)),
        )
        await interaction.response.send_message("✅ Contraoferta ta a fost trimisă freelancerului.", ephemeral=True)


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

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        async with get_db() as db:
            await db.execute("UPDATE quotes SET amount = ? WHERE id = ?", (self.amount, quote["id"]))
            await db.commit()

        await interaction.followup.send(
            "✅ Ai acceptat contraoferta. Cardul ofertei tale a fost actualizat pentru client - "
            "poate o accepta oricând."
        )

        ticket = await get_ticket_by_id(quote["ticket_id"])
        if ticket:
            customer_channel = interaction.guild.get_channel(ticket["customer_channel_id"])
            if customer_channel and quote["message_id"]:
                try:
                    msg = await customer_channel.fetch_message(quote["message_id"])
                    embed = msg.embeds[0] if msg.embeds else None
                    freelancer = interaction.guild.get_member(quote["freelancer_id"]) or interaction.user
                    if embed:
                        embed.description = (
                            f"{freelancer.mention} has quoted **${self.amount}** (updated after counteroffer)"
                        )
                    await msg.edit(
                        embed=embed,
                        view=IncomingQuoteView(quote_id=quote["id"], amount=self.amount),
                    )
                except discord.HTTPException:
                    pass

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
            customer_channel = interaction.guild.get_channel(ticket["customer_channel_id"])
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
        await interaction.response.send_message("⭐ Displaying freelancer reviews...", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Get a quote", style=discord.ButtonStyle.success, emoji="💵", custom_id="mythral_ticket_quote")
    async def quote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_open_ticket(interaction.user.id)
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(QuoteModal())

    @discord.ui.button(label="Apply for freelancer", style=discord.ButtonStyle.primary, emoji="💼", custom_id="mythral_ticket_apply")
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_open_ticket(interaction.user.id)
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(ApplyModal())

    @discord.ui.button(label="General Support", style=discord.ButtonStyle.secondary, emoji="🎧", custom_id="mythral_ticket_support")
    async def support_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_open_ticket(interaction.user.id)
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(SupportModal())


# ---------------------------------------------------------------------------
# SHARED LOGIC - lookups
# ---------------------------------------------------------------------------

async def get_open_ticket(user_id: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT COALESCE(customer_channel_id, channel_id) FROM tickets "
            "WHERE owner_id = ? AND status NOT IN ('denied', 'closed')",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


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
            "SELECT rowid AS id, * FROM tickets WHERE customer_channel_id = ?", (channel_id,)
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
# TICKET CREATION
# ---------------------------------------------------------------------------

async def create_quote_ticket(interaction: discord.Interaction, fields: list[tuple[str, str]]):
    """Creates two separate channels (customer / freelancer) so neither side
    can see or interact with the other until a quote is accepted."""
    guild = interaction.guild
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
    freelancer_embed.add_field(name="Rating", value="⭐⭐⭐⭐⭐ (0)", inline=False)
    freelancer_embed.set_footer(text=config.STUDIO_FOOTER)
    freelancer_embed.timestamp = interaction.created_at

    ping = f"New quote request for {freelancer_role.mention}." if freelancer_role else "New quote request received."
    await freelancer_channel.send(content=ping, embed=freelancer_embed, view=NewTicketActionsView())

    # Any freelancer with the role can post here to chat with the client and
    # form an opinion on the project before quoting - no first-reply claim,
    # everyone stays able to talk until a quote is accepted.
    chat_embed = discord.Embed(
        title="💬 Discuție cu clientul",
        description="Scrie aici oricând pentru a discuta cu clientul și a-ți face o idee despre proiect. "
                     "Mesajele tale ajung la client anonim, fără numele tău. Când ești pregătit, trimite o "
                     "ofertă cu butonul **Quote** de mai sus.",
        color=discord.Color.blurple(),
    )
    await freelancer_channel.send(embed=chat_embed)

    # Embed sent to the customer channel, WITHOUT freelancer identity
    customer_embed = discord.Embed(
        title="✅ Cererea ta a fost trimisă",
        description="Freelancerii din echipa noastră pot analiza cererea și te pot contacta direct aici, anonim, "
                     "pentru a discuta detalii și a-ți trimite oferte. Nu vei putea vedea cine anume lucrează la "
                     "fiecare ofertă până când nu o accepți.",
        color=discord.Color.green(),
    )
    customer_embed.set_footer(text=config.STUDIO_FOOTER)
    customer_embed.timestamp = interaction.created_at
    await customer_channel.send(embed=customer_embed)

    await interaction.response.send_message(f"✅ Your ticket has been created: {customer_channel.mention}", ephemeral=True)


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
    await interaction.response.send_message(f"✅ Your ticket has been created: {channel.mention}", ephemeral=True)


# ---------------------------------------------------------------------------
# ANONYMOUS GROUP RELAY (pre-acceptance discussion between client and every
# freelancer with access to the offer channel)
# ---------------------------------------------------------------------------

async def relay_message(destination_channel: discord.TextChannel, message: discord.Message, label: str):
    embed = discord.Embed(description=message.content or "*[fără text]*", color=discord.Color.blurple())
    embed.set_author(name=label)
    embed.timestamp = message.created_at

    files = [await a.to_file() for a in message.attachments] if message.attachments else []
    await destination_channel.send(embed=embed, files=files)

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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        # Freelancer discussion channel: any freelancer can talk to the
        # client to form an opinion on the project. Relayed anonymously -
        # no one freelancer claims the client by being first to reply.
        ticket = await get_ticket_by_freelancer_channel(message.channel.id)
        if ticket:
            if ticket["status"] != "open":
                return  # a freelancer already won this project, channel is being archived
            freelancer_role = message.guild.get_role(FREELANCER_ROLE_ID)
            if freelancer_role and freelancer_role in message.author.roles:
                customer_channel = message.guild.get_channel(ticket["customer_channel_id"])
                if customer_channel:
                    await relay_message(customer_channel, message, label="💬 Freelancer")
            return

        # Customer channel: the client talking back to the whole freelancer
        # group. Once a quote is accepted the winning freelancer shares this
        # very channel, so nothing needs relaying anymore.
        ticket = await get_ticket_by_customer_channel(message.channel.id)
        if ticket and ticket["status"] == "open" and message.author.id == ticket["owner_id"]:
            freelancer_channel = message.guild.get_channel(ticket["freelancer_channel_id"])
            if freelancer_channel:
                await relay_message(freelancer_channel, message, label="💬 Client")
            return

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
