import discord
from discord import app_commands
from discord.ext import commands

import config
from database import get_db

TICKET_TYPES = {
    "quote": ("📝", "Get a quote"),
    "apply": ("🪪", "Apply for freelancer"),
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
        placeholder="e.g., $50-$100",
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
        ticket_suffix = interaction.channel.name.replace("freelance-", "")
        customer_channel = discord.utils.get(interaction.guild.text_channels, name=f"ticket-{ticket_suffix}")
        
        if not customer_channel:
            await interaction.response.send_message("❌ Nu am putut găsi canalul corespondent al clientului.", ephemeral=True)
            return

        embed = discord.Embed(
            title="💰 Official Project Quote",
            description=f"A freelancer ({interaction.user.mention}) has submitted a price offer for this project.",
            color=discord.Color.gold()
        )
        embed.add_field(name="Amount", value=str(self.amount), inline=True)
        embed.add_field(name="Deadline", value=str(self.deadline), inline=True)
        if self.comment.value:
            embed.add_field(name="Comment", value=str(self.comment), inline=False)
            
        embed.set_footer(text=config.STUDIO_FOOTER)
        embed.timestamp = interaction.created_at
        
        class AcceptQuoteView(discord.ui.View):
            def __init__(self, freelancer, freelance_channel):
                super().__init__(timeout=None)
                self.freelancer = freelancer
                self.freelance_channel = freelance_channel
                
            @discord.ui.button(label="Accept Quote", style=discord.ButtonStyle.success, emoji="✅", custom_id="accept_freelance_quote")
            async def accept_callback(self, button_interaction: discord.Interaction):
                await button_interaction.channel.set_permissions(self.freelancer, view_channel=True, send_messages=True, attach_files=True)
                await button_interaction.response.send_message(f"🎉 Cota a fost acceptată! {self.freelancer.mention} a fost adăugat în chat. Puteți începe colaborarea!")
                
                self.clear_items()
                await button_interaction.message.edit(view=self)
                
                if self.freelance_channel:
                    try:
                        await self.freelance_channel.delete(reason="Cota a fost acceptata.")
                    except:
                        pass

        await customer_channel.send(embed=embed, view=AcceptQuoteView(interaction.user, interaction.channel))
        await interaction.response.send_message("✅ Oferta ta a fost trimisă cu succes în panoul clientului!", ephemeral=True)


# ---------------------------------------------------------------------------
# DENY REASONS DROPDOWN (Exclusive for Freelancers)
# ---------------------------------------------------------------------------

class DenyReasonSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Won't fit in the deadline", description="The deadline is too short for our freelancers.", emoji="⌛"),
            discord.SelectOption(label="Not interested", description="Our freelancers are not interested in this project.", emoji="❌"),
            discord.SelectOption(label="Not my niche", description="This project is outside of our studio's expertise.", emoji="🎯"),
            discord.SelectOption(label="Budget", description="The budget is too low for the requested work.", emoji="💰"),
        ]
        super().__init__(placeholder="Select a deny reason", min_values=1, max_values=1, options=options, custom_id="mythral_deny_select")

    async def callback(self, interaction: discord.Interaction):
        freelancer_role = interaction.guild.get_role(1544135641275568158)
        if freelancer_role not in interaction.user.roles:
            await interaction.response.send_message("❌ Only freelancers can select the deny reason.", ephemeral=True)
            return

        reason = self.values
        await interaction.response.send_message(f"🔒 Ticket denied. Reason: **{reason}**. Archiving...", ephemeral=True)

        embed_reason = discord.Embed(
            title="Ticket Denied",
            description=f"This ticket has been rejected by the freelancers.\n\n**Reason:** {reason}",
            color=discord.Color.red()
        )
        await interaction.channel.send(embed=embed_reason)

        await interaction.channel.set_permissions(interaction.guild.default_role, view_channel=False)
        archive_category = interaction.guild.get_channel(1544151748900425829)
        if archive_category:
            await interaction.channel.edit(category=archive_category, name=f"closed-{interaction.channel.name[-4:]}")


class DenyReasonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(DenyReasonSelect())


