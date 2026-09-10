import asyncio
import logging

import discord
from discord.ext import commands

import config
from database import init_db
from cogs.tickets import ensure_schema

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

EXTENSIONS = [
    "cogs.tickets",
    "cogs.profiles",
    # "cogs.reviews",  # dezactivat complet
    "cogs.levels",
    "cogs.store",
    "cogs.interactions",
]


@bot.event
async def on_ready():
    print(f"[OK] Conectat ca {bot.user} (ID: {bot.user.id})")
    try:
        # Sincronizăm curat comenzile doar pe serverul tău
        ID_SERVER = 1544005370383704207 
        server_obiect = discord.Object(id=ID_SERVER)
        
        # Copiem comenzile din module în server și le trimitem la Discord
        bot.tree.copy_global_to(guild=server_obiect)
        synced = await bot.tree.sync(guild=server_obiect)
        
        print(f"[SUCCESS] {len(synced)} comenzi slash active pe server!")
    except Exception as e:
        print(f"[EROARE] Sincronizare comenzi: {e}")


async def main():
    await init_db()
    await ensure_schema()

    async with bot:
        for ext in EXTENSIONS:
            try:
                await bot.load_extension(ext)
                print(f"[OK] Modul incarcat: {ext}")
            except Exception as e:
                print(f"[EROARE] Nu am putut incarca {ext}: {e}")
        await bot.start(config.BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