# ---------------------------------------------------------------------------
# VIEWS FOR PANELS (Exclusive for Freelancers)
# ---------------------------------------------------------------------------

class NewTicketActionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Quote", style=discord.ButtonStyle.success, custom_id="mythral_action_quote")
    async def quote_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        freelancer_role = interaction.guild.get_role(1544135641275568158)
        if freelancer_role not in interaction.user.roles:
            await interaction.response.send_message("❌ Only freelancers can submit a quote for this ticket.", ephemeral=True)
            return
        await interaction.response.send_modal(QuotePriceModal())

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="mythral_action_deny")
    async def deny_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        freelancer_role = interaction.guild.get_role(1544135641275568158)
        if freelancer_role not in interaction.user.roles:
            await interaction.response.send_message("❌ Only freelancers can use the Deny button for this ticket.", ephemeral=True)
            return

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
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Get a quote", style=discord.ButtonStyle.success, emoji="📝", custom_id="mythral_ticket_quote")
    async def quote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_open_ticket(interaction.user.id, interaction.guild.id)
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(QuoteModal())

    @discord.ui.button(label="Apply for freelancer", style=discord.ButtonStyle.primary, emoji="🪪", custom_id="mythral_ticket_apply")
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
    clean_name = interaction.user.name.lower().replace(" ", "-")
    
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    
    staff_role = guild.get_role(config.STAFF_ROLE_ID)
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        
    if ticket_type != "quote":
        freelancer_role = guild.get_role(1544135641275568158)
        if freelancer_role:
            overwrites[freelancer_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            
    customer_channel = await guild.create_text_channel(
        name=f"ticket-{clean_name}",
        category=category,
        overwrites=overwrites
    )
    
    async with get_db() as db:
        await db.execute(
            "INSERT INTO tickets (channel_id, owner_id, ticket_type) VALUES (?, ?, ?)",
            (customer_channel.id, interaction.user.id, ticket_type),
        )
        await db.commit()
        
    embed = discord.Embed(title="Information", color=discord.Color.green())
    for name, value in fields:
        embed.add_field(name=name, value=value or "-", inline=False)
    embed.add_field(name="Rating", value="⭐ ⭐ ⭐ ⭐ ⭐ (0)", inline=False)
    embed.set_footer(text=config.STUDIO_FOOTER)
    embed.timestamp = interaction.created_at
    
    if ticket_type == "quote":
        freelancer_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }
        freelancer_role = guild.get_role(1544135641275568158)
        if freelancer_role:
            freelancer_overwrites[freelancer_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if staff_role:
            freelancer_overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            
        freelancer_channel = await guild.create_text_channel(
            name=f"freelance-{clean_name}",
            category=category,
            overwrites=freelancer_overwrites
        )
        
        ping = "New ticket for <@&1544135641275568158>."
        await freelancer_channel.send(content=ping, embed=embed, view=NewTicketActionsView())
        
        await customer_channel.send(embed=embed)
        await customer_channel.send(f"👋 {interaction.user.mention}, cererea ta a fost trimisă către freelanceri! Vei primi ofertele de preț direct aici în cel mai scurt timp.")
    else:
        await customer_channel.send(embed=embed, view=NewTicketActionsView() if ticket_type == "apply" else None)
        
    await interaction.response.send_message(f"✅ Your ticket has been created: {customer_channel.mention}", ephemeral=True)


# ---------------------------------------------------------------------------
# COG REGISTRATION & SLASH COMMAND DEFINITION
# ---------------------------------------------------------------------------

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(NewTicketActionsView())

    @app_commands.command(name="ticket-panel", description="Spawns the ticket creation panel")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎫 Ticket Center",
            description="Welcome to our ticket center. Here you can open a ticket to request a quote, get support for a product, or apply to work with us.",
            color=config.COLOR_MAIN
        )
        embed.set_footer(text=config.STUDIO_FOOTER)

        await interaction.response.send_message("Sending ticket panel...", ephemeral=True)
        await interaction.channel.send(embed=embed, view=TicketPanelView())


async def setup(bot):
    await bot.add_cog(Tickets(bot))
